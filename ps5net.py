"""Descubrimiento de la PS5 por red: el disparador del modo automatico.

La consola responde al protocolo de descubrimiento de Remote Play en UDP 9302.
Verificado en vivo en esta maquina: 103 ms de latencia, 10 respuestas de 10.

  -> SRCH * HTTP/1.1
     device-discovery-protocol-version:00030010
  <- HTTP/1.1 200 Ok
     host-id:XXXXXXXXXXXX  host-type:PS5  host-name:PS5-XXX
     host-request-port:997  system-version:13600007

Este es el disparador correcto y no la capturadora, por dos razones medidas:
la tarjeta se alimenta del bus USB, asi que perder la senal HDMI no la retira
del sistema (LastRemovalDate vacio, cero eventos Kernel-PnP de retirada en 30
dias); y sondear la red no toca el dispositivo, que es de acceso exclusivo.
"""

import logging
import re
import socket
import sys

log = logging.getLogger("ps5net")

PORT = 9302
PROTO = "00030010"                       # PS5; el 00020020 es de PS4
PAYLOAD = ("SRCH * HTTP/1.1\n"
           "device-discovery-protocol-version:%s\n" % PROTO).encode("ascii")

# Estados. La distincion importa: en reposo la consola TAMBIEN responde, pero no
# hay senal HDMI, asi que abrir la ventana ahi seria abrir un rectangulo negro.
READY = "ready"          # 200: encendida
STANDBY = "standby"      # 620: en reposo
GONE = "gone"            # sin respuesta: apagada, o sin red

# Se parsea el CODIGO, nunca la frase. startswith("HTTP/1.1 620") se rompe si
# cambia la reason-phrase o si algun dia llega con \r\n.
_STATUS = re.compile(rb"^HTTP/1\.1\s+(\d{3})\b")


def _parse(data):
    m = _STATUS.match(data)
    if not m:
        return None
    info = {"status": int(m.group(1))}
    for linea in data.split(b"\n")[1:]:
        if b":" in linea:
            k, _, v = linea.partition(b":")
            info[k.strip().decode("ascii", "replace").lower()] = \
                v.strip().decode("utf-8", "replace")
    return info


def local_ipv4s():
    """IPs locales utiles, descartando loopback, link-local y rangos de VPN.

    Nunca cablear una IP concreta: si el PC pasa de Ethernet a Wi-Fi, el DHCP
    cambia la direccion o se levanta una VPN, un bind fijo falla con
    WSAEADDRNOTAVAIL y la deteccion muere EN SILENCIO. Se enumera cada vez.
    """
    ips = set()
    # La ruta por defecto, que es la que casi siempre sirve. El socket UDP no
    # llega a enviar nada: connect() sobre UDP solo fija la ruta.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 53))
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass

    try:
        for res in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(res[4][0])
    except OSError:
        pass

    buenas = []
    for ip in ips:
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        if ip.startswith("10.2.0."):        # rango tipico de ProtonVPN
            continue
        buenas.append(ip)
    return sorted(buenas)


def _broadcast_of(ip):
    partes = ip.split(".")
    if len(partes) != 4:
        return None
    return ".".join(partes[:3] + ["255"])   # asume /24, que es lo domestico


