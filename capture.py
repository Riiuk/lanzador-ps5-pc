"""Apertura verificada de la capturadora y hilo de captura.

Dos reglas gobiernan este modulo y explican casi todo lo raro que hay aqui:

1. La medida es el arbitro, no la teoria. DirectShow negocia el formato y miente
   sobre el resultado, asi que no se cree ni al backend ni a la documentacion:
   se abre, se cuentan fotogramas reales durante dos segundos y se mira el shape.

2. open / grab / retrieve / release viven TODOS en el mismo hilo. El backend
   VideoCapture_DShow llama a CoInitialize y CoUninitialize en el hilo que
   ejecuta el constructor y el destructor; repartirlos entre hilos deja el
   apartamento COM inconsistente.
"""

import logging
import threading
import time

import cv2
import numpy as np

log = logging.getLogger("capture")

W, H = 1920, 1080

# Validacion de apertura.
#
# 0,7 s y no 2,0: a 60 fps son mas de 40 fotogramas, de sobra para medir la tasa,
# comprobar la resolucion y ver si la imagen es negra. Los 2 s originales eran
# prudencia sin medir, y se notaban enteros en el tiempo de apertura.
VALIDATE_S = 0.7        # ventana para contar fps reales
NEED_FPS = 55.0         # por debajo de esto la negociacion salio mal
OPEN_BUDGET_S = 6.0     # presupuesto total antes de rendirse

# Deteccion de perdida de senal.
#
# MEDIDO con la PS5 apagada, sobre la capturadora de verdad (una medida anterior
# resulto ser del cartel "Please run iVCam" de la camara virtual, porque el
# indice de OpenCV no era el que se creia; por eso ahora el indice se resuelve
# probando, ver evaluate_index()).
#
# La HYSD-88 sin senal HDMI:
#   - NO deja de entregar: 60 de 60 fotogramas a 57,8 fps, cero fallos
#   - NO baja de resolucion: se queda en 1920x1080
#   - entrega NEGRO PURO: std 0,00 exacto, movimiento 0,000
#
# Conclusion: el unico detector que dispara aqui es LA IMAGEN PLANA, y lo hace
# con un margen comodisimo (0,00 frente a un umbral de 0,5). Ni el cambio de
# resolucion ni retrieve()==False sirven para esta tarjeta.
RETRIEVE_FAIL_MAX = 3   # red de seguridad: cada fallo bloquea ~1000 ms en el driver
NO_FRAME_MAX = 3.5      # red generica de tiempo, por si grab() se queda colgado

# Reconexion.
#
# LECCION APRENDIDA EN CALIENTE: al arrancar un juego, la PS5 renegocia el HDMI
# y la senal se cae unos segundos. Es un apagon TRANSITORIO y perfectamente
# normal. La primera version daba la senal por perdida a los 4 s de negro y
# cerraba el programa, con lo que era imposible entrar en un juego. OBS no hace
# eso: simplemente espera a que vuelva.
#
# Ante una perdida de senal, entonces, NO se cierra: se avisa en pantalla, se
# sigue leyendo y se reconecta. Solo se abandona si no vuelve en SIGNAL_GIVEUP,
# y aun asi la decision de que hacer es del supervisor, no de aqui.
# 10 s, y no menos, por un fallo de CONCEPTO que costo entender: "sin senal" se
# mide mirando si la imagen es uniforme, y una pantalla de carga negra del propio
# juego es tan uniforme como un cable desenchufado. Mirando solo la imagen, un
# fundido a negro de un juego y una senal perdida son literalmente el mismo dato.
# Medido en una partida real de Gran Turismo 7: los negros de menus y cargas
# duran entre 3,0 y 4,5 s. Con 10 s no los dispara ninguno, y una perdida de
# verdad -que no vuelve hasta que se arregla- se sigue viendo enseguida.
FLAT_NOTICE = 10.0       # tras esto se avisa en pantalla, pero no se cierra nada
SIGNAL_GIVEUP = 90.0     # sin imagen valida durante esto, se rinde
REOPEN_DELAY = 1.0       # espera entre intentos de reabrir

# Imagen plana: el detector de que no llega imagen.
#
# El umbral es 0,5 porque lo medido es 0,00 exacto: no hace falta acercarse mas
# y asi un fundido a negro de un juego, que nunca es uniforme del todo, tiene
# holgura. Los 4 segundos evitan que un fundido largo cierre la partida, y aun
# asi el cierre entra dentro de los 10 s objetivo.
#
# OJO: imagen plana NO significa por si sola "consola apagada". Tambien sale
# negro con HDCP activo, 2160p o HDR, que es un problema distinto y tiene
# arreglo. Quien distingue los dos casos es el supervisor (tanda 3) preguntando
# a la red: si la consola responde 200 y la imagen es negra, es HDMI mal
# configurado y hay que avisar; si no responde, es que se ha apagado. Por eso
# esto sale por STOP_FLAT y no por STOP_SIGNAL_LOST: la decision no es de aqui.
FLAT_STD = 0.5
FLAT_SUSTAIN = 4.0
FLAT_PERIOD = 0.5       # la varianza se muestrea, no se calcula por fotograma

# Motivos de parada. player.py los traduce a codigos de salida.
STOP_SIGNAL_LOST = "signal_lost"   # -> 0
STOP_NO_SIGNAL = "no_signal"       # -> 2
STOP_BUSY = "busy"                 # -> 3
STOP_ABSENT = "absent"             # -> 4
STOP_FLAT = "flat"                 # -> 5


def _fourcc(a, b, c, d):
    # VideoWriter_fourcc no aparece en los stubs de las versiones modernas.
    try:
        return int(cv2.VideoWriter.fourcc(a, b, c, d))
    except AttributeError:
        return int(cv2.VideoWriter_fourcc(a, b, c, d))


FOURCC_MJPG = _fourcc("M", "J", "P", "G")
FOURCC_YUY2 = _fourcc("Y", "U", "Y", "2")


def tune_opencv():
    """El decode lo hace DirectShow; OpenCV solo mueve buffers."""
    cv2.setNumThreads(1)
    try:
        cv2.ocl.setUseOpenCL(False)
    except Exception:
        pass


def _measure(cap, seconds=VALIDATE_S):
    """Cuenta fotogramas REALES. Nunca cap.read().

    read() es `if (grab()) retrieve(image); return !image.empty();`, o sea que
    ignora el booleano de retrieve(); y retrieveFrame() hace frame.create()
    ANTES de que getPixels() pueda fallar. Sin HDMI eso devuelve
    (True, memoria sin inicializar) y ningun watchdog se enteraria.
    """
    n = 0
    shape = None
    peak_std = 0.0
    next_std = 0.0
    t0 = time.perf_counter()
    while True:
        now = time.perf_counter()
        if now - t0 >= seconds:
            break
        if not cap.grab():
            break
        ok, frame = cap.retrieve()
        if not ok or frame is None:
            break
        n += 1
        shape = frame.shape
        # Se muestrea la varianza aqui mismo para poder rechazar una imagen
        # negra ANTES de crear la ventana: con la consola apagada, abrir un
        # rectangulo negro a pantalla completa durante unos segundos es
        # exactamente lo que no debe pasar.
        if now >= next_std:
            next_std = now + 0.2
            peak_std = max(peak_std, float(frame[::16, ::16, 1].std()))
    dt = time.perf_counter() - t0
    return (n / dt if dt > 0 else 0.0), shape, peak_std


def _open_fast(index, fourcc):
    """Una sola negociacion, en el constructor. Es la ruta rapida y limpia."""
    return cv2.VideoCapture(index, cv2.CAP_DSHOW, [
        cv2.CAP_PROP_FRAME_WIDTH, W,
        cv2.CAP_PROP_FRAME_HEIGHT, H,
        cv2.CAP_PROP_FOURCC, fourcc,
    ])


def _open_strict(index, fourcc):
    """Orden FPS -> W -> H -> FOURCC, el unico que llama a setIdealFramerate.

    Dos trampas que este orden esquiva:
      - set(CAP_PROP_FPS) DESPUES del constructor con params cae en
        setupDevice(m_index) sin ancho ni alto (m_widthSet vale -1) y tira por
        el desague la resolucion y el FOURCC ya negociados.
      - FOURCC antes que W/H no sirve: setProperty(FOURCC) resetea m_fourcc a -1
        al salir, y el set(HEIGHT) posterior renegocia con el subtipo por
        defecto, perdiendo el MJPG en silencio.
    """
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        return cap
    cap.set(cv2.CAP_PROP_FPS, 60)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, H)
    cap.set(cv2.CAP_PROP_FOURCC, fourcc)
    return cap


