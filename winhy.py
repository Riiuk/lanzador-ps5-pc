"""Win32 por ctypes: instancia unica, eventos, Job Object, energia.

REGLA INNEGOCIABLE DE ESTE MODULO: toda funcion declara restype y argtypes.

No es pedanteria. Sin declararlos, ctypes asume que todo devuelve un int de 32
bits, y un HANDLE de 64 bits se trunca por el camino. Demostrado en esta misma
maquina: GetCurrentProcess() con el restype por defecto hace que
GetPriorityClass devuelva 0 con ERROR_INVALID_HANDLE; declarandolo como HANDLE
devuelve 32. Donde de verdad duele es en CreateEventW y OpenEventW: un handle
truncado hace que SetEvent no llegue nunca al hijo, y el cierre limpio deja de
funcionar sin que nada avise.
"""

import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger("winhy")

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

SYNCHRONIZE = 0x00100000
EVENT_MODIFY_STATE = 0x0002
PROCESS_SET_INFORMATION = 0x0200
PROCESS_TERMINATE = 0x0001
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
ERROR_ALREADY_EXISTS = 183

IDLE_PRIORITY_CLASS = 0x00000040
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
NORMAL_PRIORITY_CLASS = 0x00000020
ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9

k32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
k32.CreateMutexW.restype = wintypes.HANDLE
k32.CreateEventW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL,
                             wintypes.LPCWSTR)
k32.CreateEventW.restype = wintypes.HANDLE
k32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
k32.OpenEventW.restype = wintypes.HANDLE
k32.SetEvent.argtypes = (wintypes.HANDLE,)
k32.SetEvent.restype = wintypes.BOOL
k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
k32.WaitForSingleObject.restype = wintypes.DWORD
k32.CloseHandle.argtypes = (wintypes.HANDLE,)
k32.CloseHandle.restype = wintypes.BOOL
k32.GetCurrentProcess.argtypes = ()
k32.GetCurrentProcess.restype = wintypes.HANDLE
k32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
k32.SetPriorityClass.restype = wintypes.BOOL
k32.GetPriorityClass.argtypes = (wintypes.HANDLE,)
k32.GetPriorityClass.restype = wintypes.DWORD
k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
k32.OpenProcess.restype = wintypes.HANDLE
k32.SetThreadExecutionState.argtypes = (wintypes.DWORD,)
k32.SetThreadExecutionState.restype = wintypes.DWORD
k32.GetTickCount64.argtypes = ()
k32.GetTickCount64.restype = ctypes.c_ulonglong
k32.QueryUnbiasedInterruptTime.argtypes = (ctypes.POINTER(ctypes.c_ulonglong),)
k32.QueryUnbiasedInterruptTime.restype = wintypes.BOOL
k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
k32.CreateJobObjectW.restype = wintypes.HANDLE
k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
k32.AssignProcessToJobObject.restype = wintypes.BOOL
k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                        ctypes.c_void_p, wintypes.DWORD)
k32.SetInformationJobObject.restype = wintypes.BOOL


# --------------------------------------------------------------------------
# Instancia unica
# --------------------------------------------------------------------------

_mutex = None


def single_instance(nombre="Local\\LanzadorPS5_MUTEX_v1"):
    """False si ya hay otra instancia viva.

    El handle se guarda en un global y NO se cierra nunca a proposito: el kernel
    lo libera al morir el proceso, incluso si lo matan desde el Administrador de
    tareas. Llamar a ReleaseMutex o CloseHandle a mano abriria la puerta a que
    arranque una segunda instancia mientras la primera aun vive, y dos demonios
    peleando por un dispositivo de acceso exclusivo es justo lo que hay que
    evitar.

    Limitacion conocida: el espacio de nombres Local\\ es POR SESION, asi que con
    cambio rapido de usuario cada sesion tendria su demonio. Ahi el choque se
    resuelve por el codigo de salida 3 (dispositivo ocupado).
    """
    global _mutex
    _mutex = k32.CreateMutexW(None, False, nombre)
    if not _mutex:
        log.warning("no se pudo crear el mutex: %d", ctypes.get_last_error())
        return True                      # ante la duda, dejar arrancar
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


