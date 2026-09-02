@echo off
rem ---------------------------------------------------------------------
rem  Lanzador PS5 PC - doble clic para jugar.
rem
rem  %~dp0 es la carpeta de este archivo, con la barra final. Todo se
rem  resuelve contra ella para que funcione desde cualquier sitio y no
rem  dependa de en que carpeta este la consola.
rem
rem  Se usa pythonw.exe, no python.exe, para que no quede una ventana de
rem  consola detras del juego. Si algo falla, el programa lo detecta y lo
rem  avisa con un cuadro de dialogo, ademas de dejarlo en el registro de
rem  %LOCALAPPDATA%\LanzadorPS5\.
rem
rem  Arranca en ventana; la pantalla completa se pone con F. Para cambiar el
rem  comportamiento por defecto esta start_fullscreen en config.json, y para
rem  una vez suelta bastan los argumentos, que se pasan tal cual a player.py:
rem      jugar.cmd --fullscreen
rem      jugar.cmd --format mjpg
rem ---------------------------------------------------------------------

if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    echo.
    echo  No se encuentra el entorno del proyecto:
    echo      %~dp0.venv
    echo.
    echo  Para crearlo, ejecuta estas dos ordenes en esta carpeta:
    echo      py -3.9 -m venv .venv
    echo      .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

start "Lanzador PS5" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0player.py" %*
