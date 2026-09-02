"""Proceso reproductor: abre la capturadora, pinta y sale con un codigo.

El codigo de salida ES el canal de comunicacion con el supervisor. No hay
tuberias a proposito: con --windowed sys.stdout es None, y un stdout=PIPE sin
nadie leyendo bloquea al hijo en cuanto llena los 64 KB del buffer.
"""

import argparse
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import core

# Codigos de salida. Se registran siempre por nombre, nunca como numero suelto.
EXIT_SIGNAL_LOST = 0
EXIT_INTERNAL = 1
EXIT_NO_SIGNAL = 2
EXIT_BUSY = 3
EXIT_ABSENT = 4
EXIT_FLAT = 5
EXIT_AUDIO = 6
EXIT_USER = 10
EXIT_PARENT = 20

# Mensajes para cuando el reproductor no puede abrir y lo ha lanzado el usuario
# a mano. Cada uno dice QUE pasa y QUE hacer, no solo que ha fallado.
MOTIVOS_VISIBLES = {
    2: "No llega imagen de la PS5.\n\n"
       "Comprueba que la consola está encendida y que el cable HDMI va del "
       "puerto HDMI OUT de la PS5 al HDMI IN de la capturadora.",
    3: "Otro programa está usando la capturadora.\n\n"
       "Cierra OBS, PotPlayer o lo que la tenga abierta y vuelve a intentarlo. "
       "La capturadora solo admite un programa a la vez.",
    4: "No se encuentra la capturadora.\n\n"
       "Comprueba que el cable USB está bien conectado. Si acabas de "
       "enchufarla, espera unos segundos y reinténtalo.",
    5: "La PS5 parece apagada, o la imagen llega en negro.\n\n"
       "Si la consola está encendida, revisa en Ajustes > Sistema > HDMI que el "
       "HDCP esté desactivado y que la salida sea 1080p SDR.",
}

VOL_STEP = 5                # puntos de volumen por pulsacion
VOL_SAVE_DELAY = 1.0        # espera antes de persistir, para no escribir 20 veces

EXIT_NAMES = {
    0: "SIGNAL_LOST", 1: "INTERNAL", 2: "NO_SIGNAL", 3: "BUSY", 4: "ABSENT",
    5: "FLAT", 6: "AUDIO", 10: "USER", 20: "PARENT",
}

log = logging.getLogger("player")


# --------------------------------------------------------------------------
# Capturas de pantalla
# --------------------------------------------------------------------------

class ScreenshotWriter:
    """Codifica y guarda PNG fuera del hilo principal.

    Codificar un PNG de 1080p cuesta entre 30 y 80 ms. Hacerlo en el bucle de
    pintado tiraria cuatro fotogramas y se notaria el tiron justo en el momento
    en que el usuario quiere inmortalizar algo. Al pulsar P solo se copia el
    fotograma (6,2 MB, uno o dos milisegundos) y se encola.
    """

    def __init__(self, on_done=None):
        self.q = queue.Queue(maxsize=8)
        self.on_done = on_done
        self._dir = None
        self._thread = threading.Thread(target=self._run, name="screenshot", daemon=True)
        self._thread.start()

    def request(self, frame):
        try:
            self.q.put_nowait(frame.copy())
            return True
        except queue.Full:
            log.warning("cola de capturas llena, se descarta")
            return False

    def stop(self):
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass

    # -- interno

    def _target_dir(self):
        if self._dir is not None:
            return self._dir
        wanted = core.screenshots_dir()
        alternativa = core.screenshots_fallback()
        candidatos = [wanted] if wanted == alternativa else [wanted, alternativa]
        for candidate in candidatos:
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                probe = candidate / ".escritura"
                probe.write_bytes(b"")
                probe.unlink()
                self._dir = candidate
                if candidate != wanted:
                    log.warning("%s no es escribible, las capturas van a %s", wanted, candidate)
                return candidate
            except OSError as exc:
                log.warning("no se puede usar %s: %s", candidate, exc)
        self._dir = Path(".")
        return self._dir

    @staticmethod
    def _unique(folder, stem):
        """screenshot_PS5_DDMMYYYY.png, con sufijo solo si ya existe.

        El formato pedido lleva unicamente la fecha, asi que la segunda captura
        del mismo dia machacaria la primera. La primera del dia conserva el
        nombre exacto; a partir de ahi se numera.
        """
        p = folder / ("%s.png" % stem)
        if not p.exists():
            return p
        for n in range(2, 1000):
            p = folder / ("%s_%02d.png" % (stem, n))
            if not p.exists():
                return p
        return folder / ("%s_%d.png" % (stem, int(time.time())))

    def _run(self):
        import cv2
        while True:
            frame = self.q.get()
            if frame is None:
                return
            try:
                folder = self._target_dir()
                path = self._unique(folder, "screenshot_PS5_" + time.strftime("%d%m%Y"))
                ok, buf = cv2.imencode(".png", frame, [cv2.IMWRITE_PNG_COMPRESSION, 3])
                if not ok:
                    raise RuntimeError("imencode devolvio False")
                # imencode + write_bytes en vez de imwrite: imwrite falla EN
                # SILENCIO cuando la ruta lleva caracteres no ASCII en Windows.
                path.write_bytes(buf.tobytes())
                log.info("captura guardada: %s (%.1f MB)", path, len(buf) / 1e6)
                if self.on_done:
                    self.on_done(path.name, None)
            except Exception as exc:
                log.exception("no se pudo guardar la captura")
                if self.on_done:
                    self.on_done(None, str(exc))


