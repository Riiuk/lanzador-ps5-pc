# -*- mode: python ; coding: utf-8 -*-
"""Receta de construccion del .exe. Se ejecuta con:

    .venv\\Scripts\\python.exe -m PyInstaller --clean --noconfirm LanzadorPS5.spec

El .spec es la UNICA fuente de verdad. Cuando se construye desde un .spec,
PyInstaller IGNORA los flags de linea de ordenes (--exclude-module, --add-data,
--icon, --noconsole...), asi que todo tiene que estar aqui dentro o no se aplica.
"""

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable,
    VarFileInfo, VarStruct, VSVersionInfo)

VERSION = (1, 0, 2, 0)
VERSION_TXT = "1.0.2.0"

# Esto NO es decorativo. El nombre que Windows muestra en los globos de
# notificacion sale del FileDescription del ejecutable, no del titulo que se le
# pase a la API. Ejecutando bajo pythonw.exe las notificaciones ponian "Python";
# con esto ponen "Lanzador PS5".
version_res = VSVersionInfo(
    ffi=FixedFileInfo(filevers=VERSION, prodvers=VERSION, mask=0x3F, flags=0x0,
                      OS=0x40004, fileType=0x1, subtype=0x0),
    kids=[
        StringFileInfo([StringTable("040A04B0", [   # 040A = espanol, 04B0 = UTF-16
            StringStruct("CompanyName", "Abel Santiago Fuentes"),
            StringStruct("FileDescription", "Lanzador PS5"),
            StringStruct("FileVersion", VERSION_TXT),
            StringStruct("InternalName", "LanzadorPS5"),
            StringStruct("OriginalFilename", "LanzadorPS5.exe"),
            StringStruct("ProductName", "Lanzador PS5"),
            StringStruct("ProductVersion", VERSION_TXT),
            StringStruct("LegalCopyright", "Copyright (c) 2026 Abel Santiago Fuentes"),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x040A, 1200])]),
    ])

# Lista de exclusiones.
#
# SOLO paquetes de terceros pesados. Se quitaron a proposito email, unittest y
# xml.dom, que estaban en un borrador anterior: son de la biblioteca estandar y
# los importan http.client, urllib.request, importlib.metadata y pkg_resources.
# Excluirlos ahorraba 0,4 MB sobre ~190 y a cambio provocaba un ImportError en
# tiempo de ejecucion dentro de un .exe SIN CONSOLA, o sea invisible.
#
# Casi todos estos ni siquiera estan en el venv; van como red por si algun dia se
# construye con el interprete equivocado.
EXCLUIR = [
    "tkinter", "_tkinter", "matplotlib", "scipy", "pandas", "pyarrow",
    "altair", "streamlit", "PyQt5", "PyQt6", "PySide2", "PySide6",
    "IPython", "pytest", "notebook", "jupyter",
]

a = Analysis(
    ["ps5.py"],
    pathex=[],
    binaries=[],
    datas=[("assets", "assets")],
    # Cinturon y tirantes: hook-pystray.py hace collect_submodules("pystray"),
    # asi que en teoria sobra, pero el backend de Windows se elige en tiempo de
    # ejecucion y no cuesta nada asegurarlo.
    hiddenimports=["pystray._win32"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUIR,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,       # onedir: los binarios van aparte, en COLLECT
    name="LanzadorPS5",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                   # UPX dispara heuristicas de antivirus
    console=False,               # sin ventana de consola: vive en la bandeja
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/ps5.ico",
    version=version_res,
)

# onedir y no onefile, y no por lo que suele decirse.
#
# El motivo real: en onefile, el proceso lanzador BORRA el directorio temporal
# _MEIxxxxx cuando termina el proceso principal, y este diseno contempla
# expresamente un hijo que sobrevive al padre (el estado "dispositivo
# envenenado"). A ese hijo se le borrarian las DLL en caliente. Ademas el
# bootloader de un hijo onefile comprueba el proceso padre con OpenProcess y
# aborta si ya no existe.
#
# Como extra, onedir arranca al instante en lugar de reextraer ~190 MB en cada
# arranque, y esto es un programa que se inicia con Windows.
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LanzadorPS5",
)
