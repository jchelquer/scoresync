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


def armar_pdf_desde_imagenes(imagenes):
    """Arma un único PDF a partir de una lista de imágenes (bytes PNG, una
    por página, YA en el orden final) — usado para exportar una parte con
    las marcas (números/avisos/rampas/anotaciones) ya dibujadas del lado
    del cliente sobre cada página completa (ver exportarPdf en
    navegador_obra.html y exportar_pdf_partitura en views.py). No vuelve a
    dibujar nada — el dibujo real vive sólo en el canvas/JS, esto sólo
    empaqueta lo que ya llegó armado en un solo archivo."""
    doc = fitz.open()
    try:
        for datos in imagenes:
            pix = fitz.Pixmap(datos)
            pagina = doc.new_page(width=pix.width, height=pix.height)
            pagina.insert_image(pagina.rect, stream=datos)
        return doc.tobytes()
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
