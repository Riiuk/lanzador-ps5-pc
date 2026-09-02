# Lanzador PS5 PC

Juega a la PS5 en una ventana del PC. Enciendes la consola y la ventana **se abre
sola**; la apagas y **se cierra sola**. Sin abrir OBS, sin navegar menús, sin
tocar nada.

Un programa propio en Python para tarjetas capturadoras USB genéricas, escrito
porque las alternativas fallaban en cosas concretas:

- **PotPlayer** reproducía bien el vídeo, pero el audio daba petardazos y se
  desincronizaba a los pocos minutos.
- **OBS Studio** arreglaba el audio, pero automatizarlo era imposible:
  `--startfullscreen` daba errores de idioma, abría instancias múltiples y no
  desplegaba el proyector limpio.

---

## Qué hace

- **Vídeo 1920×1080 a 60 fps** sin comprimir (YUY2), en una ventana sin menús.
- **Audio estable**, con compensación de deriva de reloj entre la capturadora y
  la tarjeta de sonido del PC.
- **Detección automática** de la consola por red: abre y cierra la ventana solo.
- **Arranque con Windows**, residente en la bandeja con un consumo despreciable.
- **Capturas de pantalla** a resolución nativa con una tecla.
- **Control de volumen** con las flechas.

## Teclas

| Tecla | Acción |
|---|---|
| <kbd>F</kbd> | Alterna pantalla completa y ventana |
| <kbd>ESC</kbd> | Cierra la partida |
| <kbd>↑</kbd> <kbd>↓</kbd> | Volumen |
| <kbd>P</kbd> | Captura de pantalla en `Screenshots/` |
| <kbd>D</kbd> | Diagnóstico: fps y tiempos de cada etapa |
| <kbd>Ctrl</kbd>+<kbd>D</kbd> | Solo el contador de fps |

---

## Las tres decisiones que definen el proyecto

### El disparador es la red, no la capturadora

La idea intuitiva es detectar cuándo la capturadora recibe señal HDMI. **No
funciona**: la tarjeta se alimenta del bus USB, así que perder la señal no la
retira del sistema. Medido: `LastRemovalDate` vacío y cero eventos Kernel-PnP de
retirada en 30 días.

Lo que sí funciona es preguntarle a la consola. La PS5 responde al protocolo de
descubrimiento de Remote Play en **UDP 9302**:

```
-> SRCH * HTTP/1.1
   device-discovery-protocol-version:00030010

<- HTTP/1.1 200 Ok        (encendida)
   HTTP/1.1 620 Server Standby   (en reposo)
```

103 ms de respuesta, coste de CPU despreciable, y **sin tocar el dispositivo**,
que es de acceso exclusivo. La distinción entre `200` y `620` importa: en reposo
la consola también responde, pero no hay señal HDMI.

### El audio va en su propio proceso

Los petardazos tenían una causa concreta: el driver de estas capturadoras es
`usbaudio`, **USB Audio Class 1.0** genérico, un endpoint isócrono sin feedback
fiable. Su reloj de muestreo y el de la tarjeta de sonido del PC difieren en
decenas de partes por millón, así que a lo largo de los minutos uno adelanta al
otro: el búfer se vacía (corte) o se desborda (clic).

La solución es un puente con remuestreo adaptativo. Y tiene que ir en **otro
proceso**, no en un hilo: medido, con un hilo de Python compitiendo el percentil
99 del callback de audio pasa de **380 µs a 64 ms**, seis veces por encima del
presupuesto.

```
[capturadora] -> anillo SPSC -> remuestreador sinc32 -> [altavoces]
                                        ^
                                  ratio ajustado por un control P
                                  según el nivel de relleno del anillo
```

El remuestreo es de una parte por diez mil —se consumen 1,0001 muestras de
entrada por cada muestra de salida— e inaudible, pero suficiente para que los
dos relojes no se separen nunca. Medido con un tono puro: **−76,6 dB** de error
en el peor caso, cuando −60 dB ya no se percibe.

### Tres procesos, y no por gusto

| Restricción medida | Consecuencia |
|---|---|
| Un hilo Python competidor lleva el p99 del callback de audio a 64 ms | El audio no puede compartir intérprete |
| `TerminateProcess` no completa si un hilo está bloqueado dentro del driver | Quien abre la capturadora debe ser un hijo desechable |
| `VideoCapture_DShow` hace `CoInitialize` en el hilo del constructor | `open`/`grab`/`retrieve`/`release` van todos en el mismo hilo |

```
DEMONIO (bandeja)  ->  REPRODUCTOR (ventana)  ->  PUENTE DE AUDIO
```

El demonio nunca importa `cv2` ni `sounddevice`. Si el reproductor se queda
bloqueado dentro del driver, se le mata y el demonio sigue vivo.

---

## Requisitos