# --------------------------------------------------------------------------
# Proceso de audio
# --------------------------------------------------------------------------

AUDIO_POLL_S = 2.0
AUDIO_RETRY_S = 5.0
AUDIO_MAX_TRIES = 3

# Nombres de los codigos de salida de audio.py, para que el aviso diga algo util
# en vez de un numero pelado.
AUDIO_REASONS = {
    30: "no se encuentra la entrada de la capturadora",
    31: "el nombre del dispositivo de sonido es ambiguo",
    32: "PortAudio no arranca",
    33: "el dispositivo no acepta 48 kHz en estéreo",
    1: "error interno",
}


class AudioChild:
    """Lanza y vigila el proceso del puente de audio.

    Vigilarlo no es un extra. Con --windowed no hay stdout, y sin tuberias (que
    se descartan a proposito: un PIPE sin lector bloquea al hijo al llenar los
    64 KB) el unico canal es el codigo de salida. Sin esta vigilancia, si el
    puente muere al arrancar el usuario se queda jugando en silencio sin ningun
    indicio de por que: exactamente el sintoma que motivo el proyecto entero.
    """

    def __init__(self, on_status):
        self.on_status = on_status
        self.proc = None
        self.tries = 0
        self.next_try = 0.0
        self._next_poll = 0.0

    def _spawn(self):
        argv = core.child_argv("audio", parent_pid=os.getpid())
        flags = 0
        if os.name == "nt":
            # ABOVE_NORMAL importa: la clase de prioridad SE HEREDA, y el
            # callback de PortAudio no puede competir desde una clase baja.
            flags = subprocess.CREATE_NO_WINDOW | subprocess.ABOVE_NORMAL_PRIORITY_CLASS
        log.info("lanzando audio: %s", " ".join(argv))
        self.proc = subprocess.Popen(argv, creationflags=flags,
                                     cwd=str(core.app_dir()))
        self.tries += 1

    def start(self):
        try:
            self._spawn()
        except Exception as exc:
            log.exception("no se pudo lanzar el proceso de audio")
            self.on_status("Sin audio — no se pudo arrancar (%s)" % exc)

    def poll(self, now):
        if now < self._next_poll:
            return
        self._next_poll = now + AUDIO_POLL_S

        if self.proc is not None:
            code = self.proc.poll()
            if code is None:
                return
            motivo = AUDIO_REASONS.get(code, "código %d" % code)
            log.error("el puente de audio ha muerto: %s", motivo)
            self.proc = None
            if self.tries >= AUDIO_MAX_TRIES:
                self.on_status("Sin audio — %s" % motivo)
                return
            self.on_status("Se ha cortado el audio (%s). Reintentando…" % motivo)
            self.next_try = now + AUDIO_RETRY_S
            return

        if self.tries < AUDIO_MAX_TRIES and now >= self.next_try:
            self.start()

    def stop(self):
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3.0)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None


