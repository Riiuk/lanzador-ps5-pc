"""Enumeracion de dispositivos DirectShow por COM, para saber QUIEN es cada indice.

OpenCV no expone nombres: solo acepta un numero. Y ese numero no es estable,
porque depende del orden en que Windows enumera la categoria de captura de
video, y ese orden cambia cuando se registra o se quita una camara virtual. En
esta maquina conviven la capturadora, e2eSoft iVCam y OBS Virtual Camera, y se
ha visto a la capturadora aparecer como indice 0 y como indice 1 en dias
distintos.

Agarrar el indice equivocado NO da error: da la imagen de otro dispositivo. Ya
paso una vez, midiendo durante media hora el cartel "Please run iVCam" creyendo
que era la capturadora sin senal.

VERIFICADO ANTES DE ESCRIBIR ESTO: el orden que devuelve esta enumeracion COM
coincide con el indice que usa OpenCV. Se comprobo ejecutando las dos
enumeraciones en el mismo proceso y comparando cual daba 1920x1080. Aviso
honesto sobre la fuerza de la prueba: solo un dispositivo de los tres pudo
abrirse en OpenCV (las camaras virtuales no abren si sus aplicaciones no estan
corriendo), asi que es una coincidencia confirmada, no tres. La teoria la
respalda: el backend de OpenCV recorre la misma categoria de la misma forma.

Por eso todo lo de aqui es MEJOR ESFUERZO: si COM falla o no encuentra el
dispositivo, capture.py sigue teniendo su sondeo, que funciona igual aunque mas
despacio.
"""

import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger("dshow")

ole32 = ctypes.WinDLL("ole32")
oleaut32 = ctypes.WinDLL("oleaut32")

CLSCTX_INPROC_SERVER = 1
VT_BSTR = 8

# Indices en la vtable. IMoniker hereda de IPersistStream, que hereda de
# IPersist: por eso BindToStorage cae en la posicion 9 y no en la 4.
IDX_RELEASE = 2
IDX_CREATE_CLASS_ENUMERATOR = 3
IDX_ENUM_NEXT = 3
IDX_BIND_TO_STORAGE = 9
IDX_BAG_READ = 3


class GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort),
                ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]

    def __init__(self, txt):
        super().__init__()
        ole32.CLSIDFromString(ctypes.c_wchar_p(txt), ctypes.byref(self))


class VARIANT(ctypes.Structure):
    # 24 bytes en x64: vt, tres reservados y una union de 16.
    _fields_ = [("vt", ctypes.c_ushort), ("w1", ctypes.c_ushort),
                ("w2", ctypes.c_ushort), ("w3", ctypes.c_ushort),
                ("val", ctypes.c_void_p), ("val2", ctypes.c_void_p)]


CLSID_SystemDeviceEnum = GUID("{62BE5D10-60EB-11d0-BD3B-00A0C911CE86}")
IID_ICreateDevEnum = GUID("{29840822-5B84-11D0-BD3B-00A0C911CE86}")
CLSID_VideoInputDeviceCategory = GUID("{860BB310-5D01-11d0-BD3B-00A0C911CE86}")
IID_IPropertyBag = GUID("{55272A00-42CB-11CE-8135-00AA004BB851}")


def _metodo(ptr, indice, restype, *argtypes):
    """Saca un metodo de la vtable de una interfaz COM.

    ctypes no sabe de COM, asi que hay que leer el puntero a la vtable, indexar
    y construir el prototipo a mano. El primer argumento siempre es `this`.
    """
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0]
    fn = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[indice]
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(fn)


def _release(ptr):
    if ptr:
        _metodo(ptr, IDX_RELEASE, ctypes.c_ulong)(ptr)


