# Lanzador PS5 PC — contexto del proyecto

Muestra una PS5 en una ventana del PC mediante una capturadora USB. Abre y
cierra la ventana sola al encender y apagar la consola, arranca con Windows y
vive en la bandeja del sistema.

**Estado: terminado y en uso.** Repo público en
<https://github.com/Riiuk/lanzador-ps5-pc>. Instalador con Inno Setup listo.
Lo que venga ahora son mejoras sobre algo que ya funciona, no construcción.

Hardware del usuario: capturadora **ALBURAN HYSD-88** (chip MacroSilicon MS2130,
USB 3.0), PS5 en la LAN, tres monitores (el principal 2560×1440, dos de 1080p).
Windows 11, Python 3.9.3 en un venv dentro del proyecto.

---

## Arquitectura: tres procesos, y ninguno por gusto

```
DEMONIO (--tray)  →  REPRODUCTOR (--player)  →  PUENTE DE AUDIO (--audio)
bandeja, red,        ventana pygame,             anillo + remuestreo
máquina de estados   captura cv2
```

Cada separación responde a una medida, no a una preferencia:

| Medida | Por qué obliga |
|---|---|
| Un hilo Python compitiendo lleva el p99 del callback de audio de **380 µs a 64 ms** | El audio no puede compartir intérprete |
| `TerminateProcess` no completa si un hilo está bloqueado dentro del driver | Quien abre la capturadora debe ser un hijo desechable |
| `VideoCapture_DShow` hace `CoInitialize` en el hilo del constructor | `open`/`grab`/`retrieve`/`release` van todos en el mismo hilo |

El demonio **nunca** importa `cv2` ni `sounddevice`. IPC: padre→hijo por Event
nombrado (manual-reset, nombre único por lanzamiento), hijo→padre por **código de
salida**. Nada de tuberías: con `console=False` no hay stdout y un `PIPE` sin
lector bloquea al hijo al llenar 64 KB.

---

## Hechos medidos — no re-derivar ni "optimizar"

Todo esto costó tiempo de medir. Si algo parece un número arbitrario, no lo es.

- **La PS5 responde a UDP 9302** (descubrimiento de Remote Play) en 103 ms.
  `200` = encendida, `620` = reposo. **En reposo también responde**, así que hay
  que exigir el código 200. Se parsea el número, no la frase.
- **La capturadora NO re-enumera** al perder HDMI: es bus-powered.
  `LastRemovalDate` vacío, cero eventos Kernel-PnP en 30 días. Por eso el
  disparador es la red y no el dispositivo.
- **Sin señal la tarjeta sigue dando 1080p**, en negro puro (`std` 0,00). Ni el
  cambio de resolución ni `retrieve()==False` sirven de detector aquí.
- **Un negro no distingue "consola apagada" de "pantalla de carga del juego".**
  Medido en GT7: los negros de menús duran 3,0-4,5 s. Por eso `FLAT_NOTICE=10 s`
  y por eso perder la señal **avisa y espera**, no cierra.
- **MJPG se ve.** Comprimido deja rayado horizontal en degradados oscuros. YUY2
  sin comprimir son 249 MB/s y caben de sobra: **60,0 fps sostenidos**.
- **Audio**: `usbaudio.sys` entrega bloques de **480 frames exactos** cada
  10,00 ms, máximo 12,89, **sin ráfagas**. Deriva real entre relojes: **7-23 ppm**
  frente a un tope reservado de 2000. `target_ms=30`.
- **Remuestreador verificado** con tono puro: −76,6 dB de error en el peor caso.
  El suelo es la cuantización de fase en 512 pasos, no un defecto.
- **Coste de pintado**: `convert` 1,4 ms + `blit+flip` 3,8 ms = 5,2 de 16,6 ms.
  Por eso la conversión a BGRA se quedó en el hilo principal.
- **Tiempos reales**: desde reposo la ventana abre en **1-2 s**; desde apagado
  ~17 s, de los cuales **~15 son la PS5 arrancando**. Cerrar es casi instantáneo.

---

## Trampas: cosas que ya mordieron

Cada una costó una sesión de depuración. Ninguna da un error claro.

**Vídeo / OpenCV**
- `cap.read()` es `if(grab()) retrieve(img); return !img.empty()` — **ignora el
  bool de `retrieve()`**. Sin HDMI devuelve `(True, memoria sin inicializar)`.
  Usar siempre `grab()` + `retrieve()`.