def strategies(prefer="yuy2"):
    """Formatos a probar, en orden.

    YUY2 va PRIMERO, y no es un detalle menor: MJPG es comprimido con perdida y
    en los degradados oscuros se ve el ruido de bloque a simple vista (en un
    monitor decente, un fondo casi negro sale granulado mientras que el negro
    puro sale limpio: la firma clasica de la DCT). YUY2 es sin comprimir.
    Cuesta 1920*1080*2*60 = 249 MB/s, que cabe de sobra en el SuperSpeed que ya
    esta confirmado, asi que no hay ninguna razon para comprimir.
    """
    yuy2 = (("fast/YUY2", _open_fast, FOURCC_YUY2),
            ("strict/YUY2", _open_strict, FOURCC_YUY2))
    mjpg = (("fast/MJPG", _open_fast, FOURCC_MJPG),
            ("strict/MJPG", _open_strict, FOURCC_MJPG))
    if prefer == "mjpg":
        return mjpg + yuy2
    if prefer == "yuy2":
        return yuy2 + mjpg
    return yuy2 + mjpg


def open_verified(index, prefer="yuy2", require_signal=True):
    """Devuelve (cap, info) o lanza CaptureError.

    Lo que se valida es 1080p, >=55 fps y que la imagen no sea negra; el formato
    NO se comprueba leyendolo, porque get(CAP_PROP_FOURCC) devuelve la variable
    cacheada VD->videoType (inicial RGB24), que no se actualiza en la rama
    "closest": miente. La unica verdad es lo que llega en frame.shape y los fps
    contados.
    """
    t0 = time.perf_counter()
    last = None

    for name, opener, fourcc in strategies(prefer):
        if time.perf_counter() - t0 > OPEN_BUDGET_S:
            break

        cap = opener(index, fourcc)
        if not cap.isOpened():
            cap.release()
            last = "no se pudo abrir el indice %d" % index
            log.warning("%s: isOpened() False", name)
            continue

        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        fps, shape, std = _measure(cap)
        log.info("%s -> shape=%s fps=%.1f std=%.2f", name, shape, fps, std)

        if shape is None:
            cap.release()
            last = "el dispositivo abre pero no entrega fotogramas"
            continue
        if (shape[1], shape[0]) != (W, H):
            cap.release()
            last = "resolucion %dx%d en vez de %dx%d" % (shape[1], shape[0], W, H)
            continue
        if fps < NEED_FPS:
            cap.release()
            last = "solo %.1f fps" % fps
            continue
        if std < FLAT_STD and require_signal:
            # Negro en la PRIMERA apertura: se rechaza aqui, sin llegar a crear
            # la ventana, porque lo mas probable es que la consola este apagada
            # y abrir un rectangulo negro a pantalla completa no ayuda a nadie.
            #
            # En una RECONEXION, en cambio, require_signal viene a False: ahi el
            # negro es lo esperado (la consola esta renegociando el HDMI) y hay
            # que abrir igualmente para poder ver cuando vuelve la imagen.
            cap.release()
            raise CaptureError(STOP_FLAT,
                               "imagen completamente negra (std %.2f): la consola esta "
                               "apagada, o hay HDCP / HDR / 2160p en la salida HDMI" % std)

        return cap, {"ruta": name, "shape": shape, "fps": round(fps, 1), "std": round(std, 2)}

    raise CaptureError(STOP_NO_SIGNAL, last or "sin senal")