# --------------------------------------------------------------------------
# Bucle
# --------------------------------------------------------------------------

class Volumen:
    """Volumen 0-100, compartido con el proceso de audio por memoria mapeada.

    Aqui solo se guarda el numero y se traduce a ganancia; quien la aplica a las
    muestras es el callback de audio, con una rampa para que no chasquee.
    """

    def __init__(self, inicial):
        import winhy
        self.v = max(0, min(100, int(inicial)))
        self.shm = winhy.SharedFloat(winhy.VOL_SHM, self._gain())
        self._guardar_en = 0.0
        if not self.shm.ok:
            log.warning("sin memoria compartida: el volumen no tendra efecto")

    @staticmethod
    def _curva(v):
        # Misma curva que audio.py: al cuadrado, porque el oido es logaritmico y
        # una escala lineal se notaria casi toda entre 0 y 20.
        return (max(0.0, min(100.0, float(v))) / 100.0) ** 2

    def _gain(self):
        return self._curva(self.v)

    def ajustar(self, delta):
        antes = self.v
        self.v = max(0, min(100, self.v + delta))
        if self.v == antes:
            return False
        self.shm.set(self._gain())
        self._guardar_en = time.monotonic() + VOL_SAVE_DELAY
        return True

    def tick(self, ahora):
        """Persiste con retardo: subir de 50 a 100 son diez pulsaciones, y no
        tiene sentido escribir el fichero de configuracion diez veces."""
        if self._guardar_en and ahora >= self._guardar_en:
            self._guardar_en = 0.0
            core.save_config(volume=self.v)
            log.info("volumen guardado: %d%%", self.v)

    def close(self):
        if self._guardar_en:
            core.save_config(volume=self.v)
        self.shm.close()


class Supervision:
    """Escucha al proceso padre: el evento de parada y su propia vida.

    Sin esto, el demonio no tiene forma limpia de cerrar la partida: senala el
    evento, nadie lo escucha, espera tres segundos y acaba con TerminateProcess.
    Y matar el reproductor a lo bruto significa que NO libera el filtro
    DirectShow, que es de acceso exclusivo: el aparato queda tomado hasta que
    Windows recoge el proceso. Eso pasaria en CADA cierre automatico.

    El handle del padre es cinturon y tirantes para el caso contrario: que maten
    al demonio y este proceso se quede huerfano a pantalla completa.
    """

    def __init__(self, stop_event=None, parent_pid=None):
        import winhy
        self.w = winhy
        self.stop = winhy.open_event(stop_event) if stop_event else None
        self.parent = winhy.open_process(parent_pid) if parent_pid else None
        self._next = 0.0
        if stop_event and not self.stop:
            log.warning("no se pudo abrir el evento de parada %s", stop_event)
        if self.stop or self.parent:
            log.info("supervisado por el demonio (evento=%s, padre=%s)",
                     bool(self.stop), bool(self.parent))

    def debe_parar(self, ahora):
        """True si el padre pide cerrar o ha muerto. Se consulta cada 250 ms:
        con timeout 0 no bloquea, pero tampoco hace falta mirarlo 60 veces por
        segundo."""
        if ahora < self._next:
            return False
        self._next = ahora + 0.25
        if self.stop and self.w.wait(self.stop, 0):
            log.info("el demonio pide cerrar")
            return True
        if self.parent and self.w.wait(self.parent, 0):
            log.info("el proceso padre ha muerto")
            return True
        return False

    def close(self):
        self.w.close(self.stop)
        self.w.close(self.parent)


