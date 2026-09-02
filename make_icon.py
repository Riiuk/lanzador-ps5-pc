"""Convierte icon.jpg en assets/ps5.ico con todos los tamanos que pide Windows.

Un .ico no se puede escribir a mano: es un contenedor binario con varias
resoluciones dentro, y sin el PyInstaller aborta al pasarle --icon.

El origen es un JPEG, y ahi esta la gracia del asunto: el JPEG NO TIENE CANAL
ALFA, asi que lo que en el diseno original era transparente viene grabado como
un damero gris de verdad. Convertirlo tal cual dejaria el icono con cuadritos
alrededor.

Y no vale con "todo lo gris fuera": el cuerpo de la PS5 es blanco, y una regla
asi le haria agujeros a la consola. Lo que distingue el fondo del contenido no
es el color, es que el fondo esta CONECTADO AL BORDE. Por eso se rellena desde
las esquinas hacia dentro, y lo que queda rodeado de azul sobrevive.
"""

import os
import sys

import numpy as np
from PIL import Image

TAMANOS = [16, 24, 32, 48, 64, 128, 256]

# El damero es gris claro: sus tres canales casi iguales y brillo alto.
GRIS_TOL = 14        # diferencia maxima entre canales para considerarlo gris
GRIS_MIN = 195       # por debajo de esto ya no es damero, es contenido oscuro
GRIS_TOL_SUAVE = 42  # segunda pasada: el JPEG deja tinte de color en los bordes
GRIS_MIN_SUAVE = 170
MARGEN = 6           # pixeles de margen alrededor del recorte


def _grises(a, tol, minimo):
    maxc = a.max(axis=2).astype(np.int16)
    minc = a.min(axis=2).astype(np.int16)
    return ((maxc - minc) <= tol) & (minc >= minimo)


def _expandir(semilla, permitido, limite=3000):
    """Crece la semilla por vecindad, sin salirse de `permitido`.

    Dilataciones sucesivas en numpy en vez de un relleno recursivo: cada paso es
    una operacion sobre todo el array y converge en unos cientos de iteraciones,
    mientras que ir pixel a pixel en Python puro sobre medio millon se
    arrastraria.
    """
    fondo = semilla & permitido
    for _ in range(limite):
        anterior = fondo.sum()
        crecido = fondo.copy()
        crecido[1:, :] |= fondo[:-1, :]
        crecido[:-1, :] |= fondo[1:, :]
        crecido[:, 1:] |= fondo[:, :-1]
        crecido[:, :-1] |= fondo[:, 1:]
        fondo = crecido & permitido
        if fondo.sum() == anterior:
            break
    return fondo


def mascara_fondo(a):
    """Fondo = damero CONECTADO al borde, en dos pasadas.

    La primera es estricta y establece con seguridad que es fondo. La segunda
    afloja la tolerancia para barrer el anillo de pixeles que la compresion JPEG
    dejo con un tinte de color y que la primera no reconocia como grises.

    La segunda pasada es segura precisamente porque parte de la primera: solo
    crece desde lo que YA se sabe que es fondo, asi que no puede colarse hacia el
    interior. El cuerpo blanco de la PS5 esta rodeado de azul y sigue a salvo.
    """
    borde = np.zeros(a.shape[:2], dtype=bool)
    borde[0, :] = borde[-1, :] = True
    borde[:, 0] = borde[:, -1] = True

    fondo = _expandir(borde, _grises(a, GRIS_TOL, GRIS_MIN))
    return _expandir(fondo, _grises(a, GRIS_TOL_SUAVE, GRIS_MIN_SUAVE))


def construir(origen, destino):
    im = Image.open(origen).convert("RGB")
    a = np.asarray(im)

    fondo = mascara_fondo(a)
    alfa = np.where(fondo, 0, 255).astype(np.uint8)

    ys, xs = np.where(alfa > 0)
    if not len(ys):
        raise SystemExit("no se ha encontrado ningun contenido en %s" % origen)
    y0 = max(0, ys.min() - MARGEN)
    y1 = min(a.shape[0], ys.max() + 1 + MARGEN)
    x0 = max(0, xs.min() - MARGEN)
    x1 = min(a.shape[1], xs.max() + 1 + MARGEN)

    rgba = np.dstack([a, alfa])[y0:y1, x0:x1]
    recorte = Image.fromarray(rgba)

    # Cuadrado y centrado: un .ico no cuadrado sale deformado en la bandeja.
    lado = max(recorte.size)
    lienzo = Image.new("RGBA", (lado, lado), (0, 0, 0, 0))
    lienzo.paste(recorte, ((lado - recorte.size[0]) // 2,
                           (lado - recorte.size[1]) // 2))

    os.makedirs(os.path.dirname(destino), exist_ok=True)
    lienzo.save(destino, format="ICO",
                sizes=[(s, s) for s in TAMANOS])

    vista = os.path.splitext(destino)[0] + "-preview.png"
    lienzo.resize((256, 256), Image.LANCZOS).save(vista)

    return recorte.size, lado, vista


if __name__ == "__main__":
    base = os.path.dirname(os.path.abspath(__file__))
    origen = sys.argv[1] if len(sys.argv) > 1 else os.path.join(base, "icon.jpg")
    destino = os.path.join(base, "assets", "ps5.ico")

    if not os.path.exists(origen):
        raise SystemExit("no existe %s" % origen)

    tam, lado, vista = construir(origen, destino)
    print("origen    : %s" % origen)
    print("recortado : %dx%d  ->  lienzo cuadrado de %d" % (tam[0], tam[1], lado))
    print("tamanos   : %s" % ", ".join(str(s) for s in TAMANOS))
    print("escrito   : %s (%.1f KB)" % (destino, os.path.getsize(destino) / 1024))
    print("vista     : %s" % vista)
