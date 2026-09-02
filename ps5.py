"""Punto de entrada unico: encamina segun el modo y no hace nada mas.

Este archivo tiene que ser ABURRIDO. Es el primero que se ejecuta en los tres
procesos, y el despacho ocurre ANTES de importar nada pesado por dos razones:

  - El demonio no debe cargar jamas cv2 ni sounddevice. No los necesita, y
    cargarlos le costaria memoria y tiempo de arranque para nada.
  - Empaquetado, sys.executable ES este propio programa, asi que lanzar un hijo
    significa volver a entrar aqui. El flag de argv es lo unico que corta la
    recursion, y por eso se mira lo primero.

No hace falta multiprocessing.freeze_support(): los hijos se lanzan con
subprocess, no con multiprocessing.
"""

import sys

MODOS = ("--tray", "--player", "--audio", "--setup", "--selftest",
         "--autostart-on", "--autostart-off")

AYUDA = """Lanzador PS5 PC

  ps5.py --tray      residente en la bandeja; detecta la consola y abre solo
  ps5.py --player    abre la ventana de juego ahora
  ps5.py --audio     solo el puente de audio (lo lanza el reproductor)
  ps5.py --setup     busca la consola en la red y guarda la configuracion

Sin argumentos arranca en modo --tray, que es la forma normal de usarlo: se
queda en la bandeja y abre la ventana solo cuando enciendes la PS5. Cada modo
acepta sus propias opciones:
  ps5.py --player --windowed --format mjpg
  ps5.py --audio  --measure 30
"""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    if "-h" in argv or "--help" in argv:
        print(AYUDA)
        return 0

    modo = None
    for i, a in enumerate(argv):
        if a in MODOS:
            modo = a
            argv.pop(i)
            break

    if modo in ("--autostart-on", "--autostart-off"):
        # Los usa el instalador. NO puede escribir el la clave Run el mismo: corre
        # elevado, asi que su HKCU es el del usuario que acepto el UAC, que no
        # tiene por que ser quien va a usar el programa. Inno lo lanza con
        # runasoriginaluser para que esto se ejecute como el usuario de verdad.
        import core
        import autostart
        core.setup("autostart")
        ok = autostart.enable() if modo == "--autostart-on" else autostart.disable()
        return 0 if ok else 1

    if modo == "--selftest":
        # Comprobacion de humo para el .exe recien construido. Que PyInstaller
        # termine sin errores NO prueba que el ejecutable funcione: lo que falla
        # de verdad es que en tiempo de ejecucion falte una DLL o un modulo que
        # el analisis estatico no vio. Aqui se importa todo y se mira que la
        # parte nativa responda, que es donde suele romperse.
        # Los resultados van a un fichero ademas de a stdout: empaquetado con
        # console=False NO HAY stdout, asi que sin esto un fallo del .exe seria
        # un codigo de salida pelado y nada mas que mirar.
        lineas = []

        def di(txt):
            lineas.append(txt)
            print(txt)

        fallos = []
        try:
            import numpy
            import cv2
            di("  cv2         %s | numpy %s" % (cv2.__version__, numpy.__version__))
            cv2.VideoWriter.fourcc("M", "J", "P", "G")
        except Exception as exc:
            fallos.append("cv2/numpy: %s" % exc)
        try:
            import pygame
            pygame.display.init()
            di("  pygame      %s | pantallas %d"
               % (pygame.version.ver, len(pygame.display.get_desktop_sizes())))
            pygame.display.quit()
        except Exception as exc:
            fallos.append("pygame: %s" % exc)
        try:
            import sounddevice
            # La DLL de PortAudio es el fallo silencioso clasico del empaquetado:
            # el .exe arranca igual y simplemente no suena.
            di("  sounddevice %s" % sounddevice.get_portaudio_version()[1][:34])
            if not any("WASAPI" in a["name"].upper()
                       for a in sounddevice.query_hostapis()):
                fallos.append("sounddevice: no aparece el host API WASAPI")
        except Exception as exc:
            fallos.append("sounddevice/PortAudio: %s" % exc)
        try:
            import pystray
            from PIL import Image  # noqa: F401
            di("  pystray     ok | notificaciones: %s" % pystray.Icon.HAS_NOTIFICATION)
        except Exception as exc:
            fallos.append("pystray/PIL: %s" % exc)

        import core
        try:
            di("  rutas       app=%s" % core.app_dir())
            di("  congelado   %s" % core.frozen())
        except Exception as exc:
            fallos.append("core: %s" % exc)

        for f in fallos:
            di("  FALLO: %s" % f)
        di("RESULTADO: %s" % ("hay fallos" if fallos else "todo correcto"))

        try:
            import io
            with io.open(str(core.data_dir() / "selftest.log"), "w",
                         encoding="utf-8") as fh:
                fh.write("\n".join(lineas) + "\n")
        except Exception:
            pass
        return 1 if fallos else 0

    # Sin modo explicito se arranca el residente, NO el reproductor.
    #
    # El doble clic en el icono del escritorio no lleva argumentos, y con el
    # reproductor por defecto pasaba esto: la PS5 estaba apagada, el reproductor
    # se negaba a abrir ventana -correctamente- y salia en silencio. Para el
    # usuario, "he hecho doble clic y no pasa nada". La aplicacion ES el
    # residente; abrir el juego en ese preciso instante es el caso raro.
    if modo in (None, "--tray"):
        import daemon
        return daemon.main(argv)

    if modo == "--audio":
        import audio
        return audio.main(argv)

    if modo == "--setup":
        import core
        import ps5net
        core.setup("setup")
        encontradas = ps5net.discover()
        if not encontradas:
            print("No se ha encontrado ninguna PS5 en la red.")
            print("Comprueba que esta encendida o en reposo con la red activada.")
            return 2
        for i, info in enumerate(encontradas):
            print("[%d] %s  %s  host-id %s" % (i, info.get("host-name", "?"),
                                               info["ip"], info.get("host-id", "?")))
        elegida = encontradas[0]
        if len(encontradas) > 1:
            print("\nHay mas de una; se guarda la primera. Edita config.json para"
                  " cambiarla.")
        core.save_config(ps5_ip=elegida["ip"], ps5_host_id=elegida.get("host-id", ""))
        print("\nGuardado: %s en %s" % (elegida.get("host-name"), elegida["ip"]))
        return 0

    import player
    return player.main(argv)          # modo --player


if __name__ == "__main__":
    sys.exit(main())
