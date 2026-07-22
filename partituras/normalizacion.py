"""
Corrección de orientación de página (contrato de desarrollo §6), en dos
pasos separados a propósito:

1. Rotación en múltiplos de 90° (`detectar_rotacion_90` / `aplicar_rotacion_90`):
   usa cv2.rotate, que es una permutación exacta de píxeles (transposición/
   espejado), sin interpolación ni recálculo de coordenadas — no introduce
   error. Resuelve el caso grave (página subida de costado).

2. Desalineado fino (`detectar_angulo_deskew` / `aplicar_deskew`): página
   más o menos derecha pero con una leve inclinación de escaneo. A
   diferencia del punto 1, esto sí requiere una rotación en ángulo
   arbitrario con interpolación (cv2.warpAffine) — inevitable acá porque el
   error de origen ya es sub-píxel, no hay forma de corregirlo sin
   remuestrear la imagen.

Limitación conocida: el puntaje de horizontalidad no distingue 0° de 180°
(una página al revés también tiene líneas horizontales fuertes) ni 90°
horario de 90° antihorario (mismo puntaje débil en ambos). Se propone un
default razonable pero la pantalla de ajuste manual asistido debe permitir
al usuario corregirlo con un flip de 180°/cambio de sentido si hace falta.
"""

import cv2
import numpy as np

from .imagen import MARGEN_FRAC, binarizar as _binarizar

# Ancho máximo (px) para la imagen que efectivamente pasa por
# cv2.HoughLinesP/cv2.morphologyEx en este módulo. Medido en la VPS: a los
# ~2480px de una página A4 a 300 DPI, detectar_angulo_deskew solo (Hough)
# tardaba ~40s/página — con 15 páginas eso supera el timeout de gunicorn.
# Ni el ángulo (arctan2, invariante a escala uniforme) ni el score de
# horizontalidad (ya normalizado por ancho) pierden precisión real al medir
# sobre una versión reducida — no hace falta la resolución completa para
# esto, sólo para el recorte final que ve el usuario.
_ANCHO_DETECCION_MAX = 1200


def _reducir_para_deteccion(b, ancho_max=_ANCHO_DETECCION_MAX):
    """Reduce una imagen binaria (0/255) para acelerar Hough/morphology.
    Devuelve (imagen_reducida, escala) — escala < 1 si se redujo, para que
    el caller pueda reescalar cualquier parámetro en píxeles absolutos
    (ver maxLineGap en detectar_angulo_deskew). INTER_AREA (promedia en vez
    de tomar un solo píxel) preserva mejor las líneas finas que
    INTER_NEAREST; el threshold post-resize vuelve a dejarla binaria (el
    promediado introduce grises en los bordes de cada línea)."""
    h, w = b.shape
    if w <= ancho_max:
        return b, 1.0
    escala = ancho_max / w
    reducida = cv2.resize(b, (ancho_max, max(int(round(h * escala)), 1)), interpolation=cv2.INTER_AREA)
    _, reducida = cv2.threshold(reducida, 0, 255, cv2.THRESH_BINARY)
    return reducida, escala


def _score_horizontalidad(img_bgr, margen_frac=MARGEN_FRAC):
    """
    Fuerza de la estructura de líneas horizontales largas, 0-1. Recorta
    márgenes en las 4 direcciones antes de medir: una sombra de
    encuadernación u otro artefacto de borde, al rotar la imagen, puede
    terminar como una barra que domina falsamente la medición.
    """
    b = _binarizar(img_bgr)
    h, w = b.shape
    mx, my = int(w * margen_frac), int(h * margen_frac)
    b = b[my:h - my, mx:w - mx]
    b, _ = _reducir_para_deteccion(b)
    h2, w2 = b.shape
    if h2 <= 0 or w2 <= 0:
        return 0.0
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(int(w2 * 0.02), 20), 1))
    lh = cv2.morphologyEx(b, cv2.MORPH_OPEN, kernel)
    return float((lh.sum(axis=1) / 255).max() / w2)


def detectar_rotacion_90(img_bgr):
    """
    Devuelve 0 o 90: si hace falta un giro de 90° para que las líneas del
    pentagrama queden horizontales. No distingue sentido (90 vs 270) —
    se asume horario como default, corregible en el ajuste manual.
    """
    score_actual = _score_horizontalidad(img_bgr)
    rotada = cv2.rotate(img_bgr, cv2.ROTATE_90_CLOCKWISE)
    score_rotada = _score_horizontalidad(rotada)
    return 90 if score_rotada > score_actual else 0


def aplicar_rotacion_90(img_bgr, grados):
    """grados: 0, 90, 180 o 270 (horario). Permutación exacta, sin interpolación."""
    if grados == 90:
        return cv2.rotate(img_bgr, cv2.ROTATE_90_CLOCKWISE)
    if grados == 180:
        return cv2.rotate(img_bgr, cv2.ROTATE_180)
    if grados == 270:
        return cv2.rotate(img_bgr, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return img_bgr


def detectar_angulo_deskew(img_bgr, margen_frac=MARGEN_FRAC):
    """
    Ángulo (grados, + = sentido horario) que corrige la inclinación fina de
    escaneo. Asume que la rotación de 90° ya fue aplicada (líneas
    aproximadamente horizontales). Usa la transformada de Hough
    probabilística para encontrar segmentos casi horizontales y promedia su
    desviación respecto de 0°.
    """
    b = _binarizar(img_bgr)
    h, w = b.shape
    mx, my = int(w * margen_frac), int(h * margen_frac)
    recorte = b[my:h - my, mx:w - mx]
    recorte, escala = _reducir_para_deteccion(recorte)

    lineas = cv2.HoughLinesP(
        recorte, 1, np.pi / 1800,  # resolución angular fina: 0.1°
        threshold=int(recorte.shape[1] * 0.15),
        minLineLength=int(recorte.shape[1] * 0.15),
        maxLineGap=max(int(round(10 * escala)), 2),  # 10px era en la resolución completa — reescalado para el mismo criterio relativo
    )
    if lineas is None:
        return 0.0

    angulos = []
    for x1, y1, x2, y2 in lineas.reshape(-1, 4):
        if x2 == x1:
            continue
        angulo = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        if abs(angulo) < 10:  # descartar lo que no sea "casi horizontal"
            angulos.append(angulo)

    if not angulos:
        return 0.0
    return float(np.median(angulos))


def normalizar_pagina(img_bgr, rotacion_grados, angulo_deskew):
    """Aplica primero la rotación de 90° y después el desalineado fino, en ese orden."""
    rotada = aplicar_rotacion_90(img_bgr, rotacion_grados)
    return aplicar_deskew(rotada, angulo_deskew)


def aplicar_deskew(img_bgr, angulo):
    """
    Rotación en ángulo arbitrario (grados, + = horario) vía warpAffine.
    A diferencia de aplicar_rotacion_90, esto interpola — inevitable para
    un ángulo que no es múltiplo de 90°. Fondo blanco fuera de los bordes
    originales (no negro), para que la página normalizada quede prolija.
    """
    if abs(angulo) < 0.05:
        return img_bgr
    h, w = img_bgr.shape[:2]
    centro = (w / 2, h / 2)
    matriz = cv2.getRotationMatrix2D(centro, angulo, 1.0)
    return cv2.warpAffine(
        img_bgr, matriz, (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )
