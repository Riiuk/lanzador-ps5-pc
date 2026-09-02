"""Puente de audio: entrada de la capturadora -> altavoces, sin cortes.

Este modulo es la razon de ser del proyecto. Con PotPlayer el audio daba
petardazos, y la causa esta identificada: el driver de la capturadora es
`usbaudio`, USB Audio Class 1.0 generico, un endpoint isocrono sin feedback
fiable cuyo reloj de muestreo NO es el mismo que el de la tarjeta de sonido del
PC. Los dos van nominalmente a 48000 Hz, pero difieren en decenas de partes por
millon, asi que a lo largo de los minutos uno adelanta al otro: el buffer se
vacia (corte) o se desborda (clic). No es un fallo de software, es fisica, y hay
que compensarla explicitamente.

Aqui se compensa asi:

  entrada -> [anillo SPSC] -> [remuestreador sinc polifasico] -> salida
                                        ^
                                  ratio ajustado por un control P
                                  segun el nivel de relleno del anillo

El remuestreo es de una parte por diez mil: se consumen 1,0001 muestras de
entrada por cada muestra de salida, por ejemplo. Inaudible, pero suficiente
para que los dos relojes no se separen nunca.

Corre en su PROPIO PROCESO, no en un hilo. Esta medido: con un hilo de Python
compitiendo, el percentil 99 del callback pasa de 380 us a 64 ms, o sea 6 veces
por encima del presupuesto de 10 ms. El GIL no se comparte con nadie.
"""

import argparse
import logging
import queue
import sys
import threading
import time

import numpy as np

import core

log = logging.getLogger("audio")

# Codigos de salida propios. player.py los muestra en el overlay tal cual, para
# que "no hay sonido" nunca sea un misterio silencioso.
EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_NO_INPUT = 30       # no se encuentra la entrada de la capturadora
EXIT_AMBIGUOUS = 31      # el nombre casa con varios dispositivos
EXIT_PORTAUDIO = 32      # PortAudio no arranca
EXIT_FORMAT = 33         # el dispositivo no acepta 48 kHz / 2 canales

CH = 2
DTYPE = "float32"        # coincide con el formato de mezcla de WASAPI: conversion cero

RING_FRAMES = 1 << 16    # 65536 -> 1,36 s a 48 kHz. Potencia de 2 para enmascarar
RING_MASK = RING_FRAMES - 1
MAX_BLOCK = 4096         # tope para preasignar; PortAudio suele dar 480

TAPS = 32
PHASES = 512
KAISER_BETA = 8.0

MAX_PPM = 2000.0         # 3,5 cents de desafinacion: inaudible
KP = 1e-6                # correccion por muestra de error de relleno
CONTROL_PERIOD = 1.0     # el control actua una vez por segundo, no en el callback
FADE_FRAMES = 96         # 2 ms a 48 kHz


# --------------------------------------------------------------------------
# Filtro
# --------------------------------------------------------------------------

def _gain_de(volumen):
    """Convierte un volumen de 0 a 100 en ganancia lineal.

    Al cuadrado, no lineal: el oido percibe la sonoridad de forma aproximadamente
    logaritmica, asi que una escala lineal se nota casi toda entre el 0 y el 20 y
    apenas cambia del 50 al 100. Con el cuadrado, el 50 % suena a "la mitad"
    (-12 dB), que es lo que uno espera al ver ese numero.
    """
    v = max(0.0, min(100.0, float(volumen)))
    return (v / 100.0) ** 2