- Windows 10 u 11
- Python 3.9 o superior
- Una capturadora USB UVC que entregue 1080p60 (probado con **ALBURAN HYSD-88**,
  chip MacroSilicon MS2130)
- La PS5 y el PC en la misma red

## Instalación

### Con el instalador

Descarga `LanzadorPS5-x.y.z-instalador.exe` de la sección *Releases* y ejecútalo.
Te preguntará dos cosas:

1. **Dónde instalar**, con `C:\Program Files\Lanzador PS5` como sugerencia.
2. **Dónde guardar las capturas**, con `Imágenes\Screenshots PS5` por defecto.

La segunda pregunta no es un capricho: `Program Files` es de **solo lectura**
para el usuario, así que las capturas no pueden guardarse junto al programa. La
carpeta que elijas se crea durante la instalación y queda registrada en
`defaults.json`, dentro de la carpeta del programa, de forma que vale para
cualquier usuario del equipo. Cada uno puede cambiarla luego en su `config.json`.

También puedes marcar que arranque con Windows. Esa casilla no la escribe el
instalador: **lanza el propio programa** para que lo haga. Un instalador corre
elevado y su `HKEY_CURRENT_USER` es el del usuario que aceptó el aviso de UAC,
que no tiene por qué ser quien va a usar el programa.

### Desde el código

```
git clone https://github.com/Riiuk/lanzador-ps5-pc.git
cd lanzador-ps5-pc
py -3.9 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe ps5.py --setup
```

`--setup` busca la consola en la red y guarda su identificador.

## Uso

```
.venv\Scripts\pythonw.exe ps5.py --tray
```

Aparece el icono en la bandeja y ya está: enciende la PS5 y la ventana se abre
sola. Desde el menú del icono puedes activar el arranque con Windows.

Otros modos:

```
ps5.py --player            abre la ventana ahora
ps5.py --player --windowed en ventana en vez de pantalla completa
ps5.py --audio --measure   mide el patrón de entrega del driver de audio
ps5.py --selftest          comprueba que todas las dependencias cargan
```

## Compilar

```
.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\build.ps1                 # solo el .exe
.\build.ps1 -Acceso         # y un acceso directo en el escritorio
.\build.ps1 -Instalador     # y el instalador (necesita Inno Setup 6)
```

Genera `dist\LanzadorPS5\LanzadorPS5.exe` (~184 MB) y, con `-Instalador`, un
`.exe` de instalación de ~49 MB.

El script comprueba tres cosas antes de dar el resultado por bueno: que el icono
se genera, que **PortAudio queda empaquetado** —si no, el `.exe` funciona pero
sale mudo— y que el ejecutable **arranca de verdad** y encuentra sus
dependencias. Compilar sin errores no demuestra ninguna de las tres.

---

## Configuración

`%LOCALAPPDATA%\LanzadorPS5\config.json`. Los valores útiles:

| Clave | Por defecto | Qué hace |
|---|---|---|
| `video_format` | `"yuy2"` | `"mjpg"` comprime y **se nota** en los degradados oscuros |
| `target_ms` | `30` | Búfer de audio. Súbelo si aparecen cortes |
| `monitor` | `0` | En una pantalla de 1080p el blit es 1:1, sin escalar |
| `start_fullscreen` | `false` | También se cambia desde el menú de la bandeja |
| `av_offset_ms` | `0.0` | Ajuste fino del desfase entre imagen y sonido |

Los registros están en esa misma carpeta, uno por proceso.

## Si algo no va

**Se ve granulado en las zonas oscuras.** Estás en MJPG. Pon
`"video_format": "yuy2"`.

**Pantalla negra con la consola encendida.** HDCP. En la PS5,
*Ajustes → Sistema → HDMI → Habilitar HDCP* en OFF, y la salida en 1080p SDR.

**No detecta la consola.** En la PS5, *Ajustes → Sistema → Ahorro de energía →
Funciones disponibles en modo de reposo → Mantener conexión a Internet* activado.

**Se abre la ventana de otra cámara.** El índice de OpenCV no es estable si hay
cámaras virtuales instaladas. El programa lo corrige solo comparando el
`DevicePath`; si tu capturadora no es la del proyecto, ajusta
`cam_device_path` con el identificador USB de la tuya.

**No hay sonido.** Ejecuta `ps5.py --selftest`. Si no aparece el host API
WASAPI, falta la DLL de PortAudio.

## Limitaciones conocidas

- Solo Windows: usa DirectShow, WASAPI y bastante Win32 por `ctypes`.
- Pensado para 1080p60. Si la consola sale a 2160p o HDR, la capturadora puede
  no entregar nada.
- Con la consola apagada del todo, la ventana tarda ~17 s en aparecer, pero
  **15 de esos segundos son la PS5 arrancando**. Desde reposo: 1-2 segundos.

## Licencia

MIT.
