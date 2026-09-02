# Construye LanzadorPS5.exe.
#
#   .\build.ps1
#
# Todo va en una sola linea por orden: nada de continuaciones. Y OJO, el
# caracter de continuacion de PowerShell es la comilla invertida, NO el
# circunflejo de cmd.exe: un ^ al final de linea aqui es un error de sintaxis.

$ErrorActionPreference = "Stop"
$raiz = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $raiz ".venv\Scripts\python.exe"
$log = Join-Path $raiz "build.log"

Write-Host "== Lanzador PS5 - construccion ==" -ForegroundColor Cyan

if (-not (Test-Path $py)) { Write-Host "No existe $py. Crea el entorno primero." -ForegroundColor Red; exit 1 }

# Se invoca SIEMPRE como "python -m PyInstaller" y con la ruta del venv, nunca
# el ejecutable pyinstaller suelto: asi es imposible construir sin querer con el
# interprete del sistema, que no tiene las dependencias fijadas.
$prefix = & $py -c "import sys; print(sys.prefix)"
Write-Host "interprete : $py"
Write-Host "entorno    : $prefix"
if ($prefix -notlike "*$raiz*") { Write-Host "El interprete no apunta al venv del proyecto." -ForegroundColor Red; exit 1 }

Write-Host ""; Write-Host "-- 1/4 icono --" -ForegroundColor Yellow
& $py (Join-Path $raiz "make_icon.py")
if ($LASTEXITCODE -ne 0) { Write-Host "Fallo al generar el icono." -ForegroundColor Red; exit 1 }

Write-Host ""; Write-Host "-- 2/4 PyInstaller --" -ForegroundColor Yellow
# PyInstaller escribe su registro por stderr, y en PowerShell 5.1 redirigir el
# stderr de un ejecutable nativo envuelve CADA LINEA en un registro de error y
# pone $? a falso aunque el programa devuelva 0. Con ErrorActionPreference en
# Stop, eso aborta una construccion perfectamente correcta. Se baja a Continue
# solo aqui y se comprueba el codigo de salida a mano, que es lo unico fiable.
$ErrorActionPreference = "Continue"
# El ForEach-Object convierte cada registro de error en texto plano ANTES de que
# PowerShell lo pinte en rojo. Sin el, la salida normal de PyInstaller aparece
# como si fueran errores y no hay forma de distinguir un fallo real.
& $py -m PyInstaller --clean --noconfirm (Join-Path $raiz "LanzadorPS5.spec") 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $log
$rc = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($rc -ne 0) { Write-Host "PyInstaller ha fallado (codigo $rc). Mira $log" -ForegroundColor Red; exit 1 }

Write-Host ""; Write-Host "-- 3/4 canario de PortAudio --" -ForegroundColor Yellow
# Este aviso concreto significa que el .exe se ha construido SIN la DLL de
# PortAudio: se ejecutara igual, pero saldra mudo. Y quedarse sin sonido es
# justo el problema que motivo todo el proyecto, asi que se comprueba siempre.
$canario = Select-String -Path $log -Pattern "portaudio shared library not found" -SimpleMatch -Quiet
if ($canario) { Write-Host "AVISO GRAVE: falta la DLL de PortAudio, el .exe NO TENDRA SONIDO." -ForegroundColor Red } else { Write-Host "ok: PortAudio empaquetado" -ForegroundColor Green }

$exe = Join-Path $raiz "dist\LanzadorPS5\LanzadorPS5.exe"
if (-not (Test-Path $exe)) { Write-Host "No se ha generado $exe" -ForegroundColor Red; exit 1 }

Write-Host ""; Write-Host "-- 4/4 humo: que arranque de verdad --" -ForegroundColor Yellow
# Construir sin errores no prueba nada: lo que hay que saber es si el .exe
# encuentra sus dependencias en tiempo de ejecucion. --selftest las importa
# todas y sale con 0.
# Start-Process -Wait y NO "& $exe": el ejecutable se construye sin consola, y
# a una aplicacion de ventanas PowerShell no la espera. Con "&" la orden volveria
# al instante y $LASTEXITCODE seria el de otra cosa, asi que la prueba de humo
# habria dado por bueno cualquier resultado, incluido un .exe que no arranca.
$smoke = Start-Process -FilePath $exe -ArgumentList "--selftest" -Wait -PassThru
if ($smoke.ExitCode -ne 0) { Write-Host "El .exe se construyo pero NO ARRANCA (codigo $($smoke.ExitCode))." -ForegroundColor Red; Write-Host "Detalle en: $env:LOCALAPPDATA\LanzadorPS5\selftest.log" -ForegroundColor Red; exit 1 }
Write-Host "ok: el .exe importa todo lo que necesita" -ForegroundColor Green
Get-Content (Join-Path $env:LOCALAPPDATA "LanzadorPS5\selftest.log") -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "   $_" -ForegroundColor DarkGray }

$tam = (Get-ChildItem (Join-Path $raiz "dist\LanzadorPS5") -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB
Write-Host ""; Write-Host ("LISTO: {0}  ({1:N0} MB)" -f $exe, $tam) -ForegroundColor Green

if ($args -contains "-Instalador") {
    Write-Host ""; Write-Host "-- instalador --" -ForegroundColor Yellow
    $iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) { Write-Host "Falta Inno Setup 6. Instalalo con: winget install JRSoftware.InnoSetup" -ForegroundColor Red; exit 1 }
    $ErrorActionPreference = "Continue"
    & $iscc (Join-Path $raiz "installer.iss") 2>&1 | ForEach-Object { "$_" } | Select-Object -Last 6
    $rc = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($rc -ne 0) { Write-Host "Inno Setup ha fallado (codigo $rc)." -ForegroundColor Red; exit 1 }
    $inst = Get-ChildItem (Join-Path $raiz "dist") -Filter "LanzadorPS5-*-instalador.exe" | Select-Object -First 1
    if ($inst) { Write-Host ("INSTALADOR: {0}  ({1:N0} MB)" -f $inst.FullName, ($inst.Length / 1MB)) -ForegroundColor Green }
}

Write-Host ""
Write-Host "Opciones: .\build.ps1 -Acceso        acceso directo en el escritorio" -ForegroundColor DarkGray
Write-Host "          .\build.ps1 -Instalador    genera el instalador .exe" -ForegroundColor DarkGray

if ($args -contains "-Acceso") {
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "Lanzador PS5.lnk"))
    $lnk.TargetPath = $exe
    # --tray explicito aunque ya sea el modo por defecto: el acceso directo deja
    # el residente escuchando, que es como se usa esto. Sin argumentos se
    # lanzaba el reproductor, y con la consola apagada salia en silencio: desde
    # fuera parecia que el doble clic no hacia nada.
    $lnk.Arguments = "--tray"
    $lnk.WorkingDirectory = Split-Path -Parent $exe
    $lnk.IconLocation = "$exe,0"
    $lnk.Description = "Deja el Lanzador PS5 escuchando en la bandeja"
    $lnk.Save()
    Write-Host "Acceso directo creado en el escritorio." -ForegroundColor Green
}
