"""Rasterización de páginas PDF a imágenes para su análisis con OpenCV."""

import cv2
import fitz
import numpy as np


def contar_paginas(pdf_path):
    doc = fitz.open(pdf_path)
    try:
        return doc.page_count
    finally:
        doc.close()


def rasterizar_pagina(pdf_path, numero_pagina, dpi=300):
    """
    Devuelve la página `numero_pagina` (1-indexada) como array numpy BGR,
    listo para procesar con OpenCV.
    """
    doc = fitz.open(pdf_path)
    try:
        page = doc[numero_pagina - 1]
        zoom = dpi / 72  # PyMuPDF trabaja en puntos, 72pt = 1 pulgada
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if pix.n == 4:
            img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
        elif pix.n == 3:
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        return img
    finally:
        doc.close()
