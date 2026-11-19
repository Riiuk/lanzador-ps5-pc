"""Ventana de juego: pantalla completa, teclas y overlay de diagnostico.

El hint de escalado tiene que fijarse ANTES de importar pygame. pygame lo
establece con prioridad SDL_HINT_DEFAULT, que es la mas baja, asi que la
variable de entorno gana; si se pone despues del import, no sirve de nada y el
escalado 1080p -> 1440p sale sin interpolar, a bloques.
"""

import ctypes
import os

os.environ.setdefault("SDL_RENDER_SCALE_QUALITY", "1")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import logging
import time

import cv2
import numpy as np
import pygame

log = logging.getLogger("display")

W, H = 1920, 1080

# Acciones que devuelve pump()
QUIT = "quit"
SCREENSHOT = "screenshot"
VOL_UP = "vol_up"
VOL_DOWN = "vol_down"

FLASH_S = 1.5

# Un QUIT que llegue dentro de esta ventana de tiempo tras un fallo al cambiar
# de modo lo ha generado SDL al destruir la ventana, no el usuario.
QUIT_GRACE_S = 3.0

# Segundos que el raton tiene que estar quieto sobre la ventana para que el
# cursor se retire. Vale en los dos modos: a pantalla completa se arranca sin
# cursor, pero moverlo lo devuelve y hace falta quien lo vuelva a esconder.
CURSOR_HIDE_S = 5.0

VOL_REPEAT_DELAY = 0.35     # espera antes de empezar a repetir
VOL_REPEAT = 0.08           # cadencia mientras se mantiene pulsada

# Niveles del overlay de diagnostico.
#   D         alterna OFF <-> COMPLETO   (todo: fps, tiempos, ruta, formato)
#   Ctrl+D    alterna OFF <-> FPS        (solo el contador, para jugar con el)
DIAG_OFF = 0
DIAG_FULL = 1
DIAG_FPS = 2


# ------------------------------------------------------- escala entera de SDL
#
# Ctrl+F pone la pantalla completa "nitida": 1920x1080 a escala entera, que en
# el panel de 2560x1440 es 1:1 centrado con marco negro. Estirar x1,33 obliga a
# interpolar y ablanda la imagen; a 1:1 cada pixel de la consola es un pixel
# del monitor. pygame no expone SDL_RenderSetIntegerScale, asi que se llama por
# ctypes sobre el MISMO SDL2.dll que ya tiene cargado pygame: es un interruptor
# sobre el renderer existente, sin set_mode y sin crear otro renderer.

class _SDLRect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int),
                ("w", ctypes.c_int), ("h", ctypes.c_int)]


_sdl = None


def _sdl_lib():
    """El SDL2.dll que YA tiene cargado pygame, con restype/argtypes declarados.

    No se elige una ruta: empaquetado hay dos copias (_internal y
    _internal\\pygame), y cargar la que no usa pygame daria una segunda SDL
    sin ninguna ventana. GetModuleHandleW devuelve la del proceso.
    """
    global _sdl
    if _sdl is not None:
        return _sdl
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetModuleHandleW.argtypes = (ctypes.c_wchar_p,)
    k32.GetModuleHandleW.restype = ctypes.c_void_p
    handle = k32.GetModuleHandleW("SDL2.dll")
    if not handle:
        raise RuntimeError("SDL2.dll no esta cargado en el proceso")
    lib = ctypes.CDLL("SDL2.dll", handle=handle)
    lib.SDL_GetWindowFromID.argtypes = (ctypes.c_uint32,)
    lib.SDL_GetWindowFromID.restype = ctypes.c_void_p
    lib.SDL_GetWindowFlags.argtypes = (ctypes.c_void_p,)
    lib.SDL_GetWindowFlags.restype = ctypes.c_uint32
    lib.SDL_GetRenderer.argtypes = (ctypes.c_void_p,)
    lib.SDL_GetRenderer.restype = ctypes.c_void_p
    lib.SDL_RenderSetIntegerScale.argtypes = (ctypes.c_void_p, ctypes.c_int)
    lib.SDL_RenderSetIntegerScale.restype = ctypes.c_int
    lib.SDL_RenderGetViewport.argtypes = (ctypes.c_void_p, ctypes.POINTER(_SDLRect))
    lib.SDL_RenderGetViewport.restype = None
    _sdl = lib
    return lib