class CaptureError(Exception):
    def __init__(self, reason, detail=""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------
# Resolucion del indice
# --------------------------------------------------------------------------
#
# OpenCV no expone los nombres de los dispositivos DirectShow, solo indices, y
# el orden NO es estable: en esta maquina la capturadora ha aparecido como
# indice 1 y como indice 0 en la misma sesion, porque conviven con ella dos
# camaras virtuales (e2eSoft iVCam y OBS Virtual Camera). Agarrar el indice
# equivocado no da error: da la imagen de otra cosa. Aqui paso media hora
# midiendo el cartel "Please run iVCam" creyendo que era la capturadora sin
# senal.
#
# La regla es no fiarse del numero: se prueba y se elige el que ENTREGA
# 1920x1080 de verdad, y el resultado se guarda en config.json para que el
# siguiente arranque sea inmediato.

SCAN_MAX = 6
SCAN_SECONDS = 1.0
MOVEMENT_FRAMES = 12
MOVEMENT_MIN = 0.05


def evaluate_index(idx, seconds=SCAN_SECONDS):
    """Devuelve un dict con lo que entrega ese indice, o None si no sirve."""
    try:
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW, [
            cv2.CAP_PROP_FRAME_WIDTH, W,
            cv2.CAP_PROP_FRAME_HEIGHT, H,
        ])
    except Exception:
        return None
    if not cap.isOpened():
        cap.release()
        return None

    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        frames = []
        n = 0
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < seconds:
            if not cap.grab():
                break
            ok, f = cap.retrieve()
            if not ok or f is None:
                break
            n += 1
            if len(frames) < MOVEMENT_FRAMES:
                frames.append(f)
        dt = time.perf_counter() - t0
        if not frames:
            return None

        shape = frames[-1].shape
        if (shape[1], shape[0]) != (W, H):
            return None

        # Cuanto cambia la imagen entre fotogramas. Solo se usa para desempatar
        # si mas de un indice entrega 1080p: el marcador de una camara virtual
        # sin usar es identico byte a byte, una fuente real nunca lo es.
        movement = 0.0
        for i in range(1, len(frames)):
            d = float(np.abs(frames[i].astype(np.int16) -
                             frames[i - 1].astype(np.int16)).mean())
            movement = max(movement, d)

        return {"index": idx, "shape": shape, "fps": round(n / dt if dt else 0.0, 1),
                "movement": round(movement, 3)}
    finally:
        cap.release()


def resolve_index(preferred=None):
    """Indice de la capturadora. Prueba el preferido y si falla escanea.

    Devuelve (indice, info) o lanza CaptureError. El que llama deberia guardar
    el indice devuelto en config.json cuando cambie.
    """
    if preferred is not None:
        info = evaluate_index(preferred)
        if info and info["fps"] >= NEED_FPS:
            log.info("indice %d validado: %s", preferred, info)
            return preferred, info
        log.warning("el indice %s de la configuracion no entrega 1080p60, se escanea",
                    preferred)

    candidates = []
    for idx in range(SCAN_MAX):
        if idx == preferred:
            continue
        info = evaluate_index(idx)
        if info:
            log.info("candidato: %s", info)
            if info["fps"] >= NEED_FPS:
                candidates.append(info)
        else:
            log.debug("indice %d descartado", idx)

    if not candidates:
        raise CaptureError(STOP_NO_SIGNAL,
                           "ningun dispositivo entrega 1920x1080; con la consola "
                           "encendida revisa el cable HDMI, y que la salida sea 1080p SDR")

    if len(candidates) > 1:
        moving = [c for c in candidates if c["movement"] > MOVEMENT_MIN]
        if moving:
            candidates = moving
        log.info("varios candidatos, se elige por movimiento")

    best = max(candidates, key=lambda c: (c["movement"], c["fps"]))
    log.info("indice elegido: %d (%s)", best["index"], best)
    return best["index"], best