def run(cam_index, monitor, vsync, fullscreen, prefer="yuy2",
        stop_event=None, parent_pid=None):
    import capture
    import display

    core.set_dpi_aware()
    capture.tune_opencv()

    cap_thread = capture.CaptureThread(cam_index, prefer=prefer,
                                       device_path=core.get("cam_device_path"))
    cap_thread.start()

    # El hijo valida que hay senal ANTES de crear la ventana: si la consola esta
    # apagada no debe aparecer un rectangulo negro a pantalla completa.
    listo = cap_thread.ready.wait(capture.OPEN_BUDGET_S + 2.0)

    # El indice corregido se guarda AQUI, antes de decidir si hay senal. Es
    # informacion valida por si misma: si la capturadora ha cambiado de indice,
    # eso es cierto tanto si la consola esta encendida como si no. Guardarlo solo
    # en el camino de exito hacia que con la consola apagada se perdiera la
    # correccion y hubiera que rehacerla en cada intento.
    if cap_thread.index_changed:
        core.save_config(cam_index=cap_thread.index)
        log.info("indice %d guardado en la configuracion", cap_thread.index)

    if not listo or cap_thread.info is None:
        cap_thread.stop()
        cap_thread.join(timeout=3.0)
        reason = cap_thread.stop_reason or capture.STOP_NO_SIGNAL
        log.info("sin ventana, motivo=%s", reason)
        return {
            capture.STOP_NO_SIGNAL: EXIT_NO_SIGNAL,
            capture.STOP_BUSY: EXIT_BUSY,
            capture.STOP_ABSENT: EXIT_ABSENT,
            capture.STOP_FLAT: EXIT_FLAT,
        }.get(reason, EXIT_NO_SIGNAL)

    dsp = display.Display(monitor=monitor, vsync=vsync, fullscreen=fullscreen)
    shots = ScreenshotWriter(
        on_done=lambda name, err: dsp.flash(
            "Captura guardada: %s" % name if name
            else "No se pudo guardar la captura: %s" % err, 2.0))

    def audio_status(texto):
        if texto.startswith("Sin audio"):
            dsp.notice(texto, "audio")   # persistente: no se puede pasar por alto
        else:
            dsp.flash(texto, 3.0)

    vol = Volumen(core.get("volume"))
    audio = AudioChild(audio_status)
    audio.start()
    jefe = Supervision(stop_event, parent_pid)

    exit_code = EXIT_SIGNAL_LOST
    stats = {"fps": 0.0, "read_ms": 0.0, "convert_ms": 0.0, "blit_ms": 0.0,
             "extra": "ruta %s  shape %s" % (cap_thread.info["ruta"], cap_thread.info["shape"])}
    shown = 0
    esperando = False
    t_fps = time.perf_counter()

    try:
        while True:
            # Ritmo por evento: sin esto el bucle giraria a cientos de fps
            # quemando CPU contra una fuente de 60 Hz.
            if not cap_thread.new_frame.wait(timeout=0.1):
                if not cap_thread.is_alive():
                    break
                continue
            cap_thread.new_frame.clear()

            if not cap_thread.is_alive():
                break

            frame = cap_thread.latest()
            if frame is None:
                continue

            actions = dsp.pump()
            if display.QUIT in actions:
                exit_code = EXIT_USER
                log.info("cerrado por el usuario (ESC)")
                break
            if display.SCREENSHOT in actions:
                shots.request(frame)
            if display.VOL_UP in actions or display.VOL_DOWN in actions:
                paso = VOL_STEP * (actions.count(display.VOL_UP)
                                   - actions.count(display.VOL_DOWN))
                if vol.ajustar(paso):
                    dsp.flash("Volumen %d%%" % vol.v if vol.v else "Silencio", 1.2)

            conv_ms, blit_ms = dsp.present(frame, stats)
            shown += 1

            ahora_m = time.monotonic()
            if jefe.debe_parar(ahora_m):
                exit_code = EXIT_PARENT
                break

            audio.poll(ahora_m)
            vol.tick(ahora_m)

            # Perder la senal no cierra nada: se avisa y se espera. Arrancar un
            # juego renegocia el HDMI y la imagen se cae unos segundos; cerrar
            # ahi hacia imposible entrar en ningun juego.
            if cap_thread.waiting != esperando:
                esperando = cap_thread.waiting
                dsp.notice("Sin señal — esperando a que vuelva la imagen"
                           if esperando else "", "senal")

            now = time.perf_counter()
            if now - t_fps >= 0.5:
                stats["fps"] = shown / (now - t_fps)
                stats["read_ms"] = cap_thread.read_ms
                stats["convert_ms"] = conv_ms
                stats["blit_ms"] = blit_ms
                shown, t_fps = 0, now

        if exit_code not in (EXIT_USER, EXIT_PARENT):
            reason = cap_thread.stop_reason or capture.STOP_SIGNAL_LOST
            exit_code = {
                capture.STOP_SIGNAL_LOST: EXIT_SIGNAL_LOST,
                capture.STOP_FLAT: EXIT_FLAT,
                capture.STOP_NO_SIGNAL: EXIT_NO_SIGNAL,
                capture.STOP_BUSY: EXIT_BUSY,
                capture.STOP_ABSENT: EXIT_ABSENT,
            }.get(reason, EXIT_SIGNAL_LOST)
    finally:
        vol.close()
        jefe.close()
        audio.stop()
        shots.stop()
        cap_thread.stop()
        cap_thread.join(timeout=5.0)
        if cap_thread.is_alive():
            # El hilo sigue bloqueado dentro del driver. El proceso es
            # desechable justamente para esto: al morir, Windows suelta el filtro.
            log.error("el hilo de captura no termino: el dispositivo puede quedar tomado")
        dsp.close()

    return exit_code


