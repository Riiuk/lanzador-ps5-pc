"""Arranque con Windows mediante HKCU\\...\\CurrentVersion\\Run.

Se eligio Run frente a las alternativas por motivos concretos:

  - Tarea programada: su XML no aparece en Administrador de tareas > Inicio, asi
    que el usuario no puede desactivarla desde donde espera hacerlo.
  - Acceso directo .lnk en shell:startup: hay que crearlo por COM (IShellLink +
    IPersistFile), sesenta lineas de vtable fragil para lo mismo.
  - HKLM: requiere elevacion y afecta a todos los usuarios. Nunca.

Run son cinco lineas de winreg, no pide UAC, y sale en el gestor de inicio de
Windows donde el usuario lo espera. Ademas es el patron dominante en esta
maquina: 17 de sus entradas usan /silent, --hidden o --minimized.
"""

import logging
import os
import sys
import winreg

import core

log = logging.getLogger("autostart")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
NOMBRE = "LanzadorPS5"


def comando():
    """Linea que se registra. Siempre entrecomillada y absoluta.

    Con pythonw.exe y no python.exe cuando no esta congelado: si no, al iniciar
    sesion parpadearia una consola negra en la cara del usuario.
    """
    if core.frozen():
        return '"%s" --tray' % sys.executable
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    return '"%s" "%s" --tray' % (pyw, core.app_dir() / "ps5.py")


def _leer():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            valor, _ = winreg.QueryValueEx(k, NOMBRE)
            return valor
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _vetado():
    """True si Windows tiene la entrada DESHABILITADA desde el gestor de inicio.

    La semantica del blob es por bits, no por valores concretos: 0x02 y 0x06
    estan habilitados, 0x03 deshabilitado. Comprobarlo como
    `data[0] not in (0x00, 0x02)` daria 0x06 por deshabilitado, que es falso.
    Lo que manda es el BIT 0.

    Importa mas de lo que parece: este veto SOBREVIVE al borrado del valor de
    Run. En esta maquina hay 18 vetos huerfanos de programas ya desinstalados,
    asi que activar el autoarranque escribiendo solo en Run puede no bastar.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as k:
            data, _ = winreg.QueryValueEx(k, NOMBRE)
            if data and len(data) > 0:
                return bool(data[0] & 1)
    except (FileNotFoundError, OSError):
        pass
    return False


def _quitar_veto():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, NOMBRE)
            log.info("veto de StartupApproved eliminado")
    except (FileNotFoundError, OSError):
        pass


def is_enabled():
    return _leer() is not None and not _vetado()


def enable():
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.SetValueEx(k, NOMBRE, 0, winreg.REG_SZ, comando())
        _quitar_veto()
        log.info("autoarranque activado: %s", comando())
        return True
    except OSError as exc:
        log.error("no se pudo activar el autoarranque: %s", exc)
        return False


def disable():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, NOMBRE)
        log.info("autoarranque desactivado")
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.error("no se pudo desactivar el autoarranque: %s", exc)
        return False
    return True


def heal():
    """Reapunta la entrada si la carpeta del proyecto ha cambiado de sitio.

    Sin esto, mover la carpeta deja una entrada que apunta a una ruta que ya no
    existe: Windows la ejecuta, falla en silencio y el usuario cree que el
    programa se ha roto.
    """
    actual = _leer()
    if actual is None:
        return False
    esperado = comando()
    if actual != esperado:
        log.warning("la ruta de autoarranque ha cambiado; se corrige")
        log.warning("  antes : %s", actual)
        log.warning("  ahora : %s", esperado)
        return enable()
    return False


if __name__ == "__main__":
    core.setup("autostart-test")
    accion = sys.argv[1] if len(sys.argv) > 1 else "estado"
    if accion == "on":
        enable()
    elif accion == "off":
        disable()
    elif accion == "heal":
        heal()

    print("comando que se registraria:")
    print("   ", comando())
    print()
    print("valor en el registro :", _leer() or "(no existe)")
    print("vetado por Windows   :", _vetado())
    print("ACTIVADO             :", is_enabled())
    print()
    print("uso: autostart.py [on|off|heal|estado]")