_en_uso = None


def marcar_en_uso(nombre="Local\LanzadorPS5_EN_USO_v1"):
    """Mutex que existe solo como senal para el instalador (AppMutex de Inno).

    No es el de instancia unica y no lo sustituye: aquel dice "ya hay un
    demonio", este dice "hay algo del Lanzador vivo, no borres los archivos".

    Lo crean los TRES roles a proposito. Quien mantiene cargados los DLL de
    _internal y hace que el desinstalador no pueda borrar la carpeta es el
    REPRODUCTOR, no el demonio; sin esto, desinstalar con la ventana abierta
    dejaba media instalacion en disco y aun asi decia que todo habia ido bien.

    No se mira ERROR_ALREADY_EXISTS: aqui no importa cuantos lo tengan abierto,
    solo que el objeto exista mientras viva alguno. El handle no se cierra
    nunca, igual que en single_instance: lo suelta el kernel al morir el
    proceso, tambien si lo matan desde el Administrador de tareas.
    """
    global _en_uso
    _en_uso = k32.CreateMutexW(None, False, nombre)
    if not _en_uso:
        log.debug("no se pudo crear el mutex de uso: %d", ctypes.get_last_error())


# --------------------------------------------------------------------------
# Eventos nombrados
# --------------------------------------------------------------------------

def create_event(nombre):
    """Evento de reset MANUAL, sin senalar.

    Manual y con nombre unico por lanzamiento, no reutilizado: con un nombre
    fijo y reset manual, el siguiente hijo abriria un evento YA senalado del
    ciclo anterior y moriria al instante, en un bucle de abrir y cerrar que
    parece un fallo del dispositivo.
    """
    h = k32.CreateEventW(None, True, False, nombre)
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    return h


def open_event(nombre, acceso=SYNCHRONIZE):
    return k32.OpenEventW(acceso, False, nombre) or None


def set_event(h):
    return bool(k32.SetEvent(h))


def wait(h, ms=0):
    """True si el objeto esta senalado (o el proceso ha terminado)."""
    return k32.WaitForSingleObject(h, ms) == WAIT_OBJECT_0


def close(h):
    if h:
        k32.CloseHandle(h)


# --------------------------------------------------------------------------
# Procesos
# --------------------------------------------------------------------------

def set_priority(clase, handle=None):
    h = handle if handle is not None else k32.GetCurrentProcess()
    if not k32.SetPriorityClass(h, clase):
        log.debug("SetPriorityClass fallo: %d", ctypes.get_last_error())
        return False
    return True


def open_process(pid, acceso=SYNCHRONIZE):
    return k32.OpenProcess(acceso, False, int(pid)) or None


def create_job():
    """Job Object que mata a sus procesos al cerrarse.

    Sin esto, matar el demonio desde el Administrador de tareas -que es
    exactamente lo que hara el usuario para apagar el automatismo, y lo unico
    que puede hacer si el icono no aparece- deja una ventana sin bordes a
    pantalla completa, sin icono que la cierre, reteniendo el filtro DirectShow
    en exclusiva. Y como el mutex se libera al morir el proceso, un demonio
    nuevo arrancaria y chocaria contra su propio huerfano.
    """
    job = k32.CreateJobObjectW(None, None)
    if not job:
        log.warning("no se pudo crear el Job Object: %d", ctypes.get_last_error())
        return None

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong)]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                       ctypes.byref(info), ctypes.sizeof(info)):
        log.warning("SetInformationJobObject fallo: %d", ctypes.get_last_error())
        k32.CloseHandle(job)
        return None
    return job


def assign_to_job(job, proc_handle):
    if not job or not proc_handle:
        return False
    if not k32.AssignProcessToJobObject(job, proc_handle):
        log.warning("AssignProcessToJobObject fallo: %d", ctypes.get_last_error())
        return False
    return True


# --------------------------------------------------------------------------
# Apagado de Windows
# --------------------------------------------------------------------------

WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
GWLP_WNDPROC = -4

user32 = ctypes.WinDLL("user32", use_last_error=True)

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

_ptr = "SetWindowLongPtrW" if hasattr(user32, "SetWindowLongPtrW") else "SetWindowLongW"
_set_wndproc = getattr(user32, _ptr)
_set_wndproc.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
_set_wndproc.restype = ctypes.c_ssize_t
user32.CallWindowProcW.argtypes = (ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM)
user32.CallWindowProcW.restype = ctypes.c_ssize_t

# Las referencias a los WNDPROC tienen que sobrevivir: si el recolector se lleva
# el objeto de ctypes, Windows llamara a memoria liberada y el proceso revienta.
_subclases = []


def permitir_apagado(hwnd, al_apagar=None):
    """Hace que una ventana conteste que SI a WM_QUERYENDSESSION.

    Hace falta por un fallo de pystray: su procedimiento de ventana devuelve 0
    para todo mensaje que no maneja, en vez de delegar en DefWindowProc. Y 0 en
    respuesta a WM_QUERYENDSESSION significa "no dejes apagar", asi que Windows
    mostraria la pantalla de "esta aplicacion impide el apagado" CADA VEZ que el
    usuario apague el ordenador. Comprobado enviando el mensaje de verdad a la
    ventana y viendo que contestaba 0.

    Se sustituye su procedimiento por uno propio que contesta a los dos mensajes
    del apagado y delega el resto en el original, sin tocar la libreria.
    """
    if not hwnd:
        return False

    anterior = ctypes.c_ssize_t(0)

    def proc(h, msg, wp, lp):
        if msg == WM_QUERYENDSESSION:
            return 1                      # si, se puede apagar
        if msg == WM_ENDSESSION and wp:
            # La sesion se cierra de verdad. Aviso para cerrar lo que haga falta;
            # tiene que ser RAPIDO, porque esto corre en la bomba de mensajes.
            if al_apagar:
                try:
                    al_apagar()
                except Exception:
                    pass
            return 0
        return user32.CallWindowProcW(anterior.value, h, msg, wp, lp)

    cb = WNDPROC(proc)
    viejo = _set_wndproc(hwnd, GWLP_WNDPROC, ctypes.cast(cb, ctypes.c_void_p).value)
    if not viejo:
        log.warning("no se pudo sustituir el WndProc de 0x%X: %d",
                    hwnd, ctypes.get_last_error())
        return False
    anterior.value = viejo
    _subclases.append(cb)
    log.info("ventana 0x%X preparada para no bloquear el apagado", hwnd)
    return True


# --------------------------------------------------------------------------
# Memoria compartida
# --------------------------------------------------------------------------

PAGE_READWRITE = 0x04
FILE_MAP_ALL_ACCESS = 0xF001F

# Nombre del hueco compartido del volumen. Vive aqui, que es el modulo que
# importan tanto el reproductor como el puente de audio, para que ninguno de los
# dos tenga que importar al otro solo por una cadena.
VOL_SHM = "Local\\LanzadorPS5_VOL_v1"

k32.CreateFileMappingW.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR)
k32.CreateFileMappingW.restype = wintypes.HANDLE
k32.MapViewOfFile.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                              wintypes.DWORD, ctypes.c_size_t)
k32.MapViewOfFile.restype = ctypes.c_void_p
k32.UnmapViewOfFile.argtypes = (ctypes.c_void_p,)
k32.UnmapViewOfFile.restype = wintypes.BOOL


