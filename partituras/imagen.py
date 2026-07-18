"""Utilidades de procesamiento de imagen compartidas por vision.py y normalizacion.py."""

import cv2

# Márgenes recortados antes de analizar: escaneos/fotocopias reales suelen
# tener sombras de encuadernación o bordes oscuros pegados al margen que
# ensucian el conteo de tinta por fila/columna. El contenido musical real
# casi nunca llega al borde absoluto de la página.
MARGEN_FRAC = 0.06


def binarizar(img_bgr):
    """
    Umbral adaptativo (no un único umbral global tipo Otsu): calcula el
    umbral por región local, así que tolera mucho mejor una fotocopia con
    iluminación/tinta despareja — con un umbral global, la tinta débil de
    una zona más clara de la página puede perderse por completo.
    Tinta = blanco (255), fondo = negro (0).
    """
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    block_size = 51  # impar; ventana local sobre la que se calcula el umbral
    return cv2.adaptiveThreshold(
        gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV,
        block_size, 10,
    )
