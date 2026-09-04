"""El residente: vigila la red, abre el juego solo y vive en la bandeja.

Reparto de hilos, que aqui no es un detalle de estilo:

  hilo principal : maquina de estados, sondeo UDP, lanzamiento de hijos,
                   KeepAwake (que es POR HILO y por eso tiene que vivir aqui)
  hilo bandeja   : pystray, daemon
  cola           : unico canal de la bandeja hacia la maquina de estados

Este proceso NUNCA importa cv2 ni sounddevice. Abrir la capturadora es trabajo
del hijo, que es desechable: si se queda bloqueado dentro del driver, se le mata
y el demonio sigue vivo. Al reves seria imposible, porque TerminateProcess no
completa mientras un hilo este bloqueado en el driver.
"""

import logging
import os
import queue
import subprocess
import sys
import threading
import time
import uuid

import core
import ps5net
import winhy

log = logging.getLogger("daemon")

# Cadencias.
#
# Un sondeo cuesta un paquete UDP y 103 ms de respuesta medidos, o sea nada, y
# en cambio marca directamente lo que tarda la ventana en aparecer y en irse.
# Los valores originales (2 s en reposo, 3 s jugando) eran prudencia sin medir y
# ponian 4 s de retraso solo en detectar.
POLL_IDLE = 0.75
POLL_SLOW = 1.5             # tras un buen rato sin novedad, la mitad de ritmo
SLOW_AFTER = 600.0
POLL_PLAYING = 1.0

# Confirmaciones. La asimetria es deliberada: 620 es una afirmacion POSITIVA de
# la consola (estoy en reposo) y con dos basta; un timeout es ambiguo -un
# microcorte de red, el router reiniciandose- y exige mas.
ON_CONFIRMS = 2
OFF_CONFIRMS = 2
GONE_CONFIRMS = 3

# Margen para el handshake HDMI tras encenderse la consola.
#
# Era de 5 s a ojo. Ahora es de 1 s porque el margen de verdad no hace falta
# aqui: el reproductor YA valida la senal antes de crear la ventana y sale con
# codigo 5 si aun no hay imagen. Si abrimos demasiado pronto, el coste es un
# reintento rapido, no un fallo. Lo que si hace falta es que ese reintento
# temprano NO cuente como strike, o tres consolas encendidas seguidas
# desarmarian el automatismo.
ON_SETTLE = 1.0
WARMUP_S = 25.0             # ventana tras encenderse en la que FLAT no penaliza
WARMUP_RETRY = 2.0
REDISCOVER_EVERY = 30.0     # cada cuanto se rastrea por difusion por si cambio la IP
AVISO_S = 5.0               # cuanto dura un globo de notificacion antes de retirarse
# Cuanto tarda en darse por arrancado el reproductor. Era de 20 s, y ademas el
# demonio NO sondeaba si la consola se habia apagado mientras estaba en ABRIENDO:
# apagar la consola en esos 20 s no se notaba hasta que pasaban. Ahora se sondea
# siempre que hay un hijo vivo, y esto solo marca cuando se activa el KeepAwake.
PLAYER_START_BUDGET = 5.0

# Histeresis tras un cierre. Solo aplica cuando el reproductor se cierra SOLO
# (perdio la senal con la consola encendida), que es el caso que podria repetirse
# en bucle. Tras un cierre pedido por nosotros -la consola se apago- no pinta
# nada: para reabrir hace falta que la consola vuelva a estar encendida, y eso ya
# es histeresis de sobra. Aplicarlo ahi anadia 20 s muertos a CADA reapertura, y
# era el grueso de los 17 s que se tardaba en abrir.
REOPEN_GUARD = 5.0
REOPEN_AFTER_PARENT = 1.0
RETRY_BACKOFF = 30.0
DEVICE_POISONED_BACKOFF = 300.0
MANUAL_COOLDOWN = 1800.0    # media hora tras cerrar con ESC
FLAT_STRIKES = 3
FAST_EXIT_S = 2.0           # salir en menos de esto es "no arranca"

# Codigos de salida del reproductor (espejo de player.py)
EXIT_SIGNAL_LOST = 0
EXIT_INTERNAL = 1
EXIT_NO_SIGNAL = 2
EXIT_BUSY = 3
EXIT_ABSENT = 4
EXIT_FLAT = 5
EXIT_USER = 10
EXIT_PARENT = 20

