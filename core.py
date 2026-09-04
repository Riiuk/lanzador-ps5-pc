"""Cimientos comunes a los tres procesos: rutas, configuracion, logging y
enganches de excepciones.

Este modulo se importa SIEMPRE el primero, antes que cv2, pygame o sounddevice.
La razon es el arranque automatico: cuando Windows lanza la entrada de Run, el
proceso no tiene consola (sys.stdout es None) y el directorio de trabajo es
C:\\Windows\\system32. Cualquier print o cualquier ruta relativa revienta ahi y
solo ahi, nunca al probar a mano, que es la peor clase de fallo posible.
"""

import io
import json
import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_NAME = "LanzadorPS5"

# El faulthandler necesita que el fichero siga vivo mientras dure el proceso; si
# lo recoge el GC, el volcado de un cuelgue duro se pierde.
_crash_file = None

# Habia consola real al arrancar. Se decide UNA vez, en setup(), y nunca con
# isatty(): isatty() tambien devuelve False cuando la salida esta redirigida a
# un fichero o a una tuberia, que es lo normal, y usarlo para decidir si sacar un
# MessageBox hace aparecer un modal bloqueante en mitad de una ejecucion normal.
_had_console = False


def has_console() -> bool:
    return _had_console


# --------------------------------------------------------------------------
# Rutas
# --------------------------------------------------------------------------

def frozen() -> bool:
    return getattr(sys, "frozen", False)


def app_dir() -> Path:
    """Carpeta de la aplicacion: la del .exe si esta empaquetado, la del codigo
    si no. Nunca os.getcwd(): con el arranque automatico seria system32."""
    if frozen():
        return Path(sys.executable).resolve().parent
    return Path(os.path.abspath(__file__)).parent