class SharedFloat:
    """Un float compartido entre procesos, respaldado por memoria con nombre.

    Existe para el volumen: las flechas se pulsan en el reproductor, pero las
    muestras las tiene el proceso de audio, y entre ellos no hay tuberias a
    proposito. Las alternativas se descartaron con motivo:

      - config.json: el audio tendria que releerlo, y con un sondeo de un
        segundo pulsarias cinco veces sin oir nada.
      - UDP en localhost: funciona, pero abrir un socket a la escucha dispara el
        aviso del cortafuegos de Windows la primera vez. Inaceptable para algo
        que arranca solo con el sistema.

    Leer aqui cuesta un acceso a memoria ya mapeada, asi que se puede hacer
    dentro del callback de audio sin romper la regla de no bloquear.
    """

    def __init__(self, nombre, inicial=None):
        self._h = None
        self._ptr = None
        self._buf = None
        h = k32.CreateFileMappingW(wintypes.HANDLE(-1), None, PAGE_READWRITE,
                                   0, 8, nombre)
        if not h:
            log.warning("no se pudo crear la memoria compartida %s: %d",
                        nombre, ctypes.get_last_error())
            return
        recien_creada = ctypes.get_last_error() != ERROR_ALREADY_EXISTS
        ptr = k32.MapViewOfFile(h, FILE_MAP_ALL_ACCESS, 0, 0, 8)
        if not ptr:
            log.warning("no se pudo mapear %s: %d", nombre, ctypes.get_last_error())
            k32.CloseHandle(h)
            return
        self._h = h
        self._ptr = ptr
        self._buf = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_float))
        # Solo el primero que llega fija el valor inicial; el segundo se
        # encuentra el que ya hay y no lo pisa.
        if recien_creada and inicial is not None:
            self._buf[0] = float(inicial)

    @property
    def ok(self):
        return self._buf is not None

    def get(self, por_defecto=1.0):
        if self._buf is None:
            return por_defecto
        return float(self._buf[0])

    def set(self, valor):
        if self._buf is not None:
            self._buf[0] = float(valor)

    def close(self):
        if self._ptr:
            k32.UnmapViewOfFile(self._ptr)
            self._ptr = None
        if self._h:
            k32.CloseHandle(self._h)
            self._h = None
        self._buf = None


# --------------------------------------------------------------------------
# Energia
# --------------------------------------------------------------------------

def keep_awake(activo):
    """Evita el salvapantallas y la suspension mientras se juega.

    OJO: es POR HILO. Hay que llamarlo siempre desde el hilo principal, o el
    estado se pierde cuando muere el hilo que lo pidio.

    Sin ES_AWAYMODE_REQUIRED: eso es para grabar en segundo plano con la
    pantalla apagada, no para esto.
    """
    if activo:
        estado = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
    else:
        estado = ES_CONTINUOUS
    return k32.SetThreadExecutionState(estado) != 0


def slept_ms():
    """Milisegundos que el sistema ha pasado suspendido desde el arranque.

    El truco habitual de comparar time.monotonic() con time.time() NO funciona
    en Windows: monotonic() es GetTickCount64(), que SI incluye el tiempo
    dormido, asi que la diferencia da cero. El detector real es la resta entre
    el tiempo con sesgo (incluye suspension) y el insesgado (no la incluye).
    """
    ub = ctypes.c_ulonglong(0)
    if not k32.QueryUnbiasedInterruptTime(ctypes.byref(ub)):
        return 0
    return int(k32.GetTickCount64() - ub.value // 10000)


if __name__ == "__main__":
    import os
    import core
    core.setup("winhy-test")

    print("instancia unica :", single_instance("Local\\LanzadorPS5_TEST"))
    print("  (segunda vez) :", single_instance("Local\\LanzadorPS5_TEST"))

    h = k32.GetCurrentProcess()
    print("handle proceso  : %s (si fuera 32 bits estaria truncado)" % h)
    print("prioridad actual: 0x%X" % k32.GetPriorityClass(h))

    ev = create_event("Local\\LanzadorPS5_TEST_EVENT")
    print("evento creado   :", bool(ev), "| senalado antes:", wait(ev, 0))
    set_event(ev)
    print("                   senalado despues:", wait(ev, 0))
    close(ev)

    job = create_job()
    print("job object      :", bool(job))
    close(job)

    print("suspendido      : %d ms desde el arranque" % slept_ms())
    print("keep_awake on   :", keep_awake(True))
    print("keep_awake off  :", keep_awake(False))
    print("pid propio      :", os.getpid())
