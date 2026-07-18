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


def generar_pdf_normalizado(paginas_bgr):
    """
    Reensambla una lista de imágenes (ya corregidas — rotación + deskew
    aplicados) en un PDF nuevo, una imagen por página. Se re-rasteriza en
    vez de intentar preservar contenido vectorial: el desalineado fino es un
    ángulo arbitrario que el propio formato PDF no soporta como rotación
    nativa (/Rotate solo admite múltiplos de 90°), así que este enfoque
    mantiene el pipeline uniforme para PDFs escaneados o vectoriales por
    igual. Devuelve los bytes del PDF resultante.
    """
    doc = fitz.open()
    try:
        for img_bgr in paginas_bgr:
            ok, buf = cv2.imencode('.png', img_bgr)
            if not ok:
                raise ValueError("No se pudo codificar la página como PNG")
            img_bytes = buf.tobytes()
            h, w = img_bgr.shape[:2]
            page = doc.new_page(width=w, height=h)
            page.insert_image(fitz.Rect(0, 0, w, h), stream=img_bytes)
        return doc.tobytes()
    finally:
        doc.close()