NOMBRES = {0: "SIGNAL_LOST", 1: "INTERNAL", 2: "NO_SIGNAL", 3: "BUSY",
           4: "ABSENT", 5: "FLAT", 10: "USER", 20: "PARENT"}

EVENT_SHOW = "Local\\LanzadorPS5_SHOW_v1"


def hacer_icono(activo=True):
    """Icono de la bandeja, dibujado en memoria. Sin fichero .ico hasta la tanda 5."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = (60, 140, 220, 255) if activo else (120, 120, 120, 255)
    d.rounded_rectangle((6, 14, 58, 50), radius=10, fill=color)
    d.ellipse((16, 26, 28, 38), fill=(255, 255, 255, 255))
    d.rectangle((36, 26, 48, 38), fill=(255, 255, 255, 255))
    if not activo:
        d.line((10, 54, 54, 10), fill=(220, 70, 70, 255), width=6)
    return img


class Supervisor:
    def __init__(self):
        self.cmd = queue.Queue()
        self.auto = bool(core.get("auto_mode"))
        self.armed = False
        self.estado = "ESCUCHANDO"
        self.proc = None
        self.stop_event = None
        self.stop_name = None
        self.job = winhy.create_job()
        self.icon = None
        self._timer_aviso = None

        self.ip = core.get("ps5_ip") or None
        self.host_id = core.get("ps5_host_id") or None

        self._on_seen = 0
        self._off_seen = 0
        self._gone_seen = 0
        self._ready_desde = None
        self._siguiente_intento = 0.0
        self._flat_strikes = 0
        self._fast_exits = 0
        self._ultimo_cambio = time.monotonic()
        self._siguiente_rastreo = 0.0
        self._slept = winhy.slept_ms()
        self._salir = False

        # armed inicial. Este unico dato decide entre "no se abre nunca" y "se
        # abre siempre", asi que no puede quedar implicito: solo se arma si el
        # modo automatico esta activo Y no venimos de un cierre manual reciente.
        # suppressed_until se persiste, de modo que un ESC sobrevive a reiniciar
        # el PC; sin eso, apagar y encender el ordenador reabriria la ventana
        # que el usuario acababa de cerrar a proposito.
        supr = float(core.get("suppressed_until") or 0)
        if time.time() < supr:
            log.info("arranque en silencio: quedan %.0f min de la supresion manual",
                     (supr - time.time()) / 60.0)
        else:
            self.armed = self.auto

    # ---------------------------------------------------------------- bandeja

    def arrancar_bandeja(self):
        import pystray
        listo = threading.Event()

        # Las casillas leen TODAS de la configuracion, que es la unica fuente de
        # verdad. Antes "Modo automatico" leia una variable interna y "Abrir en
        # ventana" la configuracion: dos fuentes actualizadas en momentos
        # distintos, y las marcas se contradecian entre si.
        def item_auto(_):
            return bool(core.get("auto_mode"))

        def item_ventana(_):
            return not bool(core.get("start_fullscreen"))   # marcado = ventana

        def item_inicio(_):
            import autostart
            return autostart.is_enabled()

        # Los toggles se aplican AQUI MISMO, no por la cola.
        #
        # Son operaciones baratas -un bool y una escritura de configuracion- y
        # pasarlas por la cola significaba que el estado cambiaba hasta 0,2 s
        # despues de cerrarse el menu. Sumado a que no se refrescaba el menu, la
        # casilla mostraba siempre el valor anterior y parecia que se activaba la
        # otra opcion. update_menu() es lo que obliga a pystray a volver a
        # evaluar los `checked`: sin el, la marca se queda congelada.
        # Los tres avisan de lo que han hecho.
        #
        # No es un adorno: son interruptores que cambian algo INVISIBLE en ese
        # momento, y sin confirmacion el usuario vuelve a pulsarlos para
        # comprobar si funcionaron... deshaciendo lo que acababa de hacer. Paso
        # de verdad con el autoarranque: quedo activado a las 22:17:39 y
        # desactivado a las 22:17:42 por un segundo clic.
        def alternar_auto(icon, item):
            self.auto = not bool(core.get("auto_mode"))
            core.save_config(auto_mode=self.auto)
            if self.auto:
                self.armed = True
                core.save_config(suppressed_until=0.0)
            log.info("modo automatico: %s", "on" if self.auto else "off")
            self.avisar(
                "La ventana del juego se abrirá sola cuando enciendas la PS5."
                if self.auto else
                "La ventana del juego ya no se abrirá sola. Usa «Abrir ahora» "
                "cuando la quieras.")
            self.refrescar_icono()
            icon.update_menu()

        def alternar_ventana(icon, item):
            nuevo = not bool(core.get("start_fullscreen"))
            core.save_config(start_fullscreen=nuevo)
            # Afecta a la PROXIMA apertura: el modo se elige al crear la ventana.
            # Sobre una partida ya abierta manda F, que alterna en caliente sin
            # tocar esta preferencia.
            log.info("las proximas se abriran en %s",
                     "pantalla completa" if nuevo else "ventana")
            alternar_ventana_aviso(nuevo)
            self.refrescar_icono()
            icon.update_menu()

        def alternar_ventana_aviso(nuevo):
            self.avisar(
                "A partir de ahora la ventana del juego se abrirá a pantalla completa."
                if nuevo else
                "A partir de ahora la ventana del juego se abrirá en modo ventana.")

        def alternar_inicio(icon, item):
            import autostart
            if autostart.is_enabled():
                ok = autostart.disable()
                self.avisar(
                    "El Lanzador PS5 ya no se iniciará solo al encender el ordenador."
                    if ok else
                    "No se ha podido desactivar el inicio automático. "
                    "Mira «Ver registro» para saber por qué.")
            else:
                ok = autostart.enable()
                self.avisar(
                    "El Lanzador PS5 se iniciará solo cada vez que enciendas el "
                    "ordenador."
                    if ok else
                    "No se ha podido activar el inicio automático. "
                    "Mira «Ver registro» para saber por qué.")
            self.refrescar_icono()
            icon.update_menu()

        menu = pystray.Menu(
            # SIN default=True: en pystray el item por defecto se dispara con un
            # solo clic izquierdo, no con doble, y un roce abriria el juego.
            pystray.MenuItem("Abrir ahora", lambda i, x: self.cmd.put(("abrir", None))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Modo automatico", alternar_auto, checked=item_auto),
            pystray.MenuItem("Abrir en ventana", alternar_ventana, checked=item_ventana),
            pystray.MenuItem("Iniciar con Windows", alternar_inicio, checked=item_inicio),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Ver registro", lambda i, x: os.startfile(str(core.data_dir()))),
            pystray.MenuItem("Salir", lambda i, x: self.cmd.put(("salir", None))),
        )
        self.icon = pystray.Icon("LanzadorPS5", hacer_icono(self.auto),
                                 "Lanzador PS5", menu)

        def preparado(ic):
            ic.visible = True
            listo.set()

        def correr():
            # run(setup=...) y no run_detached(): run_detached crea el hilo como
            # NO daemon y colgaria la salida del programa. El Event cierra ademas
            # una carrera real: antes de que pystray marque el icono como listo,
            # icon.stop() es un no-op SILENCIOSO, y el icono se quedaria fantasma
            # en la bandeja al salir.
            self.icon.run(setup=preparado)

        threading.Thread(target=correr, name="tray", daemon=True).start()
        listo.wait(timeout=10.0)
        log.info("bandeja lista")

        # Sin esto, la ventana de pystray contesta que NO a WM_QUERYENDSESSION y
        # Windows saca la pantalla de "esta aplicacion impide el apagado" cada
        # vez que el usuario apaga el ordenador.
        hwnd = getattr(self.icon, "_hwnd", None)
        if hwnd:
            winhy.permitir_apagado(hwnd, self._al_apagar_windows)
        else:
            log.warning("no se encontro la ventana de la bandeja; podria bloquear "
                        "el apagado de Windows")

    def _al_apagar_windows(self):
        """Windows esta cerrando la sesion. Rapido y sin bloquear.

        Solo se senala el evento de parada al reproductor y se marca la salida:
        cualquier espera aqui retrasaria el apagado de todo el sistema. Si no da
        tiempo a mas, el Job Object se lleva a los hijos igualmente.
        """
        log.info("Windows esta cerrando la sesion")
        self._salir = True
        if self.stop_event:
            winhy.set_event(self.stop_event)

    ESTADOS = {"ESCUCHANDO": "esperando a la PS5",
               "ABRIENDO": "abriendo el juego",
               "REPRODUCIENDO": "jugando"}

    def refrescar_icono(self, motivo=""):
        """Icono y tooltip. El tooltip lleva el estado de los tres interruptores.

        Asi el usuario puede comprobar como esta todo pasando el raton por
        encima, sin depender de haber visto pasar un globo de notificacion.
        """
        if not self.icon:
            return
        try:
            import autostart
            self.icon.icon = hacer_icono(self.auto)
            lineas = ["Lanzador PS5 — %s" % self.ESTADOS.get(self.estado,
                                                             self.estado.lower())]
            if motivo:
                lineas[0] += " (%s)" % motivo
            lineas.append("Automático: %s · Ventana: %s · Con Windows: %s" % (
                "sí" if self.auto else "no",
                "no" if core.get("start_fullscreen") else "sí",
                "sí" if autostart.is_enabled() else "no"))
            self.icon.title = "\n".join(lineas)
        except Exception:
            log.debug("no se pudo refrescar el icono", exc_info=True)

    def avisar(self, texto):
        """Aviso en la bandeja, que se retira solo a los AVISO_S segundos.

        notify() no admite duracion: Windows decide, y en la practica el globo se
        queda un buen rato y luego se acumula en el centro de notificaciones.
        Como pystray si expone remove_notification(), la quitamos nosotros con un
        temporizador. Se cancela el anterior en cada aviso para que dos cambios
        seguidos no dejen la primera notificacion colgada.

        El globo va SIN titulo propio: Windows ya pone arriba el nombre del
        EJECUTABLE ("Lanzador PS5" con el .exe, "Python" bajo pythonw), asi que
        pasar ademas szInfoTitle sacaba el nombre dos veces. Medido con los dos
        globos seguidos: sin titulo se muestra exactamente igual.
        """
        log.info("AVISO: %s", texto)
        try:
            if not (self.icon and self.icon.HAS_NOTIFICATION):
                return
            if self._timer_aviso is not None:
                self._timer_aviso.cancel()
            self._notificar(texto)
            self._timer_aviso = threading.Timer(AVISO_S, self._quitar_aviso)
            self._timer_aviso.daemon = True
            self._timer_aviso.start()
        except Exception:
            log.debug("no se pudo mostrar el aviso", exc_info=True)

    def _notificar(self, texto):
        """Manda el globo con szInfo y szInfoTitle vacio.

        icon.notify() no admite un titulo vacio: hace `title or self.title or
        ''` y self.title es nuestro tooltip multilinea, que quedaria aun peor.
        Por eso se envia el NIF_INFO a mano. Si esa API interna de pystray
        cambiara, se vuelve al notify de siempre: mejor un titulo repetido que
        quedarse sin aviso.
        """
        try:
            from pystray._util import win32
            self.icon._message(win32.NIM_MODIFY, win32.NIF_INFO,
                               szInfo=texto, szInfoTitle="")
        except Exception:
            log.debug("globo sin titulo fallo, se usa notify()", exc_info=True)
            self.icon.notify(texto, "Lanzador PS5")

    def _quitar_aviso(self):
        try:
            if self.icon:
                self.icon.remove_notification()
        except Exception:
            pass

    # ------------------------------------------------------------------ hijos

    def lanzar(self):
        self.stop_name = "Local\\PS5L_STOP_" + uuid.uuid4().hex
        self.stop_event = winhy.create_event(self.stop_name)
        argv = core.child_argv("player",
                               stop_event=self.stop_name,
                               parent_pid=os.getpid(),
                               cam_index=core.get("cam_index"),
                               monitor=core.get("monitor"))
        flags = subprocess.CREATE_NO_WINDOW | subprocess.ABOVE_NORMAL_PRIORITY_CLASS
        log.info("lanzando reproductor: %s", " ".join(argv))
        self.proc = subprocess.Popen(argv, creationflags=flags, cwd=str(core.app_dir()))
        if self.job:
            winhy.assign_to_job(self.job, int(self.proc._handle))
        self.estado = "ABRIENDO"
        self._ultimo_cambio = time.monotonic()
        self.refrescar_icono()

    def parar(self, motivo="peticion"):
        """Cierre en escalera: pedir, esperar, matar, y rendirse con dignidad."""
        if self.proc is None:
            return
        log.info("cerrando el reproductor (%s)", motivo)
        if self.stop_event:
            winhy.set_event(self.stop_event)
        try:
            self.proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            log.warning("no responde al evento; se termina")
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3.0)
            except Exception:
                pass
        if self.proc.poll() is None:
            # TerminateProcess no completa si un hilo esta bloqueado dentro del
            # driver. El Job Object se lo llevara cuando muera el demonio; hasta
            # entonces el dispositivo esta tomado y no tiene sentido reintentar.
            log.error("el reproductor sigue vivo: DISPOSITIVO ENVENENADO")
            self._siguiente_intento = time.monotonic() + DEVICE_POISONED_BACKOFF
            self.avisar("No se ha podido cerrar la ventana del juego y la capturadora "
                        "sigue ocupada. Se volverá a intentar dentro de 5 minutos.")
        self._limpiar_hijo()

    def _limpiar_hijo(self):
        winhy.close(self.stop_event)
        self.stop_event = None
        self.stop_name = None
        self.proc = None
        winhy.keep_awake(False)
        self.estado = "ESCUCHANDO"
        self._ultimo_cambio = time.monotonic()
        self.refrescar_icono()

    def desarmar(self, motivo):
        """Apaga el automatismo solo y lo dice.

        Sin esto, con la consola encendida y la imagen en negro -jugando en la
        tele por el loop-out, o con HDCP- el ciclo seria: abrir, fallar, volver a
        escuchar, la consola sigue diciendo 200, abrir otra vez... una ventana a
        pantalla completa cada medio minuto durante toda la partida.
        """
        self.auto = False
        core.save_config(auto_mode=False)
        self.armed = False
        log.warning("automatismo DESARMADO: %s", motivo)
        self.avisar(motivo)
        self.refrescar_icono("pausado")

    def tratar_salida(self, code, duracion):
        nombre = NOMBRES.get(code, "DESCONOCIDO(%d)" % code)
        log.info("el reproductor termino con %d (%s) tras %.1f s", code, nombre, duracion)
        ahora = time.monotonic()

        if duracion < FAST_EXIT_S and code not in (EXIT_USER, EXIT_PARENT):
            self._fast_exits += 1
            if self._fast_exits >= 3:
                self.desarmar("Modo automático desactivado: la ventana del juego no "
                              "consigue abrirse. Puede estar bloqueándola el antivirus.")
                self._fast_exits = 0
                return
        else:
            self._fast_exits = 0

        if code == EXIT_USER:
            hasta = time.time() + MANUAL_COOLDOWN
            core.save_config(suppressed_until=hasta, last_exit_code=code,
                             last_exit_ts=time.time())
            self.armed = False
            log.info("cerrado a mano: no se reabrira solo durante %.0f min",
                     MANUAL_COOLDOWN / 60)
            return

        core.save_config(last_exit_code=code, last_exit_ts=time.time())

        if code == EXIT_FLAT:
            # Recien encendida: la consola aun esta negociando el HDMI y todavia
            # no hay imagen. Es lo esperado, no un fallo, asi que se reintenta
            # enseguida y NO cuenta como strike; si contara, tres encendidos
            # seguidos desarmarian el automatismo sin que pase nada malo.
            if self._ready_desde and ahora - self._ready_desde < WARMUP_S:
                log.info("aun sin imagen (%.1f s desde el encendido); se reintenta",
                         ahora - self._ready_desde)
                self._siguiente_intento = ahora + WARMUP_RETRY
                return
            self._flat_strikes += 1
            if self._flat_strikes >= FLAT_STRIKES:
                self.desarmar("Modo automático desactivado: la imagen llega en negro. "
                              "En la PS5, desactiva el HDCP y pon la salida en 1080p SDR.")
                self._flat_strikes = 0
            else:
                self._siguiente_intento = ahora + RETRY_BACKOFF
        elif code == EXIT_BUSY:
            self._siguiente_intento = ahora + RETRY_BACKOFF
            self.avisar("Otro programa está usando la capturadora (¿OBS?). "
                        "Ciérralo para poder abrir el juego.")
        elif code == EXIT_ABSENT:
            # No entra en el backoff normal: reintentar cada 30 s con el aparato
            # fisicamente desenchufado no lleva a ninguna parte.
            self._siguiente_intento = ahora + DEVICE_POISONED_BACKOFF
            self.avisar("No se encuentra la capturadora. Comprueba que el cable USB "
                        "esté bien conectado.")
        elif code == EXIT_PARENT:
            # Lo cerramos nosotros, casi siempre porque la consola se apago. No
            # hay nada de lo que protegerse: reabrir exige verla encendida otra vez.
            self._flat_strikes = 0
            self._siguiente_intento = ahora + REOPEN_AFTER_PARENT
        elif code == EXIT_SIGNAL_LOST:
            # Se cerro solo. Aqui si conviene un respiro para no entrar en bucle.
            self._flat_strikes = 0
            self._siguiente_intento = ahora + REOPEN_GUARD
        else:
            self._siguiente_intento = ahora + RETRY_BACKOFF

    # ------------------------------------------------------------ estado real

    def sondear(self):
        # El rastreo por difusion solo de tarde en tarde: cuesta un timeout
        # entero por interfaz y la IP de la consola casi nunca cambia. Hacerlo
        # en cada vuelta multiplicaba por siete el coste del sondeo rutinario.
        ahora = time.monotonic()
        rediscover = ahora >= self._siguiente_rastreo
        if rediscover:
            self._siguiente_rastreo = ahora + REDISCOVER_EVERY
        estado, info = ps5net.status(self.ip, self.host_id, rediscover=rediscover)
        if info:
            nueva_ip = info.get("ip")
            nuevo_id = info.get("host-id")
            if nueva_ip and nueva_ip != self.ip:
                self.ip = nueva_ip
                core.save_config(ps5_ip=nueva_ip)
            if nuevo_id and nuevo_id != self.host_id:
                self.host_id = nuevo_id
                core.save_config(ps5_host_id=nuevo_id)
        return estado

    def tick_escuchando(self, ahora):
        estado = self.sondear()

        if estado == ps5net.READY:
            self._on_seen += 1
            self._off_seen = self._gone_seen = 0
            if self._ready_desde is None:
                self._ready_desde = ahora
        else:
            self._on_seen = 0
            self._ready_desde = None
            if estado == ps5net.STANDBY:
                self._off_seen += 1
                self._gone_seen = 0
            else:
                self._gone_seen += 1
                self._off_seen = 0

        # Volver a armar cuando la consola deja de estar encendida: asi un cierre
        # con ESC no impide que se abra sola la PROXIMA vez que se encienda.
        if not self.armed and self.auto:
            if self._off_seen >= OFF_CONFIRMS or self._gone_seen >= GONE_CONFIRMS:
                supr = float(core.get("suppressed_until") or 0)
                if time.time() >= supr:
                    log.info("la consola ya no esta encendida: se rearma")
                    self.armed = True

        if not (self.auto and self.armed):
            return
        if self._on_seen < ON_CONFIRMS:
            return
        if self._ready_desde and ahora - self._ready_desde < ON_SETTLE:
            return          # margen para que termine el handshake HDMI
        if ahora < self._siguiente_intento:
            return
        self.lanzar()

    def tick_reproduciendo(self, ahora):
        estado = self.sondear()
        if estado == ps5net.STANDBY:
            self._off_seen += 1
            self._gone_seen = 0
        elif estado == ps5net.GONE:
            self._gone_seen += 1
            self._off_seen = 0
        else:
            self._off_seen = self._gone_seen = 0

        if self._off_seen >= OFF_CONFIRMS or self._gone_seen >= GONE_CONFIRMS:
            log.info("la consola se ha apagado (%s)", estado)
            self.parar("consola apagada")
            self._siguiente_intento = ahora + REOPEN_GUARD

    # -------------------------------------------------------------- comandos

    def atender_comandos(self):
        while True:
            try:
                orden, dato = self.cmd.get_nowait()
            except queue.Empty:
                return
            if orden == "abrir":
                core.save_config(suppressed_until=0.0)
                self._flat_strikes = 0
                self._siguiente_intento = 0.0
                self.armed = True
                if self.proc is not None:
                    continue
                # Comprobar la consola ANTES de lanzar. Sin esto se abria un
                # proceso condenado que tardaba dos segundos en morir y se
                # cerraba sin explicar nada: el usuario solo veia que "Abrir
                # ahora" no hacia nada. El sondeo cuesta 0,4 s.
                estado = self.sondear()
                if estado == ps5net.READY:
                    self.lanzar()
                elif estado == ps5net.STANDBY:
                    self.avisar("La PS5 está en reposo. Enciéndela y la ventana del "
                                "juego se abrirá sola.")
                else:
                    self.avisar("No se encuentra la PS5 en la red. Comprueba que esté "
                                "encendida y conectada.")
            elif orden == "salir":
                self._salir = True

    def revisar_suspension(self):
        """Si el PC ha estado suspendido, la sesion de captura esta muerta.

        Tras reanudar, el grafo DirectShow queda inservible y retrieve()
        devolveria False para siempre, asi que mas vale cerrar y reabrir.
        """
        ahora = winhy.slept_ms()
        if ahora - self._slept > 2000:
            dormido = (ahora - self._slept) / 1000.0
            self._slept = ahora
            log.warning("el PC ha estado suspendido %.0f s", dormido)
            if self.proc is not None:
                self.parar("reanudacion tras suspender")
            self._siguiente_intento = time.monotonic() + 10.0   # RESUME_GRACE
            return True
        self._slept = ahora
        return False

    # ------------------------------------------------------------------ bucle

    def run(self):
        import autostart
        autostart.heal()

        if not self.ip:
            log.info("primera ejecucion: buscando la consola en la red")
            for info in ps5net.discover():
                self.ip = info["ip"]
                self.host_id = info.get("host-id")
                core.save_config(ps5_ip=self.ip, ps5_host_id=self.host_id or "")
                log.info("consola encontrada: %s en %s", info.get("host-name"), self.ip)
                break
            else:
                log.info("no se ha encontrado ninguna PS5; se seguira buscando")

        self.arrancar_bandeja()
        log.info("en marcha. auto=%s armed=%s ip=%s", self.auto, self.armed, self.ip)

        hilo_show = threading.Thread(target=self._esperar_show, name="show", daemon=True)
        hilo_show.start()

        siguiente = 0.0
        try:
            while not self._salir:
                self.atender_comandos()
                self.revisar_suspension()
                ahora = time.monotonic()

                if self.proc is not None:
                    code = self.proc.poll()
                    if code is not None:
                        dur = ahora - self._ultimo_cambio
                        self._limpiar_hijo()
                        self.tratar_salida(code, dur)
                    elif self.estado == "ABRIENDO" and \
                            ahora - self._ultimo_cambio > PLAYER_START_BUDGET:
                        log.info("el reproductor lleva %.0f s vivo: en marcha",
                                 PLAYER_START_BUDGET)
                        self.estado = "REPRODUCIENDO"
                        winhy.keep_awake(True)
                        self.refrescar_icono()

                if ahora >= siguiente:
                    if self.proc is not None:
                        # Se sondea SIEMPRE que hay un hijo vivo, tambien durante
                        # ABRIENDO: si la consola se apaga en los primeros
                        # segundos hay que enterarse igual.
                        self.tick_reproduciendo(ahora)
                        siguiente = ahora + POLL_PLAYING
                    else:
                        self.tick_escuchando(ahora)
                        ocioso = ahora - self._ultimo_cambio
                        siguiente = ahora + (POLL_SLOW if ocioso > SLOW_AFTER
                                             else POLL_IDLE)

                time.sleep(0.2)
        finally:
            log.info("cerrando")
            if self._timer_aviso is not None:
                self._timer_aviso.cancel()
            self.parar("salida del demonio")
            winhy.keep_awake(False)
            if self.icon:
                # Sin esto queda un icono fantasma en la bandeja hasta que el
                # usuario pasa el raton por encima.
                try:
                    self.icon.stop()
                except Exception:
                    pass
            winhy.close(self.job)

    def _esperar_show(self):
        h = winhy.create_event(EVENT_SHOW)
        try:
            while not self._salir:
                if winhy.wait(h, 500):
                    log.info("otra instancia pide abrir la ventana")
                    self.cmd.put(("abrir", None))
                    winhy.close(h)
                    h = winhy.create_event(EVENT_SHOW)
        finally:
            winhy.close(h)


def main(argv=None):
    log_ = core.setup("tray")

    if not winhy.single_instance():
        log_.info("ya hay una instancia; se le pide que abra y se sale")
        h = winhy.open_event(EVENT_SHOW, winhy.EVENT_MODIFY_STATE)
        if h:
            winhy.set_event(h)
            winhy.close(h)
        return 0

    winhy.set_priority(winhy.NORMAL_PRIORITY_CLASS)
    try:
        Supervisor().run()
    except KeyboardInterrupt:
        pass
    except Exception:
        log_.exception("fallo no controlado en el demonio")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