SDL_WINDOW_FULLSCREEN = 0x00000001    # tambien presente en FULLSCREEN_DESKTOP


class Display:
    def __init__(self, monitor=0, vsync=False, fullscreen=True, title="PS5"):
        pygame.display.init()
        pygame.font.init()

        self.monitor = self._pick_monitor(monitor)
        self.vsync = bool(vsync)
        self.fullscreen = bool(fullscreen)
        self.sharp = False             # pantalla completa a escala entera (Ctrl+F)
        self._pgwin = None             # ver _apply_sharp: hay que mantenerlo vivo
        self.diag = DIAG_OFF

        self._title = title
        self._flash_text = ""
        self._flash_until = 0.0
        self._notices = {}             # avisos persistentes por clave
        self._mode_error_at = -1e9     # momento del ultimo fallo al cambiar de modo
        self._vol_next = 0.0           # cuando toca la siguiente repeticion
        self._cursor_on = True         # SDL arranca con el cursor visible
        self._mouse_last = 0.0         # ultimo gesto del raton

        # Buffer BGRA preasignado y superficie creada UNA sola vez.
        #
        # Este es el unico camino de datos de 373 MB/s del programa. frombuffer
        # comparte la memoria en vez de copiarla, y BGRA es el formato nativo del
        # display, asi que el blit es un memcpy y no hace falta convert() por
        # fotograma. Las alternativas obvias son ruinosas: surfarray.make_surface
        # transpone los ejes, y frombytes asigna 6,2 MB por fotograma, que es
        # justo la presion de asignador que hunde la latencia.
        self._buf = np.empty((H, W, 4), dtype=np.uint8)
        self._surface = pygame.image.frombuffer(self._buf.data, (W, H), "BGRA")

        self._font = pygame.font.Font(None, 26)
        self._screen = None
        self._apply_mode()
        pygame.display.set_caption(title)

    # ---------------------------------------------------------------- modo

    def _pick_monitor(self, requested):
        """Nunca asumir que el escritorio empieza en (0,0) ni que el indice
        pedido existe: aqui hay tres pantallas y una en coordenada X negativa."""
        try:
            sizes = pygame.display.get_desktop_sizes()
        except Exception:
            return 0
        if 0 <= requested < len(sizes):
            log.info("monitor %d de %d: %s", requested, len(sizes), sizes[requested])
            return requested
        log.warning("monitor %d no existe (hay %d), se usa el 0", requested, len(sizes))
        return 0

    def _apply_mode(self):
        """Crea la ventana. Solo se llama al construir: para alternar despues
        esta toggle_fullscreen(), que es lo unico que funciona con SCALED."""
        # SCALED mantiene la resolucion logica en 1920x1080 y deja que SDL
        # escale al panel real, con letterbox y sin deformar.
        #
        # RESIZABLE va SIEMPRE, tambien al arrancar a pantalla completa. SDL
        # decide si la ventana se puede redimensionar a partir de las banderas
        # con las que se CREO: si se crea solo con SCALED|FULLSCREEN, al volver
        # a modo ventana queda de tamano fijo y no hay forma de agrandarla ni
        # de acomodarla en un monitor secundario.
        flags = pygame.SCALED | pygame.RESIZABLE
        if self.fullscreen:
            flags |= pygame.FULLSCREEN
        try:
            self._screen = pygame.display.set_mode(
                (W, H), flags, display=self.monitor, vsync=1 if self.vsync else 0)
        except pygame.error as exc:
            # vsync=1 es legal junto a SCALED pero no esta garantizado en todos
            # los drivers; si lo rechaza, se cae sin vsync en vez de morir.
            log.warning("set_mode fallo (%s), se reintenta sin vsync", exc)
            self.vsync = False
            self._screen = pygame.display.set_mode((W, H), flags, display=self.monitor)
        self._cursor_reset()
        log.info("modo: %s, vsync=%s, driver=%s",
                 "pantalla completa" if self.fullscreen else "ventana",
                 self.vsync, pygame.display.get_driver())

    def _mode_label(self):
        if not self.fullscreen:
            return "Ventana"
        return "Pantalla completa nítida (1:1)" if self.sharp else "Pantalla completa"

    def toggle_fullscreen(self):
        """F: ventana <-> pantalla completa estirada. NUNCA lanza excepcion.

        Desde la nitida (Ctrl+F) tambien vuelve a ventana: F siempre significa
        "salir de pantalla completa" si se esta en ella.
        """
        self.sharp = False
        if self._switch_mode(not self.fullscreen):
            self._apply_sharp()
            self.flash(self._mode_label())

    def toggle_sharp(self):
        """Ctrl+F: ventana <-> pantalla completa nitida. NUNCA lanza excepcion.

        Desde la pantalla completa estirada no se cambia de modo: basta con
        activar la escala entera sobre el renderer que ya hay.
        """
        if self.fullscreen and self.sharp:
            self.sharp = False
            ok = self._switch_mode(False)
        elif self.fullscreen:
            self.sharp = True
            ok = True
        else:
            self.sharp = True
            ok = self._switch_mode(True)
            if not ok:
                self.sharp = False
        if ok and self._apply_sharp():
            self.flash(self._mode_label())

    def _switch_mode(self, want):
        """Pone la ventana en `want` (True = pantalla completa). Devuelve si lo
        ha conseguido; si no, ya ha avisado al usuario.

        Con SCALED hay que usar toggle_fullscreen(): volver a llamar a
        set_mode() destruye el renderer y SDL contesta "failed to create
        renderer". Y como esto se invoca desde el bucle de eventos, una
        excepcion aqui subia hasta arriba y cerraba la partida entera: pulsar F
        apagaba el juego en vez de ponerlo en ventana.
        """
        if want == self.fullscreen:
            return True
        # El valor de retorno de toggle_fullscreen() NO dice si ha funcionado:
        # medido, devuelve 0 en 20 de 20 cambios que SI se hicieron. Leerlo
        # como fallo ejecutaba el plan B encima en cada F, que es justo lo que
        # agota el renderer. La verdad es la bandera de la ventana de SDL.
        try:
            pygame.display.toggle_fullscreen()
        except pygame.error as exc:
            log.warning("toggle_fullscreen fallo: %s", exc)
        if self._is_fullscreen_now() == want:
            self.fullscreen = want
            surf = pygame.display.get_surface()
            if surf is not None:
                self._screen = surf
            self._cursor_reset()
            log.info("modo: %s", "pantalla completa" if self.fullscreen else "ventana")
            return True
        log.warning("toggle_fullscreen no cambio de modo")

        # Plan B: recrear la ventana con set_mode. Funciona las primeras veces,
        # pero con SCALED cada cambio destruye y recrea el renderer de SDL y
        # tras unos cuantos toggles deja de poder crearlo ("failed to create
        # renderer") y el modo se queda atascado.
        self.fullscreen = want
        try:
            self._apply_mode()
            return True
        except pygame.error as exc:
            log.warning("set_mode no pudo cambiar de modo: %s", exc)

        # Plan C: reiniciar el subsistema de video entero. Es lo unico que
        # limpia el renderer roto, y por eso existe: sin esto la ventana se
        # queda bloqueada en el modo en el que estuviera.
        if self._rebuild():
            return True

        self.fullscreen = not want
        self._mode_error_at = time.monotonic()
        log.error("no se pudo cambiar de modo por ninguna via")
        self.flash("No se ha podido cambiar entre ventana y pantalla completa", 2.5)
        return False

    def _sdl_window(self):
        """(lib SDL, puntero SDL_Window de la ventana actual). Puede lanzar.

        La referencia al Window de pygame se GUARDA. from_display_module()
        cuelga de la ventana de SDL un puntero al objeto Python que devuelve, y
        el codigo de eventos de pygame lo lee despues: si el objeto se libera,
        el siguiente event.get() es un acceso a memoria ya liberada. Medido:
        sin guardarla, access violation al primer o tercer cambio de modo;
        guardandola, ninguno.
        """
        sdl = _sdl_lib()
        from pygame._sdl2.video import Window
        self._pgwin = Window.from_display_module()
        return sdl, sdl.SDL_GetWindowFromID(self._pgwin.id)

    def _is_fullscreen_now(self):
        """Estado REAL de la ventana segun SDL, o None si no se puede leer
        (y entonces el que llama no da el cambio por bueno)."""
        try:
            sdl, win = self._sdl_window()
            if not win:
                return None
            return bool(sdl.SDL_GetWindowFlags(win) & SDL_WINDOW_FULLSCREEN)
        except Exception as exc:
            log.warning("no se pudo leer el modo de la ventana: %s", exc)
            return None

    def _apply_sharp(self):
        """Aplica la escala entera que toque al renderer ACTUAL.

        Hay que llamarla tras CUALQUIER cambio de modo: medido, pygame la
        devuelve a 0 al volver a pantalla completa aunque el renderer sea el
        mismo, y set_mode o reiniciar el video crean uno nuevo sin ella. En
        ventana va siempre apagada: SCALED ya da 1:1 a 1920x1080 y con ella
        encendida, agrandar la ventana no estiraria la imagen.

        Devuelve False (y avisa) solo si se pidio la nitida y no se pudo.
        """
        on = self.sharp and self.fullscreen
        try:
            sdl, win = self._sdl_window()
            ren = sdl.SDL_GetRenderer(win) if win else None
            if not ren:
                raise RuntimeError("la ventana no tiene renderer de SDL")
            if sdl.SDL_RenderSetIntegerScale(ren, 1 if on else 0) != 0:
                raise RuntimeError("SDL_RenderSetIntegerScale fallo")
            vp = _SDLRect()
            sdl.SDL_RenderGetViewport(ren, ctypes.byref(vp))
            log.info("escala entera %s, viewport (%d,%d,%d,%d)",
                     "ON" if on else "OFF", vp.x, vp.y, vp.w, vp.h)
            return True
        except Exception as exc:
            log.warning("no se pudo cambiar la escala entera: %s", exc)
            if not on:
                return True    # apagarla es lo que ya hay si nunca se encendio
            self.sharp = False
            self.flash("No se ha podido activar la pantalla completa nítida", 2.5)
            return False

    def _rebuild(self):
        """Reinicia el subsistema de video y rehace la ventana.

        Es la unica forma de recuperarse de un renderer de SDL corrupto. La
        superficie de pixeles no se toca: viene de frombuffer sobre un bufer
        propio, asi que sobrevive al reinicio.
        """
        try:
            pygame.display.quit()
            pygame.display.init()
            self._apply_mode()
            pygame.display.set_caption(self._title)
            surf = pygame.display.get_surface()
            if surf is not None:
                self._screen = surf
            if self.sharp:
                self._apply_sharp()    # el renderer nuevo nace sin ella
            log.info("ventana reconstruida")
            return True
        except pygame.error as exc:
            log.error("no se pudo reconstruir la ventana: %s", exc)
            self._mode_error_at = time.monotonic()
            return False

    # -------------------------------------------------------------- cursor

    def _cursor(self, visible):
        """Muestra u oculta el cursor, y SOLO cuando el estado cambia.

        Llamar a set_visible en cada fotograma seria pedirle a SDL sesenta
        veces por segundo que no cambie nada.
        """
        if visible == self._cursor_on:
            return
        self._cursor_on = visible
        try:
            pygame.mouse.set_visible(visible)
        except pygame.error as exc:
            log.debug("set_visible fallo: %s", exc)

    def _cursor_reset(self):
        """Estado del cursor tras crear o cambiar la ventana.

        En ventana se deja visible y con el reloj a cero, para que volver de
        pantalla completa no aparezca ya sin cursor; que lo esconda la
        inactividad. A pantalla completa se entra ya sin cursor, sin esperar los
        cinco segundos: ahi no hay nada que senalar.
        """
        self._mouse_last = time.monotonic()
        self._cursor(not self.fullscreen)

    # ------------------------------------------------------------- eventos

    def pump(self):
        """Devuelve la lista de acciones pedidas por el usuario."""
        actions = []
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                # QUIT no es lo mismo que ESC, aunque los dos cierren. Cuando el
                # renderer se rompe al cambiar de modo, SDL destruye la ventana y
                # manda un QUIT que NO ha pedido nadie: registrarlo como "cerrado
                # por el usuario" es mentir en el log y cerrar la partida sin
                # motivo. Si viene justo despues de un fallo de modo se intenta
                # reconstruir la ventana y se sigue jugando.
                if time.monotonic() - self._mode_error_at < QUIT_GRACE_S:
                    log.warning("QUIT de SDL tras un fallo de modo: se ignora y "
                                "se reconstruye la ventana")
                    if self._rebuild():
                        self.flash("Ventana recuperada tras un fallo de vídeo", 2.0)
                        continue
                    log.error("no se pudo reconstruir la ventana, se cierra")
                log.info("QUIT de SDL (ventana cerrada)")
                actions.append(QUIT)
            elif ev.type == pygame.VIDEORESIZE:
                # Con SCALED el escalado lo hace SDL y la superficie logica
                # sigue siendo 1920x1080, asi que no hay que redibujar nada;
                # basta refrescar la referencia por si SDL la ha sustituido.
                surf = pygame.display.get_surface()
                if surf is not None:
                    self._screen = surf
                # pygame recalcula la escala al redimensionar y se ha medido
                # que la devuelve a 0 al volver a pantalla completa: por si un
                # cambio que no hemos pedido nosotros hace lo mismo.
                if self.sharp:
                    self._apply_sharp()
            elif ev.type in (pygame.MOUSEMOTION, pygame.MOUSEBUTTONDOWN,
                             pygame.MOUSEWHEEL):
                self._mouse_last = time.monotonic()
                self._cursor(True)
            elif ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_ESCAPE:
                    log.info("ESC pulsado por el usuario")
                    actions.append(QUIT)
                elif ev.key == pygame.K_f:
                    if ev.mod & pygame.KMOD_CTRL:
                        self.toggle_sharp()
                    else:
                        self.toggle_fullscreen()
                elif ev.key == pygame.K_d:
                    # Cada tecla alterna su propio nivel contra OFF, asi que
                    # pulsar la que ya esta activa lo apaga y pulsar la otra
                    # salta directamente al otro nivel.
                    if ev.mod & pygame.KMOD_CTRL:
                        self.diag = DIAG_OFF if self.diag == DIAG_FPS else DIAG_FPS
                    else:
                        self.diag = DIAG_OFF if self.diag == DIAG_FULL else DIAG_FULL
                elif ev.key == pygame.K_p:
                    actions.append(SCREENSHOT)
                elif ev.key in (pygame.K_UP, pygame.K_KP8):
                    actions.append(VOL_UP)
                elif ev.key in (pygame.K_DOWN, pygame.K_KP2):
                    actions.append(VOL_DOWN)

        # Repeticion al mantener pulsada la flecha, para no tener que dar
        # veinte toques. pygame.key.set_repeat() tambien lo haria, pero afecta a
        # TODAS las teclas, y mantener F pulsada estaria alternando pantalla
        # completa cincuenta veces por segundo. Asi la repeticion es solo del
        # volumen.
        ahora = time.monotonic()
        pulsadas = pygame.key.get_pressed()
        arriba = pulsadas[pygame.K_UP] or pulsadas[pygame.K_KP8]
        abajo = pulsadas[pygame.K_DOWN] or pulsadas[pygame.K_KP2]

        if arriba != abajo:                       # exactamente una de las dos
            if VOL_UP in actions or VOL_DOWN in actions:
                self._vol_next = ahora + VOL_REPEAT_DELAY   # acaba de pulsarse
            elif self._vol_next and ahora >= self._vol_next:
                actions.append(VOL_UP if arriba else VOL_DOWN)
                self._vol_next = ahora + VOL_REPEAT
        else:
            self._vol_next = 0.0

        # SDL solo manda MOUSEMOTION de la ventana con foco, asi que mover el
        # raton por otro monitor no devuelve el cursor: justo lo que se quiere
        # mientras juegas.
        # El temporizador corre TAMBIEN a pantalla completa. Cualquier
        # MOUSEMOTION saca el cursor, y mientras esto miraba solo el modo
        # ventana no quedaba nadie que lo retirase: el primer movimiento del
        # raton dejaba el puntero encima del juego el resto de la partida.
        if ahora - self._mouse_last >= CURSOR_HIDE_S:
            self._cursor(False)

        return actions

    # -------------------------------------------------------------- avisos

    def flash(self, text, seconds=FLASH_S):
        self._flash_text = text
        self._flash_until = time.monotonic() + seconds

    def notice(self, text, key="general"):
        """Aviso persistente, con clave para que puedan convivir varios.

        Hacen falta al menos dos a la vez: SIN AUDIO y SIN SENAL son
        independientes y pueden darse juntos. Con un solo hueco, el segundo
        borraba al primero y el usuario perdia la mitad de la informacion justo
        cuando peor le venia.
        """
        if text:
            self._notices[key] = text
        else:
            self._notices.pop(key, None)

    # -------------------------------------------------------------- pintar

    def present(self, frame, stats=None):
        """Convierte, pinta y presenta. Devuelve (convert_ms, blit_ms)."""
        t0 = time.perf_counter()
        cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA, dst=self._buf)
        t1 = time.perf_counter()

        self._screen.blit(self._surface, (0, 0))
        self._draw_overlay(stats)
        pygame.display.flip()
        t2 = time.perf_counter()

        return (t1 - t0) * 1000.0, (t2 - t1) * 1000.0

    def _draw_overlay(self, stats):
        lines = []
        if stats and self.diag == DIAG_FULL:
            lines.append("%.1f fps  |  read %.1f ms  convert %.1f ms  blit+flip %.1f ms" % (
                stats.get("fps", 0.0), stats.get("read_ms", 0.0),
                stats.get("convert_ms", 0.0), stats.get("blit_ms", 0.0)))
            extra = stats.get("extra")
            if extra:
                lines.append(extra)
        elif stats and self.diag == DIAG_FPS:
            lines.append("%.1f fps" % stats.get("fps", 0.0))
        for _, texto in sorted(self._notices.items()):
            lines.append(texto)
        if self._flash_text and time.monotonic() < self._flash_until:
            lines.append(self._flash_text)
        elif self._flash_text:
            self._flash_text = ""

        if not lines:
            return

        pad = 8
        surfaces = [self._font.render(t, True, (255, 255, 255)) for t in lines]
        w = max(s.get_width() for s in surfaces) + pad * 2
        h = sum(s.get_height() for s in surfaces) + pad * 2
        box = pygame.Surface((w, h))
        box.set_alpha(190)
        box.fill((0, 0, 0))
        self._screen.blit(box, (24, 24))
        y = 24 + pad
        for s in surfaces:
            self._screen.blit(s, (24 + pad, y))
            y += s.get_height()

    def close(self):
        try:
            pygame.display.quit()
        except Exception:
            pass
        pygame.quit()


if __name__ == "__main__":
    import core
    core.setup("display-test")
    core.set_dpi_aware()
    d = Display(monitor=core.get("monitor"), vsync=core.get("vsync"), fullscreen=False)
    d.diag = DIAG_FULL
    frame = np.zeros((H, W, 3), np.uint8)
    running, i = True, 0
    conv = blit = 0.0
    print("F alterna, Ctrl+F pantalla completa nitida 1:1, D diagnostico completo, Ctrl+D solo fps, P aviso, ESC sale")
    while running:
        i += 1
        frame[:] = 0
        cv2.putText(frame, "PRUEBA DE VENTANA  %d" % i, (120, 540),
                    cv2.FONT_HERSHEY_SIMPLEX, 3, (0, 200, 255), 6)
        conv, blit = d.present(frame, {"fps": 60.0, "read_ms": 0.0,
                                       "convert_ms": conv, "blit_ms": blit})
        for a in d.pump():
            if a == QUIT:
                running = False
            elif a == SCREENSHOT:
                d.flash("captura simulada")
    d.close()