def enumerar():
    """Dispositivos de captura de video, en el orden que usa OpenCV.

    Devuelve [{"index": n, "name": str, "path": str}]. Lista vacia si COM falla:
    quien llama debe apanarselas sin esto.
    """
    salida = []
    ole32.CoInitializeEx(None, 0)

    dev_enum = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(CLSID_SystemDeviceEnum), None,
                                CLSCTX_INPROC_SERVER,
                                ctypes.byref(IID_ICreateDevEnum),
                                ctypes.byref(dev_enum))
    if hr != 0 or not dev_enum:
        log.warning("CoCreateInstance fallo: 0x%08X", hr & 0xFFFFFFFF)
        return salida

    try:
        enum_mon = ctypes.c_void_p()
        crear = _metodo(dev_enum, IDX_CREATE_CLASS_ENUMERATOR, ctypes.c_long,
                        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                        ctypes.c_ulong)
        hr = crear(dev_enum, ctypes.byref(CLSID_VideoInputDeviceCategory),
                   ctypes.byref(enum_mon), 0)
        if hr != 0 or not enum_mon:
            # S_FALSE con enum_mon nulo significa "no hay ningun dispositivo".
            return salida

        try:
            siguiente = _metodo(enum_mon, IDX_ENUM_NEXT, ctypes.c_long,
                                ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p),
                                ctypes.POINTER(ctypes.c_ulong))
            i = 0
            while True:
                mon = ctypes.c_void_p()
                traidos = ctypes.c_ulong(0)
                if siguiente(enum_mon, 1, ctypes.byref(mon),
                             ctypes.byref(traidos)) != 0 or not traidos.value:
                    break
                try:
                    datos = _leer_moniker(mon)
                    if datos is not None:
                        datos["index"] = i
                        salida.append(datos)
                finally:
                    _release(mon)
                i += 1
        finally:
            _release(enum_mon)
    finally:
        _release(dev_enum)

    return salida


def _leer_moniker(mon):
    bolsa = ctypes.c_void_p()
    bind = _metodo(mon, IDX_BIND_TO_STORAGE, ctypes.c_long, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    if bind(mon, None, None, ctypes.byref(IID_IPropertyBag),
            ctypes.byref(bolsa)) != 0 or not bolsa:
        return None
    try:
        leer = _metodo(bolsa, IDX_BAG_READ, ctypes.c_long, ctypes.c_wchar_p,
                       ctypes.POINTER(VARIANT), ctypes.c_void_p)

        def prop(nombre):
            v = VARIANT()
            oleaut32.VariantInit(ctypes.byref(v))
            texto = ""
            try:
                if leer(bolsa, nombre, ctypes.byref(v), None) == 0 and v.vt == VT_BSTR:
                    if v.val:
                        texto = ctypes.wstring_at(v.val)
            finally:
                oleaut32.VariantClear(ctypes.byref(v))
            return texto

        return {"name": prop("FriendlyName"), "path": prop("DevicePath")}
    finally:
        _release(bolsa)


def resolver(patron):
    """Indice del dispositivo cuyo DevicePath contiene `patron`, o None.

    El DevicePath lleva el identificador de fabricante y producto del USB
    (vid_345f&pid_2131 para esta capturadora), asi que identifica el aparato de
    verdad y no depende del orden ni del nombre, que pueden cambiar.
    """
    if not patron:
        return None
    patron = patron.lower()
    for d in enumerar():
        if patron in (d.get("path") or "").lower():
            return d["index"]
    return None


def coincide(indice, patron):
    """True si `indice` apunta al dispositivo esperado. None si no se sabe.

    Devolver None y no False cuando COM no puede confirmarlo es deliberado: el
    que llama debe distinguir "he comprobado que esta mal" de "no he podido
    comprobarlo", y en el segundo caso seguir adelante en vez de bloquearse.
    """
    if not patron:
        return None
    lista = enumerar()
    if not lista:
        return None
    for d in lista:
        if d["index"] == indice:
            return patron.lower() in (d.get("path") or "").lower()
    return False        # el indice ni siquiera existe


if __name__ == "__main__":
    import time

    import core
    core.setup("dshow-test")

    t0 = time.perf_counter()
    disp = enumerar()
    ms = (time.perf_counter() - t0) * 1000

    patron = core.get("cam_device_path")
    print("enumeracion COM en %.1f ms:" % ms)
    for d in disp:
        marca = "  <-- LA CAPTURADORA" if patron.lower() in (d["path"] or "").lower() else ""
        ruta = d["path"] or "(sin DevicePath)"
        print("  [%d] %-24s %s%s" % (d["index"], d["name"],
                                     ruta[:56] + ("..." if len(ruta) > 56 else ""),
                                     marca))
    print()
    print("patron buscado    :", patron)
    print("indice resuelto   :", resolver(patron))
    print("indice en config  :", core.get("cam_index"))
    print("el de config vale :", coincide(core.get("cam_index"), patron))