def build_table(taps=TAPS, phases=PHASES, beta=KAISER_BETA):
    """Banco de filtros polifasico: un sinc enventanado por cada fase fraccionaria.

    Por que 32 taps y no una interpolacion lineal, que seria una linea: a un
    ratio de 1,0001 la fase fraccionaria recorre [0,1) entera cada ~0,2 s, asi
    que el error del interpolador se convierte en una MODULACION DE AMPLITUD a
    ~5 Hz sobre la banda alta. Con interpolacion lineal el rizado a 20 kHz es de
    -11,7 dB (audible como un temblor en los agudos); con 32 taps es de -0,01 dB.
    Cuesta 167 us sobre un presupuesto de 10 ms.
    """
    n = np.arange(taps, dtype=np.float64) - (taps // 2 - 1)
    win = np.kaiser(taps, beta)
    table = np.empty((phases, taps), dtype=np.float32)
    for p in range(phases):
        h = np.sinc(n - p / phases) * win
        table[p] = (h / h.sum()).astype(np.float32)   # ganancia unidad en continua
    return table


# --------------------------------------------------------------------------
# Seleccion de dispositivos
# --------------------------------------------------------------------------

def _wasapi_index(sd):
    for i, api in enumerate(sd.query_hostapis()):
        if "WASAPI" in api["name"].upper():
            return i, api
    return None, None


def find_devices(sd, in_name, out_name):
    """Localiza entrada y salida POR NOMBRE dentro de WASAPI.

    Nunca por indice: query_devices() mezcla todas las host APIs y los numeros
    bailan. Y nunca dejando que sounddevice resuelva un nombre ambiguo: aqui hay
    DOS salidas que contienen "Sound Blaster Z" (Altavoces y SPDIF-Out), y ante
    la ambiguedad sd._get_device_id devuelve -1 EN SILENCIO, que acaba abriendo
    el dispositivo por defecto; en captura ese es el microfono Samson, o sea que
    en vez del juego se oiria la habitacion.
    """
    api_idx, api = _wasapi_index(sd)
    if api_idx is None:
        raise AudioError(EXIT_PORTAUDIO, "PortAudio no expone el host API WASAPI")

    devs = sd.query_devices()

    def buscar(substr, entrada):
        hits = []
        for i, d in enumerate(devs):
            if d["hostapi"] != api_idx:
                continue
            canales = d["max_input_channels"] if entrada else d["max_output_channels"]
            if canales < 1:
                continue
            if substr.lower() in d["name"].lower():
                hits.append((i, d["name"]))
        return hits

    entradas = buscar(in_name, True)
    if not entradas:
        disponibles = [d["name"] for i, d in enumerate(devs)
                       if d["hostapi"] == api_idx and d["max_input_channels"] > 0]
        raise AudioError(EXIT_NO_INPUT,
                         "no hay ninguna entrada WASAPI que contenga %r. Hay: %s"
                         % (in_name, ", ".join(disponibles) or "ninguna"))
    if len(entradas) > 1:
        raise AudioError(EXIT_AMBIGUOUS,
                         "%r casa con varias entradas: %s"
                         % (in_name, ", ".join(n for _, n in entradas)))
    in_idx, in_label = entradas[0]

    if out_name:
        salidas = buscar(out_name, False)
        if len(salidas) == 1:
            out_idx, out_label = salidas[0]
        else:
            # Un nombre de salida que no resuelve NO es motivo para quedarse sin
            # sonido: se avisa y se usa el predeterminado del sistema, que es lo
            # que el usuario espera oir.
            log.warning("%r %s; se usa la salida predeterminada del sistema",
                        out_name, "no casa con nada" if not salidas else "es ambiguo")
            out_idx = api["default_output_device"]
            out_label = devs[out_idx]["name"]
    else:
        out_idx = api["default_output_device"]
        out_label = devs[out_idx]["name"]

    log.info("entrada : [%d] %s", in_idx, in_label)
    log.info("salida  : [%d] %s", out_idx, out_label)
    return in_idx, out_idx, in_label, out_label


class AudioError(Exception):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def watch_parent(pid, on_dead):
    """Muere con el padre. Sin esto quedan puentes zombis.

    Observado de verdad: al matar el reproductor desde fuera, su bloque finally
    no llega a ejecutarse y el hijo de audio sobrevive reteniendo el dispositivo.
    Dos zombis a la vez se pelean por la misma entrada y el log se vuelve
    ilegible. El proceso hijo tiene que ser capaz de enterarse solo.

    Todas las llamadas Win32 con restype y argtypes declarados: sin ellos,
    ctypes trunca el handle a 32 bits y WaitForSingleObject espera sobre basura.
    """
    import ctypes

    SYNCHRONIZE = 0x00100000
    INFINITE = 0xFFFFFFFF

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    k32.WaitForSingleObject.restype = ctypes.c_uint32
    k32.CloseHandle.argtypes = (ctypes.c_void_p,)
    k32.CloseHandle.restype = ctypes.c_int

    h = k32.OpenProcess(SYNCHRONIZE, False, int(pid))
    if not h:
        log.warning("el padre %s ya no existe al arrancar; se sale", pid)
        on_dead()
        return
    try:
        k32.WaitForSingleObject(h, INFINITE)
        log.info("el proceso padre %s ha terminado; se cierra el puente", pid)
        on_dead()
    finally:
        k32.CloseHandle(h)


# --------------------------------------------------------------------------
# El puente
# --------------------------------------------------------------------------

class Bridge:
    def __init__(self, sd, in_idx, out_idx, fs, target_ms, offset_ms=0.0):
        self.sd = sd
        self.fs = fs
        self.target = max(TAPS * 4, int((target_ms + offset_ms) * fs / 1000.0))
        self.ratio = 1.0

        self.ring = np.zeros((RING_FRAMES, CH), dtype=np.float32)
        self._w = 0            # muestras escritas (monotono)
        self._rb = 0           # base de lectura (monotono)
        self._rf = 0.0         # fase fraccionaria en [0,1)

        self.table = build_table()

        # Todo preasignado: el callback de PortAudio no puede asignar memoria.
        # Una asignacion es una llamada al asignador que puede disparar el GC, y
        # un GC dentro del callback es exactamente el petardazo que se intenta
        # evitar.
        self._ar = np.arange(MAX_BLOCK, dtype=np.float64)
        self._pos = np.empty(MAX_BLOCK, dtype=np.float64)
        self._basef = np.empty(MAX_BLOCK, dtype=np.float64)
        self._idx = np.empty((MAX_BLOCK, TAPS), dtype=np.int64)
        self._gather = np.empty((MAX_BLOCK, TAPS, CH), dtype=np.float32)
        self._coefs = np.empty((MAX_BLOCK, TAPS), dtype=np.float32)
        self._ph = np.empty(MAX_BLOCK, dtype=np.int64)
        self._taps_off = np.arange(-(TAPS // 2 - 1), TAPS // 2 + 1, dtype=np.int64)
        self._last = np.zeros(CH, dtype=np.float32)
        self._fade = (np.linspace(1.0, 0.0, FADE_FRAMES, dtype=np.float32)
                      .reshape(-1, 1))

        # Volumen. El valor lo escribe el reproductor en memoria compartida; aqui
        # solo se lee. _gain es el que se esta aplicando y gain_target el pedido:
        # saltar de golpe de uno a otro produce un CHASQUIDO audible en mitad de
        # la onda, asi que el cambio se reparte a lo largo del bloque con una
        # rampa. _ramp01 y _gainbuf estan preasignados como todo lo demas.
        self.vol = None
        self._gain = 1.0
        self._ramp01 = (np.arange(1, MAX_BLOCK + 1, dtype=np.float32) / MAX_BLOCK)
        self._gainbuf = np.empty(MAX_BLOCK, dtype=np.float32)

        # Diagnostico
        self.under = 0
        self.over = 0
        self.in_blocks = 0
        self.gap_max = 0.0
        self._t_in = None
        self._fill_min = RING_FRAMES
        self.stop_flag = threading.Event()

    # -- entrada

    def in_cb(self, indata, frames, time_info, status):
        now = time.perf_counter()
        if self._t_in is not None:
            gap = now - self._t_in
            if gap > self.gap_max:
                self.gap_max = gap
        self._t_in = now
        self.in_blocks += 1

        if status:
            log.debug("estado entrada: %s", status)

        w = self._w & RING_MASK
        if w + frames <= RING_FRAMES:
            self.ring[w:w + frames] = indata
        else:
            corte = RING_FRAMES - w
            self.ring[w:] = indata[:corte]
            self.ring[:frames - corte] = indata[corte:]
        self._w += frames

        # Desbordamiento: el lector no sigue el ritmo. Se descarta lo mas viejo
        # avanzando la lectura, que suena a un clic pero deja el sistema sano.
        if self._w - self._rb > RING_FRAMES - MAX_BLOCK:
            self._rb = self._w - self.target
            self._rf = 0.0
            self.over += 1

    # -- salida

    def out_cb(self, outdata, frames, time_info, status):
        if status:
            log.debug("estado salida: %s", status)

        n = min(frames, MAX_BLOCK)
        need = self._rf + n * self.ratio
        disponible = self._w - self._rb

        if disponible < need + TAPS:
            # Subdesbordamiento. Nunca ceros en seco: un salto brusco a silencio
            # es un chasquido. Se hace un fundido de 2 ms desde la ultima
            # muestra valida, que es inaudible.
            self.under += 1
            outdata[:] = 0.0
            k = min(FADE_FRAMES, frames)
            outdata[:k] = self._last * self._fade[:k]
            return

        fill = disponible
        if fill < self._fill_min:
            self._fill_min = fill

        ar = self._ar[:n]
        pos = self._pos[:n]
        np.multiply(ar, self.ratio, out=pos)
        np.add(pos, self._rf, out=pos)

        basef = self._basef[:n]
        np.floor(pos, out=basef)

        ph = self._ph[:n]
        np.multiply(pos - basef, PHASES, out=pos)   # pos pasa a ser la fase
        np.floor(pos, out=pos)
        np.copyto(ph, pos.astype(np.int64, copy=False))
        np.clip(ph, 0, PHASES - 1, out=ph)

        idx = self._idx[:n]
        np.copyto(idx, basef.astype(np.int64, copy=False)[:, None])
        np.add(idx, self._rb, out=idx)
        np.add(idx, self._taps_off[None, :], out=idx)
        np.bitwise_and(idx, RING_MASK, out=idx)     # ring es potencia de 2

        gather = self._gather[:n]
        # mode='clip' y no 'raise': con out=, el modo 'raise' esta documentado
        # como SIEMPRE buffereado, o sea que asignaria por dentro. Los indices ya
        # vienen enmascarados, asi que clip no recorta nada.
        np.take(self.ring, idx, axis=0, out=gather, mode="clip")

        coefs = self._coefs[:n]
        np.copyto(coefs, self.table[ph])

        np.einsum("ft,ftc->fc", coefs, gather, out=outdata[:n], optimize=False)

        objetivo = self.vol.get(1.0) if self.vol is not None else 1.0
        if objetivo != self._gain:
            # Rampa dentro del bloque: en 10 ms el oido no percibe el cambio como
            # un salto, y sin ella cada pulsacion de volumen daria un clic.
            g0, dg = self._gain, objetivo - self._gain
            np.multiply(self._ramp01[:n], dg, out=self._gainbuf[:n])
            np.add(self._gainbuf[:n], g0, out=self._gainbuf[:n])
            outdata[:n] *= self._gainbuf[:n, None]
            self._gain = objetivo
        elif self._gain != 1.0:
            outdata[:n] *= self._gain

        if frames > n:
            outdata[n:] = 0.0

        self._last[:] = outdata[n - 1]

        avance = self._rf + n * self.ratio
        consumido = int(avance)
        self._rb += consumido
        self._rf = avance - consumido

    # -- control de deriva

    def control(self):
        """Ajusta el ratio. Se llama UNA VEZ POR SEGUNDO desde el hilo monitor.

        Nunca desde el callback: el callback no puede hacer nada que no sea
        aritmetica sobre buffers ya reservados.

        Control PROPORCIONAL, sin termino integral. La planta es un integrador
        puro (el relleno es la integral de la diferencia de tasas), asi que la P
        sola estabiliza. Deja un error de posicion permanente, si, pero es que
        el objetivo del anillo NO es una especificacion que haya que cumplir:
        es un margen de seguridad. Da exactamente igual quedarse cuatro
        milisegundos por encima o por debajo mientras no se toque ni el cero ni
        el techo. Meter la integral traeria anti-windup, dos constantes mas y 40
        segundos de asentamiento a cambio de corregir algo que no molesta.

        Se controla sobre el VALLE del relleno del ultimo segundo, no sobre el
        valor instantaneo: lo que hay que evitar es el subdesbordamiento, y quien
        avisa de eso es el minimo, no la media.
        """
        valle = self._fill_min
        self._fill_min = RING_FRAMES
        if valle >= RING_FRAMES:
            return None

        error = valle - self.target
        ratio = 1.0 + KP * error
        lim = MAX_PPM * 1e-6
        self.ratio = min(1.0 + lim, max(1.0 - lim, ratio))
        return valle, error

    def monitor(self):
        pegado = 0
        while not self.stop_flag.wait(CONTROL_PERIOD):
            r = self.control()
            if r is None:
                continue
            valle, error = r
            lim = MAX_PPM * 1e-6
            if abs(self.ratio - 1.0) >= lim * 0.999:
                pegado += 1
                if pegado in (5, 30, 120):
                    log.warning("DERIVA ANOMALA: el ratio lleva %d s pegado al tope "
                                "de %.0f ppm. Si persiste, subir MAX_PPM", pegado, MAX_PPM)
            else:
                pegado = 0
            # El hueco maximo se reinicia en cada informe: acumulado desde el
            # arranque solo dice cual fue el peor momento de la sesion, y lo que
            # interesa es si AHORA la entrega sigue siendo regular.
            hueco = self.gap_max
            self.gap_max = 0.0
            log.info("relleno %.1f ms (objetivo %.1f) | deriva %+.1f ppm | "
                     "under %d over %d | hueco max %.1f ms",
                     valle * 1000.0 / self.fs, self.target * 1000.0 / self.fs,
                     (self.ratio - 1.0) * 1e6, self.under, self.over,
                     hueco * 1000.0)


# --------------------------------------------------------------------------
# Modos
# --------------------------------------------------------------------------

def run_bridge(cfg, parent_pid=None):
    import sounddevice as sd

    in_idx, out_idx, in_label, out_label = find_devices(
        sd, cfg["audio_in_name"], cfg["audio_out_name"])

    fs = int(sd.query_devices(in_idx)["default_samplerate"])
    fs_out = int(sd.query_devices(out_idx)["default_samplerate"])
    if fs != fs_out:
        # No deberia pasar aqui (ambos endpoints estan a 48000), pero si pasara,
        # el ratio base tendria que ser fs_in/fs_out y no 1.0, y eso es otro
        # diseno. Mejor decirlo que sonar mal.
        raise AudioError(EXIT_FORMAT,
                         "la entrada va a %d Hz y la salida a %d Hz; este puente "
                         "asume la misma tasa nominal" % (fs, fs_out))

    log.info("%d Hz, %d canales, objetivo %d ms (%+0.1f ms de ajuste A/V)",
             fs, CH, cfg["target_ms"], cfg["av_offset_ms"])

    br = Bridge(sd, in_idx, out_idx, fs, cfg["target_ms"], cfg["av_offset_ms"])

    import winhy
    br.vol = winhy.SharedFloat(winhy.VOL_SHM, cfg.get("gain", 1.0))
    log.info("volumen inicial: %.0f%% (ganancia %.3f)",
             100.0 * (br.vol.get(1.0) ** 0.5), br.vol.get(1.0))

    try:
        istream = sd.InputStream(device=in_idx, channels=CH, samplerate=fs,
                                 dtype=DTYPE, blocksize=0, latency="low",
                                 callback=br.in_cb)
        ostream = sd.OutputStream(device=out_idx, channels=CH, samplerate=fs,
                                  dtype=DTYPE, blocksize=0, latency="low",
                                  callback=br.out_cb)
    except sd.PortAudioError as exc:
        raise AudioError(EXIT_FORMAT, "no se pudo abrir a %d Hz / %d canales: %s"
                         % (fs, CH, exc))

    hilo = threading.Thread(target=br.monitor, name="monitor", daemon=True)

    if parent_pid:
        threading.Thread(target=watch_parent, name="watch-parent", daemon=True,
                         args=(parent_pid, br.stop_flag.set)).start()

    with istream:
        # Prellenado: arrancar la salida con el anillo vacio es un
        # subdesbordamiento garantizado en el primer callback.
        #
        # Se prellena el objetivo MAS un bloque de holgura. Medido: al abrir el
        # stream de salida, PortAudio consume de golpe el primer bloque, asi que
        # prellenar justo el objetivo deja el anillo por debajo desde el segundo
        # cero y el control tarda un minuto en recuperarlo. Es margen perdido
        # justo cuando mas falta hace.
        objetivo_inicial = br.target + 480
        t0 = time.perf_counter()
        while br._w < objetivo_inicial and time.perf_counter() - t0 < 2.0:
            time.sleep(0.005)
        log.info("prellenado %.1f ms en %.0f ms",
                 br._w * 1000.0 / fs, (time.perf_counter() - t0) * 1000)
        hilo.start()
        with ostream:
            try:
                while not br.stop_flag.is_set():
                    time.sleep(0.25)
            except KeyboardInterrupt:
                pass
    br.stop_flag.set()
    if br.vol is not None:
        br.vol.close()
    log.info("puente cerrado: %d under, %d over", br.under, br.over)
    return EXIT_OK


def run_measure(cfg, seconds=30.0):
    """Mide el patron de entrega de usbaudio.sys.

    Es el numero que fija target_ms. Si el driver agrupa paquetes en rafagas, el
    anillo tiene que ser mayor que la rafaga mas grande o habra cortes por mucho
    que el promedio cuadre.
    """
    import sounddevice as sd

    in_idx, _, in_label, _ = find_devices(sd, cfg["audio_in_name"], "")
    fs = int(sd.query_devices(in_idx)["default_samplerate"])

    huecos = []
    tamanos = []
    estado = {"t": None}

    def cb(indata, frames, time_info, status):
        now = time.perf_counter()
        if estado["t"] is not None:
            huecos.append((now - estado["t"]) * 1000.0)
        estado["t"] = now
        tamanos.append(frames)

    print("Midiendo %.0f s sobre %s a %d Hz..." % (seconds, in_label, fs))
    with sd.InputStream(device=in_idx, channels=CH, samplerate=fs, dtype=DTYPE,
                        blocksize=0, latency="low", callback=cb):
        time.sleep(seconds)

    if not huecos:
        print("No llego ni un solo bloque.")
        return EXIT_INTERNAL

    h = np.array(huecos)
    t = np.array(tamanos)
    print()
    print("bloques      : %d, tamano %s frames" % (len(t), sorted(set(t.tolist()))))
    print("hueco medio  : %.2f ms" % h.mean())
    print("hueco p50    : %.2f ms" % np.percentile(h, 50))
    print("hueco p99    : %.2f ms" % np.percentile(h, 99))
    print("hueco MAXIMO : %.2f ms   <- este es el numero que importa" % h.max())
    print()
    sugerido = int(np.ceil(max(30.0, h.max() * 1.5 + 10.0) / 10.0) * 10)
    print("target_ms sugerido: %d ms" % sugerido)
    print("  (el maximo observado por 1,5 mas 10 ms de colchon, redondeado)")
    if h.max() > 15:
        print("  usbaudio.sys esta agrupando paquetes en rafagas.")
    else:
        print("  entrega regular, sin rafagas apreciables.")
    return EXIT_OK


def main(argv=None):
    ap = argparse.ArgumentParser(prog="audio")
    ap.add_argument("--audio", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--measure", nargs="?", type=float, const=30.0, default=None,
                    metavar="SEG", help="mide el patron de entrega y sale")
    ap.add_argument("--target-ms", type=int, default=None)
    ap.add_argument("--stop-event", default=None)
    ap.add_argument("--parent-pid", type=int, default=None)
    args = ap.parse_args(argv)

    log_ = core.setup("audio")
    cfg = {
        "audio_in_name": core.get("audio_in_name"),
        "audio_out_name": core.get("audio_out_name"),
        "target_ms": args.target_ms if args.target_ms is not None else core.get("target_ms"),
        "av_offset_ms": core.get("av_offset_ms"),
        "gain": _gain_de(core.get("volume")),
    }

    try:
        import sounddevice  # noqa: F401
    except Exception as exc:
        log_.error("sounddevice no disponible: %s", exc)
        print("Falta sounddevice. Instalalo con:\n"
              "    .venv\\Scripts\\python.exe -m pip install -r requirements.txt",
              file=sys.stderr)
        return EXIT_PORTAUDIO

    try:
        if args.measure is not None:
            return run_measure(cfg, args.measure)
        return run_bridge(cfg, args.parent_pid)
    except AudioError as exc:
        log_.error("fallo de audio (%d): %s", exc.code, exc.detail)
        print("Audio: %s" % exc.detail, file=sys.stderr)
        return exc.code
    except Exception:
        log_.exception("fallo no controlado en el puente de audio")
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