def probe(ip, timeout=1.5):
    """Sondeo unicast a una IP conocida. Es el camino normal: 103 ms."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(PAYLOAD, (ip, PORT))
        data, _ = s.recvfrom(2048)
        return _parse(data)
    except socket.timeout:
        return None
    except OSError as exc:
        log.debug("sondeo a %s fallido: %s", ip, exc)
        return None
    finally:
        s.close()


def discover(timeout=2.5):
    """Descubrimiento por difusion en TODOS los interfaces locales.

    Se filtra por host-type PS5: una PS4 responde al mismo protocolo en el mismo
    puerto y seria adoptada por error. Y se deduplica por host-id, porque enviar
    a 255.255.255.255 y a la difusion de la subred devuelve la misma consola dos
    veces.
    """
    encontradas = {}
    for local in local_ipv4s() or [""]:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(timeout)
        try:
            if local:
                s.bind((local, 0))
            destinos = ["255.255.255.255"]
            bc = _broadcast_of(local) if local else None
            if bc:
                destinos.append(bc)
            for d in destinos:
                try:
                    s.sendto(PAYLOAD, (d, PORT))
                except OSError:
                    pass
            while True:
                try:
                    data, addr = s.recvfrom(2048)
                except socket.timeout:
                    break
                info = _parse(data)
                if not info:
                    continue
                if info.get("host-type", "").upper() != "PS5":
                    log.info("se ignora %s (%s), no es una PS5",
                             addr[0], info.get("host-type", "?"))
                    continue
                info["ip"] = addr[0]
                info["local_ip"] = local
                encontradas[info.get("host-id", addr[0])] = info
        except OSError as exc:
            log.debug("difusion desde %s fallida: %s", local, exc)
        finally:
            s.close()
    return list(encontradas.values())


def state_of(info):
    if not info:
        return GONE
    return READY if info["status"] == 200 else STANDBY


# Un sondeo unicast a una consola encendida se contesta en 103 ms medidos, asi
# que 0,4 s de espera es margen de sobra. Importa mucho mas de lo que parece:
# este timeout es lo que cuesta CADA sondeo cuando la consola esta apagada, y
# por tanto marca cada cuanto nos podemos enterar de que se ha encendido.
PROBE_TIMEOUT = 0.4


def status(ip=None, host_id=None, timeout=PROBE_TIMEOUT, rediscover=True):
    """Estado de la consola. Devuelve (estado, info).

    Primero unicast a la IP cacheada, que es barato. Si no contesta y se pide,
    se redescubre por difusion, porque el DHCP puede haberle cambiado la IP; ahi
    se empareja por host-id, NUNCA por IP ni por MAC: la MAC de la tabla ARP no
    coincide con el host-id porque son interfaces distintas (cable y Wi-Fi).

    rediscover=False es para el sondeo rutinario. El rastreo por difusion cuesta
    otro timeout entero POR INTERFAZ, y hacerlo en cada vuelta con la consola
    apagada convertia un sondeo de 0,4 s en uno de 3 s: el bucle se arrastraba y
    encender la consola tardaba una eternidad en notarse. La IP casi nunca
    cambia, asi que se redescubre de vez en cuando, no siempre.
    """
    if ip:
        info = probe(ip, timeout)
        if info:
            info["ip"] = ip
            if not host_id or info.get("host-id") == host_id:
                return state_of(info), info
            log.warning("en %s responde otra consola (%s, esperada %s)",
                        ip, info.get("host-id"), host_id)

    if not rediscover and ip:
        return GONE, None

    for info in discover(max(timeout, 1.0)):
        if not host_id or info.get("host-id") == host_id:
            if ip and info["ip"] != ip:
                log.info("la consola ha cambiado de IP: %s -> %s", ip, info["ip"])
            return state_of(info), info

    return GONE, None


if __name__ == "__main__":
    import core
    core.setup("ps5net-test")
    print("IPs locales:", local_ipv4s())
    print()
    guardada_ip = core.get("ps5_ip")
    guardada_id = core.get("ps5_host_id")
    print("cacheado: ip=%r host-id=%r" % (guardada_ip, guardada_id))

    estado, info = status(guardada_ip, guardada_id)
    print("estado  :", estado)
    if info:
        for k in ("status", "ip", "host-id", "host-name", "host-type", "system-version"):
            if k in info:
                print("  %-15s %s" % (k, info[k]))
        if "--save" in sys.argv:
            core.save_config(ps5_ip=info["ip"], ps5_host_id=info.get("host-id", ""))
            print("\nguardado en la configuracion")
    else:
        print("  (apagada, o en reposo sin red)")