def resource_dir() -> Path:
    """Recursos empaquetados (assets). En PyInstaller 6.x el onedir los deja en
    _internal, y sys._MEIPASS apunta ahi."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else app_dir()


def data_dir() -> Path:
    """Config y registros. Van a LOCALAPPDATA y nunca junto a __file__: bajo
    PyInstaller eso seria _MEIxxxx, que se borra al salir."""
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = Path(root) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return data_dir() / "config.json"


_defaults_instalacion = None


def install_defaults() -> dict:
    """Valores elegidos durante la INSTALACION, guardados junto al ejecutable.

    Existen porque el instalador no puede escribir en la configuracion del
    usuario de forma fiable -corre elevado, y con varios usuarios cada uno tiene
    la suya-, pero si en su propia carpeta. Asi la eleccion hecha al instalar
    vale para cualquiera que use el equipo, y cada usuario puede seguir
    cambiandola en su config.json, que manda por encima de esto.
    """
    global _defaults_instalacion
    if _defaults_instalacion is None:
        _defaults_instalacion = {}
        p = app_dir() / "defaults.json"
        try:
            if p.exists():
                with io.open(p, "r", encoding="utf-8") as fh:
                    datos = json.load(fh)
                if isinstance(datos, dict):
                    _defaults_instalacion = datos
        except Exception:
            logging.getLogger(__name__).warning("defaults.json ilegible", exc_info=True)
    return _defaults_instalacion


def pictures_dir() -> Path:
    """Carpeta Imagenes del usuario, preguntandosela a Windows.

    Con la API y no juntando "~" con "Pictures": la carpeta puede estar
    redirigida a OneDrive o a otra unidad, y en un Windows en espanol ni
    siquiera se llama asi. Si la API falla, entonces si se recurre a la ruta
    clasica.
    """
    try:
        import ctypes
        from ctypes import wintypes
        CSIDL_MYPICTURES = 0x0027
        buf = ctypes.create_unicode_buffer(260)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.SHGetFolderPathW.argtypes = (wintypes.HWND, ctypes.c_int,
                                             wintypes.HANDLE, wintypes.DWORD,
                                             wintypes.LPWSTR)
        shell32.SHGetFolderPathW.restype = ctypes.c_long
        if shell32.SHGetFolderPathW(None, CSIDL_MYPICTURES, None, 0, buf) == 0:
            if buf.value:
                return Path(buf.value)
    except Exception:
        pass
    return Path(os.path.expanduser("~")) / "Pictures"


def screenshots_dir() -> Path:
    """Carpeta de capturas, por orden de preferencia.

    Ya NO va junto a la aplicacion: instalado en Archivos de programa esa
    carpeta es de solo lectura y las capturas acabarian siempre en el plan B.
    Ahora el destino se elige al instalar y, si no, va a Imagenes.

    No se crea aqui: se crea al guardar la primera captura.
    """
    override = get("screenshots_dir")
    if override:
        return Path(override)
    del_instalador = install_defaults().get("screenshots_dir")
    if del_instalador:
        return Path(del_instalador)
    return pictures_dir() / "Screenshots PS5"


def screenshots_fallback() -> Path:
    """Ultimo recurso si el destino elegido no se puede escribir: una unidad
    externa desconectada, una carpeta borrada, permisos cambiados."""
    return pictures_dir() / "Screenshots PS5"


def child_argv(mode: str, **kw) -> list:
    """Linea de comandos para lanzar un proceso hijo.

    Congelado, sys.executable ya es la app y basta el flag. Sin congelar hay que
    anadir la ruta absoluta de ps5.py, porque sys.executable es python.exe.
    """
    if frozen():
        base = [sys.executable]
    else:
        # Sin congelar hay que decirle a python.exe QUE ejecutar. A partir de la
        # tanda 3 existe ps5.py y hace de router; hasta entonces se lanza el
        # modulo del modo directamente. El flag va igual en los dos casos, asi
        # que el contrato de argv no cambia cuando aparezca el router.
        router = app_dir() / "ps5.py"
        script = router if router.exists() else app_dir() / (mode + ".py")
        base = [sys.executable, str(script)]
    argv = base + ["--" + mode]
    for k, v in kw.items():
        argv += ["--" + k.replace("_", "-"), str(v)]
    return argv


# --------------------------------------------------------------------------
# Configuracion
# --------------------------------------------------------------------------

DEFAULTS = {
    "auto_mode": True,
    "cam_index": 1,
    "cam_device_path": "vid_345f&pid_2131",
    "monitor": 0,
    # Arranca en ventana; la pantalla completa la pone el usuario con F cuando
    # quiere. Ademas, una ventana de 1920x1080 en un monitor de 2560x1440 no
    # escala nada: es 1:1 pixel perfecto, la mejor imagen posible.
    "start_fullscreen": False,
    "vsync": False,
    # "yuy2" (sin comprimir, mejor imagen) o "mjpg" (comprimido, por si el
    # ancho de banda del USB diera problemas). Para comparar a ojo sin tocar
    # esto: player.py --format mjpg
    "video_format": "yuy2",
    # Vacio = usar lo que eligio el instalador y, si no hay nada, Imagenes.
    "screenshots_dir": "",
    "audio_in_name": "USB3.0",
    "audio_out_name": "",
    # Medido con audio.py --measure: usbaudio.sys entrega bloques de 480 frames
    # cada 10,00 ms de media, p99 12,24 ms y maximo 12,89 ms, SIN rafagas. Con
    # 30 ms hay mas del doble de margen sobre el peor hueco observado, y son 30
    # ms menos de latencia que el valor conservador de 60 con el que se partia.
    "target_ms": 30,
    "av_offset_ms": 0.0,
    # Volumen 0-100, con las flechas arriba y abajo. Se convierte a ganancia
    # elevando al cuadrado, porque el oido es logaritmico.
    "volume": 100,
    "ps5_host_id": "",
    "ps5_ip": "",
    "suppressed_until": 0.0,
    "last_exit_code": 0,
    "last_exit_ts": 0.0,
    "log_level": "INFO",
}

_cfg = None


def load_config(force: bool = False) -> dict:
    """Lee config.json fusionando con los valores por defecto.

    El merge no es cosmetico: garantiza que anadir una clave en una version
    futura no rompa una instalacion vieja. Un JSON corrupto (corte de luz a
    mitad de escritura) no puede tumbar el arranque, asi que se aparta y se
    regenera.
    """
    global _cfg
    if _cfg is not None and not force:
        return _cfg

    cfg = dict(DEFAULTS)
    p = config_path()
    if p.exists():
        try:
            with io.open(p, "r", encoding="utf-8") as fh:
                disk = json.load(fh)
            if isinstance(disk, dict):
                cfg.update({k: v for k, v in disk.items() if k in DEFAULTS})
            else:
                raise ValueError("config.json no contiene un objeto")
        except Exception as exc:
            bad = p.with_name("config.bad-%d.json" % int(time.time()))
            try:
                p.replace(bad)
            except OSError:
                pass
            logging.getLogger(__name__).warning(
                "config.json ilegible (%s), apartado en %s y regenerado", exc, bad.name)
    _cfg = cfg
    return _cfg


def get(key, default=None):
    return load_config().get(key, DEFAULTS.get(key, default))


_cfg_lock = threading.Lock()


def save_config(**changes) -> dict:
    """Escritura atomica: temporal en el MISMO directorio y os.replace.

    En el mismo directorio porque replace solo es atomico dentro de un volumen.

    Con cerrojo porque hay dos hilos escribiendo: el de la bandeja cuando el
    usuario marca una casilla, y el principal con el estado de la maquina. Sin
    el, dos escrituras solapadas pueden dejar el diccionario a medias o pisarse
    el fichero temporal.
    """
    with _cfg_lock:
        # force=True a proposito: hay TRES procesos escribiendo este fichero y
        # cada uno tiene su propia copia cacheada. Con la copia en memoria, el
        # ultimo en escribir revertia lo que hubieran guardado los otros: el
        # reproductor guardaba el volumen y el demonio, al cerrarse la ventana,
        # lo devolvia al valor que leyo horas antes.
        cfg = load_config(force=True)
        cfg.update(changes)
        p = config_path()
        tmp = p.with_suffix(".json.tmp")
        with io.open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(p))
        return cfg


# --------------------------------------------------------------------------
# Logging y excepciones
# --------------------------------------------------------------------------

class _NullStream(io.TextIOBase):
    """Reemplazo de stdout/stderr cuando son None (build con --windowed).

    fileno() lanza UnsupportedOperation a proposito: devolver un descriptor
    inventado hace que una libreria en C escriba en un fd ajeno, que es mucho
    peor que un error claro.
    """

    def write(self, s):
        return len(s)

    def flush(self):
        pass

    def fileno(self):
        raise io.UnsupportedOperation("sin consola")

    def isatty(self):
        return False


# Un crash-<rol>-<pid>.log por proceso y arranque. Hace falta abierto ANTES del
# fallo -no se puede crear despues-, pero queda a cero cuando no hay ninguno,
# que es lo normal. Como el de audio se relanza en cada partida, sin barrer
# esto la carpeta de datos acaba con decenas de ficheros vacios.
CRASH_KEEP_S = 24 * 3600


def _limpiar_crash_vacios() -> None:
    """Borra los crash-*.log que esten vacios Y sean de hace mas de un dia.

    Las dos condiciones, no una: con contenido es justo el volcado que se
    queria guardar, y uno de hoy puede ser el de un proceso hermano VIVO. No
    vale fiarse de que Windows impida borrar un fichero abierto, porque si lo
    dejara borrar perderiamos en silencio el volcado de un cuelgue posterior.
    """
    limite = time.time() - CRASH_KEEP_S
    for p in data_dir().glob("crash-*.log"):
        try:
            st = p.stat()
            if st.st_size == 0 and st.st_mtime < limite:
                p.unlink()
        except OSError:
            pass                 # en uso, o ya lo borro otro: no es asunto nuestro


def setup(role: str) -> logging.Logger:
    """Prepara el proceso: stdio, log propio del rol, faulthandler y hooks.

    Un fichero de log POR ROL, no uno compartido: logging documenta que escribir
    a un mismo fichero desde varios procesos no esta soportado, y en Windows el
    os.rename de la rotacion sobre un fichero que otro proceso tiene abierto
    lanza PermissionError. Ese error acaba en stderr, que aqui es _NullStream, y
    el resultado seria un log que deja de rotar y crece sin limite.
    """
    global _crash_file, _had_console

    # Se anota ANTES de sustituir nada: si hay consola de verdad, el usuario
    # tiene que ver los errores ahi. Mandarlo todo al fichero y dejar la consola
    # muda hace que un fallo de arranque parezca "no ha pasado nada", que es la
    # peor forma posible de fallar mientras se desarrolla.
    _had_console = sys.stderr is not None
    had_console = _had_console

    if sys.stdout is None:
        sys.stdout = _NullStream()
    if sys.stderr is None:
        sys.stderr = _NullStream()

    level = getattr(logging, str(get("log_level", "INFO")).upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = RotatingFileHandler(
        str(data_dir() / ("lanzador-%s.log" % role)),
        maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(handler)

    if had_console:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
        root.addHandler(console)

    try:
        import faulthandler
        _crash_file = io.open(
            str(data_dir() / ("crash-%s-%d.log" % (role, os.getpid()))),
            "w", encoding="utf-8")
        faulthandler.enable(file=_crash_file, all_threads=True)
        _limpiar_crash_vacios()
    except Exception:
        pass

    log = logging.getLogger(role)

    def _excepthook(exc_type, exc, tb):
        log.critical("excepcion no capturada", exc_info=(exc_type, exc, tb))
        fatal("%s: %s" % (exc_type.__name__, exc))

    sys.excepthook = _excepthook

    def _thread_hook(args):
        log.critical("excepcion no capturada en %s", args.thread.name if args.thread else "?",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    if hasattr(sys, "unraisablehook"):
        sys.unraisablehook = lambda a: log.error("unraisable: %r en %r", a.exc_value, a.object)
    import threading
    threading.excepthook = _thread_hook

    log.info("--- %s arranca (pid %d, frozen=%s) ---", role, os.getpid(), frozen())
    return log


def fatal(message: str) -> None:
    """Ultimo recurso: SIN consola, un error tiene que ser visible en pantalla.

    Con consola no se abre nada. Un MessageBox es modal y bloquea el proceso
    hasta que alguien lo cierra: sacarlo cuando el mensaje ya esta en stderr
    solo sirve para congelar una ejecucion desatendida.
    """
    if _had_console:
        return
    try:
        import ctypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.MessageBoxW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p,
                                       ctypes.c_wchar_p, ctypes.c_uint)
        user32.MessageBoxW.restype = ctypes.c_int
        user32.MessageBoxW(None, message, APP_NAME + " - error", 0x10)
    except Exception:
        pass


def set_dpi_aware() -> None:
    """Se llama ANTES de importar cv2 o crear la ventana.

    Se encadena por VALOR DE RETORNO, no por excepcion: si el DPI ya viene
    fijado por manifiesto, la llamada devuelve E_ACCESSDENIED sin lanzar nada, y
    tratarlo como fallo llevaria a reintentar con una API peor.
    """
    import ctypes
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        shcore.SetProcessDpiAwareness.argtypes = (ctypes.c_int,)
        shcore.SetProcessDpiAwareness.restype = ctypes.c_long
        if shcore.SetProcessDpiAwareness(2) in (0, -2147024891):  # S_OK / E_ACCESSDENIED
            return
    except Exception:
        pass
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetProcessDPIAware.restype = ctypes.c_int
        user32.SetProcessDPIAware()
    except Exception:
        pass


if __name__ == "__main__":
    setup("selftest")
    print("frozen        :", frozen())
    print("app_dir       :", app_dir())
    print("resource_dir  :", resource_dir())
    print("data_dir      :", data_dir())
    print("config_path   :", config_path(), "(existe)" if config_path().exists() else "(se creara)")
    print("screenshots   :", screenshots_dir())
    print("child_argv    :", child_argv("player", stop_event="X", parent_pid=os.getpid()))
    print()
    for k, v in sorted(load_config().items()):
        print("  %-18s %r" % (k, v))