- `set(CAP_PROP_FPS)` tras el constructor con `params` **tira la resolución y el
  FOURCC**. Y FOURCC antes que ancho/alto pierde el MJPG en silencio.
- `get(CAP_PROP_FOURCC)` **miente**: devuelve una variable cacheada. La verdad es
  `frame.shape` y los fps contados.
- **El índice de OpenCV no es estable** si hay cámaras virtuales (aquí conviven
  iVCam y OBS Virtual Camera). Se ha visto a la capturadora en el 0 y en el 1 el
  mismo día. Agarrar el equivocado **no da error, da otra imagen**. `dshow.py`
  lo corrige por `DevicePath` en 7 ms. El orden de la enumeración COM **sí**
  coincide con el índice de OpenCV (verificado en el mismo proceso).
- `cv2.imwrite` **falla en silencio** con rutas no ASCII. Usar `imencode` +
  `write_bytes`.

**Configuración**
- `core.load_config()` cachea **una copia por proceso**, y `save_config` vuelca
  el diccionario entero. Con tres procesos escribiendo el mismo `config.json`,
  el último revertía lo que hubieran guardado los otros: el reproductor guardaba
  el volumen y el demonio lo devolvía al valor que leyó al arrancar. Por eso
  `save_config` relee con `force=True` dentro del cerrojo. Medido con dos
  procesos: 40 volvía a 100.

**Audio**
- `np.take` con `mode='raise'` está documentado como **siempre buffereado**
  aunque se pase `out=`. Usar `mode='clip'`.
- Cambiar la ganancia de golpe **chasquea**: hay que rampearla dentro del bloque.
- Hay **dos salidas "Sound Blaster Z"**; ante nombre ambiguo `sounddevice`
  devuelve −1 **en silencio** y abre el dispositivo por defecto, que en captura
  es el micrófono.

**Windows / ctypes**
- Declarar **siempre** `restype` y `argtypes`. Un `HANDLE` truncado hace que
  `SetEvent` no llegue nunca al hijo, sin ningún error.
- No llamar `_stop` a un atributo de una subclase de `threading.Thread`: pisa
  `Thread._stop()` y `join()` revienta.
- **pystray devuelve 0 a todo mensaje que no maneja** y nunca llama a
  `DefWindowProc`. Ante `WM_QUERYENDSESSION` eso significa "no dejes apagar", y
  Windows sacaría la pantalla de «esta aplicación impide el apagado» en cada
  apagado. Se corrige sustituyendo su WndProc (`winhy.permitir_apagado`).
- Las casillas del menú necesitan `icon.update_menu()` o la marca se congela.
- El globo de la bandeja **repite el nombre de la aplicación** si se le pasa
  `szInfoTitle`: Windows ya pone arriba el nombre del ejecutable. Y no se
  puede pedir un título vacío por `icon.notify()`, que hace
  `title or self.title` y cae en el tooltip multilínea. Se manda el
  `NIF_INFO` a mano (`daemon._notificar`). Medido: sin título el globo se
  muestra igual.
- `SCALED` + `set_mode` repetido agota el renderer de SDL: hay tres planes para
  cambiar de modo, y `toggle_fullscreen()` devuelve 0 **siempre** en esta máquina.
- **`{userpics}` no existe en Inno Setup**, y `{userprofile}\Pictures` es peor:
  no da error pero apunta mal si Imágenes está redirigida. Aquí lo está
  (`D:\Pictures`). Leer `Shell Folders\My Pictures` del registro.
- Los comentarios `{ }` de Pascal Script **no anidan**.
- En el `.iss`, **ninguna línea puede empezar por `#`**, ni con espacios delante:
  el preprocesador la toma por una directiva suya y aborta con «Unknown
  preprocessor directive». Los `#13#10` van al final de la línea anterior.