class CaptureThread(threading.Thread):
    """Abre el dispositivo y publica siempre el ULTIMO fotograma.

    Los atrasados se descartan: en un lanzador de juego un fotograma viejo no
    vale nada, y encolarlos solo anade latencia. El hilo principal se entera por
    un Event, asi que no gira en vacio quemando CPU con una fuente de 60 Hz.
    """

    def __init__(self, index, prefer="yuy2", device_path=""):
        super().__init__(name="capture", daemon=True)
        self.index = index
        self.prefer = prefer
        self.device_path = device_path or ""
        self.frame = None            # ultimo fotograma publicado (BGR, solo lectura)
        self.info = None
        self.new_frame = threading.Event()
        self.ready = threading.Event()
        self.stop_reason = None
        self.error = None
        self.read_ms = 0.0           # media movil de grab()+retrieve()
        self.index_changed = False   # el que llama debe persistir self.index
        self.waiting = False         # True mientras se espera a que vuelva la senal
        self.sin_senal_desde = None
        self._lock = threading.Lock()
        # OJO: no llamar a esto _stop. threading.Thread usa Thread._stop()
        # internamente desde join(), y un atributo con ese nombre lo pisa: el
        # join revienta con "'Event' object is not callable".
        self._stopping = threading.Event()
        self._frames = 0

    @property
    def frames(self):
        return self._frames

    def latest(self):
        with self._lock:
            return self.frame

    def stop(self):
        self._stopping.set()

    def run(self):
        """Abre, lee y RECONECTA. Solo se rinde si la senal no vuelve.

        La estructura es un ciclo externo de reconexion alrededor del bucle de
        lectura, porque perder la senal es un evento normal (arrancar un juego
        renegocia el HDMI) y no una razon para cerrar nada.
        """
        primera = True
        try:
            while not self._stopping.is_set():
                cap = self._open(require_signal=primera)
                if cap is None:
                    if primera:
                        return                      # nunca hubo imagen: se sale
                    if not self._esperar_reintento():
                        return
                    continue

                primera = False
                try:
                    self._loop(cap)
                finally:
                    cap.release()
                    log.info("dispositivo liberado")

                if self._stopping.is_set():
                    return
                # Se perdio la senal. No es motivo para cerrar: se reconecta.
                log.info("se reconecta tras: %s", self.stop_reason)
                self.stop_reason = None
                if not self._esperar_reintento():
                    return
        except Exception as exc:
            log.exception("fallo en el hilo de captura")
            self.error = str(exc)
            self.stop_reason = self.stop_reason or STOP_SIGNAL_LOST
        finally:
            self.ready.set()
            self.new_frame.set()

    def _esperar_reintento(self):
        """True si hay que seguir intentando; False si toca rendirse."""
        if self.sin_senal_desde is None:
            self.sin_senal_desde = time.monotonic()
        transcurrido = time.monotonic() - self.sin_senal_desde
        if transcurrido > SIGNAL_GIVEUP:
            log.warning("sin imagen valida durante %.0f s: se abandona", transcurrido)
            self.stop_reason = STOP_FLAT
            return False
        self.waiting = True
        self.new_frame.set()          # que el reproductor repinte el aviso
        return not self._stopping.wait(REOPEN_DELAY)

    def _confirmar_indice(self):
        """Corrige el indice consultando DirectShow por COM. 7 ms, sin abrir nada.

        Esto cierra un agujero real: si el indice cacheado apunta a otro
        dispositivo que TAMBIEN entrega 1080p con imagen (iVCam con un movil
        conectado, por ejemplo), el sondeo lo daria por bueno y acabariamos
        mostrando la camara del telefono en vez de la PS5, sin ningun error.
        El DevicePath lleva el identificador de fabricante y producto del USB, o
        sea que identifica el aparato de verdad.

        Es mejor esfuerzo: si COM no contesta, se sigue con el sondeo de siempre.
        """
        if not self.device_path:
            return
        try:
            import dshow
            lista = dshow.enumerar()
        except Exception:
            log.debug("no se pudo consultar DirectShow", exc_info=True)
            return
        if not lista:
            return              # COM no contesta: no se puede concluir nada

        patron = self.device_path.lower()
        encontrado = next((d["index"] for d in lista
                           if patron in (d.get("path") or "").lower()), None)
        if encontrado is None:
            # COM funciona y la capturadora NO esta. Es ausencia de verdad, no
            # falta de senal: se dice ya y se ahorra abrir dispositivos en vano.
            raise CaptureError(STOP_ABSENT,
                               "la capturadora no aparece entre los dispositivos de "
                               "video; comprueba el cable USB")
        if encontrado != self.index:
            log.warning("el indice %s apunta a otro dispositivo; la capturadora es "
                        "el %s", self.index, encontrado)
            self.index = encontrado
            self.index_changed = True

    def _open(self, require_signal=True):
        try:
            if require_signal:
                self._confirmar_indice()

            # Se intenta PRIMERO el indice cacheado, directamente.
            #
            # Antes se validaba el indice abriendo el dispositivo, cerrandolo y
            # volviendolo a abrir para usarlo: dos aperturas de DirectShow, casi
            # dos segundos, en el caso normal en que el indice cacheado es el
            # correcto. Ahora solo se escanea cuando la apertura directa falla,
            # que es justo cuando el escaneo sirve de algo.
            try:
                cap, info = open_verified(self.index, self.prefer, require_signal)
            except CaptureError as exc:
                if exc.reason != STOP_NO_SIGNAL or not require_signal:
                    raise
                log.info("el indice %s ya no vale (%s); se escanea",
                         self.index, exc.detail)
                idx, _ = resolve_index(None)
                if idx != self.index:
                    log.warning("el indice cambia de %s a %s", self.index, idx)
                    self.index = idx
                    self.index_changed = True
                cap, info = open_verified(self.index, self.prefer, require_signal)
        except CaptureError as exc:
            if require_signal:
                log.error("apertura fallida (%s): %s", exc.reason, exc.detail)
            else:
                log.info("reconexion aun sin exito: %s", exc.detail)
            self.stop_reason = exc.reason
            self.error = exc.detail
            return None
        self.info = info
        self.waiting = False
        self.sin_senal_desde = None
        log.info("capturadora lista: %s", info)
        self.ready.set()
        return cap

    def _loop(self, cap):
        fails = 0
        last_ok = time.monotonic()
        flat_since = None
        next_flat_check = 0.0

        while not self._stopping.is_set():
            t_read = time.perf_counter()
            if not cap.grab():
                # EC_DEVICE_LOST: el grafo se ha caido, no hay vuelta atras.
                log.info("grab() False: dispositivo perdido")
                self.stop_reason = STOP_SIGNAL_LOST
                return

            ok, frame = cap.retrieve()
            self.read_ms += ((time.perf_counter() - t_read) * 1000.0 - self.read_ms) * 0.1
            now = time.monotonic()

            if not ok or frame is None:
                fails += 1
                if fails >= RETRIEVE_FAIL_MAX:
                    log.info("retrieve() False x%d: senal perdida", fails)
                    self.stop_reason = STOP_SIGNAL_LOST
                    return
                continue

            fails = 0
            last_ok = now

            # Cambio de resolucion. Medido: NO es lo que pasa al apagar la
            # consola con esta tarjeta, que se queda en 1080p. Se conserva
            # porque cuesta una comparacion de enteros y si cubre un caso real
            # distinto: que la PS5 cambie de modo de salida a mitad de partida
            # (un juego a 1080p y un menu a 2160p, por ejemplo).
            if frame.shape[1] != W or frame.shape[0] != H:
                log.info("la capturadora ha vuelto a %dx%d: senal perdida",
                         frame.shape[1], frame.shape[0])
                self.stop_reason = STOP_SIGNAL_LOST
                return

            self._frames += 1

            with self._lock:
                self.frame = frame
            self.new_frame.set()

            # Imagen negra. La capturadora sigue entregando fotogramas (medido),
            # solo que sin contenido. Esto pasa continuamente en uso normal: al
            # arrancar un juego, la PS5 renegocia el HDMI y la senal se cae unos
            # segundos.
            #
            # Por eso aqui NO se cierra nada. Se avisa en pantalla y se sigue
            # leyendo, esperando a que vuelva. Solo se abandona si no vuelve en
            # SIGNAL_GIVEUP, y entonces decide el supervisor. Se muestrea sobre
            # una submuestra: son ~8000 pixeles, no 2 millones.
            if now >= next_flat_check:
                next_flat_check = now + FLAT_PERIOD
                if float(frame[::16, ::16, 1].std()) < FLAT_STD:
                    if flat_since is None:
                        flat_since = now
                        self.sin_senal_desde = now
                    elif not self.waiting and now - flat_since >= FLAT_NOTICE:
                        log.info("sin imagen; esperando a que vuelva la senal")
                        self.waiting = True
                    elif now - flat_since >= SIGNAL_GIVEUP:
                        log.warning("sin imagen durante %.0f s: se abandona",
                                    SIGNAL_GIVEUP)
                        self.stop_reason = STOP_FLAT
                        return
                elif flat_since is not None:
                    if self.waiting:
                        log.info("senal recuperada tras %.1f s", now - flat_since)
                    flat_since = None
                    self.waiting = False
                    self.sin_senal_desde = None

            if now - last_ok > NO_FRAME_MAX:
                log.info("sin fotogramas durante %.1f s", now - last_ok)
                self.stop_reason = STOP_SIGNAL_LOST
                return

        self.stop_reason = self.stop_reason or None


if __name__ == "__main__":
    import core
    core.setup("capture-test")
    tune_opencv()
    preferido = core.get("cam_index")
    print("indice preferido segun la configuracion: %s" % preferido)
    t0 = time.perf_counter()
    try:
        idx, probe = resolve_index(preferido)
        print("indice resuelto: %d en %.2f s  %s" % (idx, time.perf_counter() - t0, probe))
        if idx != preferido:
            core.save_config(cam_index=idx)
            print("guardado cam_index=%d en la configuracion" % idx)

        cap, info = open_verified(idx)
        print("apertura verificada: %s" % info)
        fps, shape, std = _measure(cap, 3.0)
        print("3 s sostenidos: %.1f fps, shape %s, std %.2f" % (fps, shape, std))
        cap.release()
    except CaptureError as exc:
        print("FALLO (%s): %s" % (exc.reason, exc.detail))