def main(argv=None):
    ap = argparse.ArgumentParser(prog="player")
    ap.add_argument("--cam-index", type=int, default=None)
    ap.add_argument("--monitor", type=int, default=None)
    modo = ap.add_mutually_exclusive_group()
    modo.add_argument("--windowed", action="store_true",
                      help="arranca en ventana (por defecto)")
    modo.add_argument("--fullscreen", action="store_true",
                      help="arranca a pantalla completa")
    ap.add_argument("--format", choices=("yuy2", "mjpg"), default=None,
                    help="formato de captura; yuy2 es sin comprimir y se ve mejor")
    # Los recibe del supervisor a partir de la tanda 3; aqui se aceptan para que
    # el contrato de argv sea estable desde el principio.
    ap.add_argument("--stop-event", default=None)
    ap.add_argument("--parent-pid", type=int, default=None)
    args = ap.parse_args(argv)

    log_ = core.setup("player")

    cam = args.cam_index if args.cam_index is not None else core.get("cam_index")
    mon = args.monitor if args.monitor is not None else core.get("monitor")

    try:
        prefer = args.format or core.get("video_format")
        # Las banderas mandan sobre la configuracion; si no viene ninguna, la
        # configuracion decide. F alterna en caliente en cualquier caso.
        if args.fullscreen:
            full = True
        elif args.windowed:
            full = False
        else:
            full = bool(core.get("start_fullscreen"))
        code = run(cam, mon, core.get("vsync"), full, prefer,
                   args.stop_event, args.parent_pid)
    except ImportError as exc:
        # El fallo mas probable durante el desarrollo, con diferencia: lanzarlo
        # con el Python del sistema en vez de con el del proyecto. Merece un
        # mensaje que diga exactamente que hacer, no un ModuleNotFoundError seco.
        venv = core.app_dir() / ".venv" / "Scripts" / "python.exe"
        msg = ("Falta una dependencia (%s).\n\n"
               "Casi seguro que se ha lanzado con el Python del sistema. "
               "Este proyecto usa su propio entorno:\n\n"
               "    & \"%s\" player.py\n\n"
               "Si el entorno no existe todavia:\n"
               "    py -3.9 -m venv .venv\n"
               "    .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt"
               % (exc.name or exc, venv))
        log_.error("dependencia ausente: %s (interprete: %s)", exc.name or exc, sys.executable)
        print("\n" + msg + "\n", file=sys.stderr)
        core.fatal(msg)   # no hace nada si hay consola: el mensaje ya salio ahi
        return EXIT_INTERNAL
    except Exception:
        log_.exception("fallo no controlado en el reproductor")
        return EXIT_INTERNAL

    log_.info("salida %d (%s)", code, EXIT_NAMES.get(code, "?"))

    # Si lo ha lanzado el usuario a mano -sin --parent-pid, o sea sin supervisor
    # detras- y no se ha podido abrir, hay que DECIRSELO. Empaquetado no hay
    # consola: sin esto, el programa se cierra sin dejar rastro visible y desde
    # fuera parece que el doble clic no ha hecho nada.
    if args.parent_pid is None and code in MOTIVOS_VISIBLES:
        core.fatal(MOTIVOS_VISIBLES[code])

    return code


if __name__ == "__main__":
    sys.exit(main())