- **Sin `AppMutex`, desinstalar con el programa abierto no da ningún error**:
  Inno borra lo que puede, los DLL de `_internal` se quedan porque están
  cargados, y la desinstalación se da por buena. Lo marcan los **tres** procesos
  (`winhy.marcar_en_uso`), no solo el demonio: quien tiene los DLL abiertos es
  el reproductor. Verificado que el desinstalador **elevado sí ve un mutex del
  espacio `Local\`**, así que no hace falta uno `Global\`.

**PowerShell 5.1**
- `2>&1` sobre un ejecutable nativo envuelve cada línea en un registro de error y
  pone `$?` a falso **aunque devuelva 0**. Rompe compilaciones correctas.
- **`& $exe` no espera** a una aplicación sin consola. Usar
  `Start-Process -Wait -PassThru` y mirar `ExitCode`.
- El intérprete `pythonw.exe` del venv es un **redirector**: lanza el del sistema
  y aparecen dos procesos. No es un bug y desaparece con el `.exe`.

---

## Archivos

| Archivo | Qué hace |
|---|---|
| `ps5.py` | Router de argv. Sin argumentos = `--tray`. Despacha antes de imports pesados |
| `core.py` | Rutas frozen-aware, config, logging por rol, excepthooks, `child_argv` |
| `capture.py` | Apertura verificada de DirectShow, hilo de captura, reconexión |
| `display.py` | Ventana pygame, teclas, overlay |
| `player.py` | Proceso reproductor: capturas, volumen, hijo de audio, códigos de salida |
| `audio.py` | Puente: anillo SPSC, sinc32 polifásico, control P de deriva |
| `ps5net.py` | Descubrimiento UDP 9302 multi-interfaz |
| `dshow.py` | Enumeración COM de DirectShow por `DevicePath` |
| `winhy.py` | Win32 por ctypes: mutex, Events, Job Object, memoria compartida, apagado |
| `daemon.py` | Máquina de estados, bandeja, arbitraje de hijos |
| `autostart.py` | `HKCU\...\Run` con la semántica del bit 0 de `StartupApproved` |

**Datos de usuario** en `%LOCALAPPDATA%\LanzadorPS5\`: `config.json`, un log por
rol (`-tray`, `-player`, `-audio`), `selftest.log`. **Nunca** junto a `__file__`:
empaquetado eso es temporal. Lo elegido al instalar va en `defaults.json` junto
al ejecutable, para que valga para cualquier usuario del equipo.

**Códigos de salida del reproductor**: 0 señal perdida · 1 error interno ·
2 sin señal · 3 dispositivo ocupado · 4 capturadora ausente · 5 imagen plana ·
6 fallo de audio · 10 ESC · 20 parada pedida por el padre.

---

## Construir y probar

```
.\build.ps1                 # .exe en dist\LanzadorPS5\
.\build.ps1 -Acceso         # y acceso directo
.\build.ps1 -Instalador     # y el instalador (Inno Setup 6)
```

El script comprueba el **canario de PortAudio** en el log —si falta, el `.exe`
funciona pero sale mudo— y que el ejecutable **arranca de verdad**
(`--selftest`). Compilar sin errores no demuestra ninguna de las dos.

Otros modos útiles: `--setup` (busca la consola), `--audio --measure` (mide el
patrón del driver), `--selftest`, `--player --windowed --format mjpg`, y
`--purge-data`, que borra los datos de usuario y lo llama el desinstalador.

---

## Cómo trabajar aquí

- **Medir antes de escribir.** Es lo que ha funcionado en todo el proyecto, y
  varias veces la medida contradijo la intuición: `dshow.py` resultó viable
  cuando parecía inútil, y `WM_QUERYENDSESSION` resultó necesario cuando parecía
  innecesario. Si una pieza se justifica por una suposición, medirla primero.
- **Nada de fallos silenciosos.** El patrón que más ha dolido: el programa hace
  lo correcto, sale, y el usuario ve que "no pasa nada". Todo camino de error
  visible para el usuario tiene que decir qué pasa y qué hacer.
- **Tandas de 4-5 archivos como máximo**, y modo plan antes de cualquier
  mecánica nueva. Es regla del usuario.
- Comentarios en el código: explican **por qué**, sobre todo cuando lo obvio
  sería otra cosa. Sin acentos en el código, con acentos en los textos que ve el
  usuario.

## Pendiente / ideas

- Verificado en un reinicio real (2026-09-04): arranca solo con Windows y
  apagar no saca la pantalla de «esta aplicación impide el apagado».
- Publicado como *Release* en GitHub: `v1.0.2`, marcado latest, con el
  instalador adjunto.
- Verificado en el `.exe` instalado (1.0.2): globo sin el nombre repetido,
  cursor que se retira a los 5 s, volumen que aguanta entre sesiones y
  desinstalación que se niega a empezar con el programa abierto.
- No implementado a propósito: control de cambio rápido de usuario (irrelevante
  en un PC doméstico de un usuario).
- `jugar.cmd` usa el código fuente, no el `.exe`: útil para probar sin recompilar.
