"""Ventana de juego: pantalla completa, teclas y overlay de diagnostico.

El hint de escalado tiene que fijarse ANTES de importar pygame. pygame lo
establece con prioridad SDL_HINT_DEFAULT, que es la mas baja, asi que la
variable de entorno gana; si se pone despues del import, no sirve de nada y el
escalado 1080p -> 1440p sale sin interpolar, a bloques.
"""

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
# cursor se retire. A pantalla completa esta oculto siempre.
CURSOR_HIDE_S = 5.0

VOL_REPEAT_DELAY = 0.35     # espera antes de empezar a repetir
VOL_REPEAT = 0.08           # cadencia mientras se mantiene pulsada

# Niveles del overlay de diagnostico.
#   D         alterna OFF <-> COMPLETO   (todo: fps, tiempos, ruta, formato)
#   Ctrl+D    alterna OFF <-> FPS        (solo el contador, para jugar con el)
DIAG_OFF = 0
DIAG_FULL = 1
DIAG_FPS = 2


class Display:
    def __init__(self, monitor=0, vsync=False, fullscreen=True, title="PS5"):
        pygame.display.init()
        pygame.font.init()

        self.monitor = self._pick_monitor(monitor)
        self.vsync = bool(vsync)
        self.fullscreen = bool(fullscreen)
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

    def toggle_fullscreen(self):
        """Alterna pantalla completa y ventana. NUNCA lanza excepcion.

        Con SCALED hay que usar toggle_fullscreen(): volver a llamar a
        set_mode() destruye el renderer y SDL contesta "failed to create
        renderer". Y como esto se invoca desde el bucle de eventos, una
        excepcion aqui subia hasta arriba y cerraba la partida entera: pulsar F
        apagaba el juego en vez de ponerlo en ventana.
        """
        want = not self.fullscreen
        try:
            if pygame.display.toggle_fullscreen():
                self.fullscreen = want
                surf = pygame.display.get_surface()
                if surf is not None:
                    self._screen = surf
                self._cursor_reset()
                self.flash("Pantalla completa" if self.fullscreen else "Ventana")
                log.info("modo: %s", "pantalla completa" if self.fullscreen else "ventana")
                return
            log.warning("toggle_fullscreen no pudo cambiar de modo")
        except pygame.error as exc:
            log.warning("toggle_fullscreen fallo: %s", exc)

        # Plan B: recrear la ventana con set_mode. Funciona las primeras veces,
        # pero con SCALED cada cambio destruye y recrea el renderer de SDL y
        # tras unos cuantos toggles deja de poder crearlo ("failed to create
        # renderer") y el modo se queda atascado.
        self.fullscreen = want
        try:
            self._apply_mode()
            self.flash("Pantalla completa" if self.fullscreen else "Ventana")
            return
        except pygame.error as exc:
            log.warning("set_mode no pudo cambiar de modo: %s", exc)

        # Plan C: reiniciar el subsistema de video entero. Es lo unico que
        # limpia el renderer roto, y por eso existe: sin esto la ventana se
        # queda bloqueada en el modo en el que estuviera.
        if self._rebuild():
            self.flash("Pantalla completa" if self.fullscreen else "Ventana")
            return

        self.fullscreen = not want
        self._mode_error_at = time.monotonic()
        log.error("no se pudo cambiar de modo por ninguna via")
        self.flash("No se ha podido cambiar entre ventana y pantalla completa", 2.5)

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
        inactividad. A pantalla completa, oculto y punto.
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
            elif ev.type in (pygame.MOUSEMOTION, pygame.MOUSEBUTTONDOWN,
                             pygame.MOUSEWHEEL):
                self._mouse_last = time.monotonic()
                self._cursor(True)
            elif ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_ESCAPE:
                    log.info("ESC pulsado por el usuario")
                    actions.append(QUIT)
                elif ev.key == pygame.K_f:
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
        if not self.fullscreen and ahora - self._mouse_last >= CURSOR_HIDE_S:
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
    print("F alterna, D diagnostico completo, Ctrl+D solo fps, P aviso, ESC sale")
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
