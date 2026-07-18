"""
Detección clásica (OpenCV) de sistemas (pentagramas) y barras de compás.

No intenta reconocer notas, alturas ni ritmos — solo estructura visual
(bandas horizontales con contenido = sistema; trazos verticales altos dentro
de un sistema = barras de compás). Es una propuesta que el usuario corrige
después, no se espera precisión perfecta — ver contrato de desarrollo §7-8.
"""

import cv2
import numpy as np

from .imagen import binarizar as _binarizar

# Margen de seguridad mínimo, no el recorte de contenido real: se asume que
# la imagen que llega acá ya fue recortada a sus márgenes reales (ver
# detectar_margenes) antes de pasar por sistemas/barras. Un margen grande acá
# (como el 6% que había antes) termina cortando contenido real — ya lo vimos
# con una barra de compás real que quedaba fuera del rango de búsqueda.
MARGEN_X_FRAC = 0.005


def _mayor_salto(valores):
    """
    Dado un array de valores, devuelve el punto de corte que separa el grupo
    de valores altos del resto. Usa Otsu (el mismo algoritmo de
    binarización, aplicado acá a una lista de valores 1D en vez de píxeles)
    en lugar de "el salto más grande entre valores consecutivos": ese
    criterio es frágil si el valor más alto es un outlier (ej. un sistema
    con más pentagrama que el resto), porque el salto más grande puede caer
    dentro del propio grupo de valores altos en vez de entre los dos grupos.
    Evita además hardcodear un umbral fijo en píxeles, que no tiene sentido
    entre partituras de distinta resolución o tamaño de pentagrama.
    """
    unicos = np.unique(valores)
    if len(unicos) < 2:
        return unicos[0] if len(unicos) else 0
    # Otsu opera sobre uint8: reescalar los valores a 0-255 y volver a mapear el corte
    escala = 255.0 / (unicos.max() - unicos.min()) if unicos.max() > unicos.min() else 1.0
    valores_u8 = ((np.asarray(valores, dtype=np.float64) - unicos.min()) * escala).astype(np.uint8)
    corte_u8, _ = cv2.threshold(valores_u8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return unicos.min() + corte_u8 / escala


def _tiene_lineas_largas(binaria, y0, y1, margen_x):
    """
    True si la banda [y0,y1) contiene al menos una corrida horizontal larga
    de tinta (una línea de pentagrama real) — a diferencia de un párrafo de
    texto (título, copyright), que por más alto/denso que sea no forma
    líneas horizontales largas.
    """
    w = binaria.shape[1]
    banda = binaria[y0:y1, margen_x:w - margen_x] > 0
    if banda.shape[0] == 0 or banda.shape[1] == 0:
        return False
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(int(banda.shape[1] * 0.2), 20), 1))
    lh = cv2.morphologyEx(banda.astype(np.uint8) * 255, cv2.MORPH_OPEN, kernel)
    return bool((lh.sum(axis=1) / 255).max() > 0)


def detectar_sistemas(img_bgr):
    """
    Segmenta la página en bandas de contenido separadas por filas casi en
    blanco (huecos entre sistemas, encabezados, texto suelto). Se queda con
    las bandas más altas (vía _mayor_salto sobre las alturas) y, de esas,
    solo las que además contienen líneas horizontales largas reales — un
    bloque de texto correspondiente (ej. nota al pie extensa) puede ser tan
    alto como un sistema pero no tiene esa estructura.

    La banda de contenido (huecos en blanco) sirve para UBICAR cada sistema,
    pero es más ancha que el pentagrama real — incluye plicas que sobresalen
    y texto de dinámica arriba/abajo. El y0/y1 devuelto se angosta al
    extremo real del pentagrama (misma función que usa detectar_barras),
    porque es lo geométricamente correcto para un "sistema" y porque una
    barra de compás solo cruza esa altura, no toda la banda de contenido.
    Devuelve una lista de {'y0', 'y1'} en píxeles.
    """
    binaria = _binarizar(img_bgr)
    h, w = binaria.shape
    margen_x = int(w * MARGEN_X_FRAC)
    contenido = binaria[:, margen_x:w - margen_x] > 0

    perfil = contenido.sum(axis=1)
    umbral_vacio = max(perfil.max() * 0.01, 3)
    salto_min = max(int(h * 0.005), 10)

    bandas = []
    en_banda = False
    gap_run = 0
    inicio = 0
    for y in range(h):
        vacio = perfil[y] < umbral_vacio
        if vacio:
            gap_run += 1
            if en_banda and gap_run >= salto_min:
                bandas.append((inicio, y - gap_run))
                en_banda = False
        else:
            if not en_banda:
                inicio = y
                en_banda = True
            gap_run = 0
    if en_banda:
        bandas.append((inicio, h - 1))

    if not bandas:
        return []

    alturas = np.array([b1 - b0 for b0, b1 in bandas])
    corte = _mayor_salto(alturas)
    candidatas = [
        (b0, b1) for (b0, b1), alto in zip(bandas, alturas)
        if alto > corte and _tiene_lineas_largas(binaria, b0, b1, margen_x)
    ]

    sistemas = []
    margen_pentagrama = max(int(h * 0.003), 2)
    for b0, b1 in candidatas:
        banda_bool = binaria[b0:b1, margen_x:w - margen_x] > 0
        extremos = _extremos_pentagrama(banda_bool)
        if extremos is None:
            continue
        top, bottom = extremos
        sistemas.append({
            'y0': max(b0, b0 + top - margen_pentagrama),
            'y1': min(b1, b0 + bottom + margen_pentagrama),
        })
    return sistemas


def _largo_max_en_rango(col_bool, r0, r1):
    mejor = actual = 0
    for v in col_bool[r0:r1 + 1]:
        if v:
            actual += 1
            mejor = max(mejor, actual)
        else:
            actual = 0
    return mejor


def _mejor_corrida(col_bool, r0, r1):
    """Como _largo_max_en_rango, pero además devuelve dónde (índices relativos
    a r0) empieza y termina la corrida más larga — no solo su longitud."""
    mejor = (0, 0, 0)
    actual = 0
    inicio = 0
    for i, v in enumerate(col_bool[r0:r1 + 1]):
        if v:
            if actual == 0:
                inicio = i
            actual += 1
            if actual > mejor[0]:
                mejor = (actual, inicio, i)
        else:
            actual = 0
    return mejor


def _extremos_pentagrama(banda):
    """
    Dentro de la banda (ya recortada) de un sistema, encuentra la fila donde
    empieza la línea superior y la fila donde termina la línea inferior del
    pentagrama — usando las filas con más densidad de tinta (las 5 líneas),
    tomando el primer y último bloque de filas por encima de la mitad del
    máximo. Es más angosto que la banda completa del sistema (que incluye
    plicas por arriba/abajo y texto de dinámica) — una barra de compás real
    conecta específicamente estos dos extremos, no el resto del margen.
    """
    perfil = banda.sum(axis=1)
    if perfil.max() == 0:
        return None
    umbral = perfil.max() * 0.5
    filas = np.where(perfil > umbral)[0]
    if len(filas) == 0:
        return None
    return int(filas[0]), int(filas[-1])


def _ancho_en_fila(fila_bool, x, ventana=25):
    """
    Ancho (píxeles) de la corrida de tinta contigua más cercana a `x` en esa
    fila — sigue el trazo aunque esté corrido unos píxeles hacia un lado
    (una cabeza de nota pegada a una plica no queda perfectamente centrada
    en la plica). 0 si no hay tinta cerca.
    """
    n = len(fila_bool)
    x0, x1 = max(0, x - ventana), min(n, x + ventana)
    seg = fila_bool[x0:x1]
    idx = x - x0
    if idx >= len(seg) or not seg[idx]:
        tinta = np.where(seg)[0]
        if len(tinta) == 0:
            return 0
        idx = tinta[np.argmin(np.abs(tinta - idx))]
    izq = idx
    while izq > 0 and seg[izq - 1]:
        izq -= 1
    der = idx
    while der < len(seg) - 1 and seg[der + 1]:
        der += 1
    return der - izq + 1


def _hay_bulto_de_nota(binaria, x, y_borde, signo, grosor_medio, margen, radio_nota, ventana):
    """
    True si lo que ensancha la columna cerca de `y_borde` es una cabeza de
    nota pegada a una plica: un bulto ACOTADO que se apaga a las pocas filas
    (~radio_nota) de distancia. Un número de compás o una ligadura que pasa
    cerca también puede ensanchar la fila justo al lado del pentagrama, pero
    esa tinta sigue sostenida mucho más lejos que el alto típico de una
    cabeza de nota — por eso no alcanza con mirar una sola fila, hay que
    comprobar que el ensanchamiento efectivamente se apague cerca.
    `signo` es +1 para mirar hacia abajo del borde, -1 hacia arriba.

    Nota (ver detectar_barras y el historial de commits): se probaron varias
    variantes — chequeo hacia adentro del segmento (profundo y superficial),
    combinado con distintos umbrales de longitud mínima — y ninguna superó
    en neto a esta. Cada una arregla algún caso puntual (una ligadura, una
    cabeza de nota hueca) pero rompe otro distinto (una barra real con otra
    ligadura cerca, en otra posición). Esta es la versión con mejor
    resultado validado hasta ahora; queda un margen de error conocido y
    aceptado, corregible a mano en la pantalla de ajuste.
    """
    h = binaria.shape[0]
    cerca = _ancho_en_fila(binaria[int(np.clip(y_borde + signo * margen, 0, h - 1))], x, ventana)
    if cerca / grosor_medio < 1.5:
        return False
    lejos = _ancho_en_fila(binaria[int(np.clip(y_borde + signo * radio_nota, 0, h - 1))], x, ventana)
    return lejos / grosor_medio < 1.2


def _es_barra_limpia(binaria, x, y_ini, y_fin, margen, radio_nota, ventana):
    """
    True si el segmento [y_ini, y_fin] en la columna `x` mantiene un grosor
    ~constante de punta a punta — una barra de compás real. False si algún
    extremo tiene una cabeza de nota pegada (ver _hay_bulto_de_nota).
    Validado contra una muestra real: barras limpias dan ratio ~0-0.2,
    plicas con cabeza de nota dan ratio 3+ — sin casos ambiguos en el medio.

    `margen`, `radio_nota` y `ventana` se pasan en píxeles ya calculados a
    partir de una referencia de escala del documento (ver detectar_barras) —
    no van hardcodeados acá porque el tamaño de pentagrama en píxeles varía
    de una partitura a otra (distinta resolución de escaneo, distinto tamaño
    de impresión), y una distancia fija en píxeles calibrada contra una sola
    muestra no generaliza.
    """
    medio = range(y_ini + 2, y_fin - 1) if y_fin - y_ini > 6 else [(y_ini + y_fin) // 2]
    grosor_medio = np.median([_ancho_en_fila(binaria[y], x, ventana) for y in medio])
    if grosor_medio == 0:
        return False
    if _hay_bulto_de_nota(binaria, x, y_ini, -1, grosor_medio, margen, radio_nota, ventana):
        return False
    if _hay_bulto_de_nota(binaria, x, y_fin, 1, grosor_medio, margen, radio_nota, ventana):
        return False
    return True


def detectar_barras_candidatas(img_bgr, sistema_px, alto_referencia=None):
    """
    Detecta posiciones x (píxeles, absolutas sobre la imagen completa) de
    TODAS las candidatas a barra de compás dentro de un sistema — tanto las
    que se consideran limpias como las dudosas — exigiendo que la columna
    tenga una corrida vertical de tinta que cubra específicamente el tramo
    entre la primera y la última línea del pentagrama (no solo "sea larga"
    dentro de la banda completa del sistema) — así se descartan plicas y
    barras de corcheas, que no llegan a cruzar las 5 líneas.

    `alto_referencia`, si se pasa (alto en píxeles de la barra ancla ya
    confirmada por el usuario), reemplaza el 80% del alto de pentagrama
    detectado localmente como umbral de cruce. Es una referencia más
    confiable: en la práctica el alto de pentagrama que se detecta acá
    (extremos por densidad de tinta) suele quedar un poco más largo que una
    barra real, y separar por alto real de plica vs. barra funciona mucho
    mejor que separar por qué tan larga es contra ese extremo aproximado —
    hay un salto limpio entre ~85-90% (plicas que llegan a cruzar por
    casualidad) y ~97-100% (barras reales) del alto de la ancla, sin casos
    ambiguos en el medio. Sin ancla (p.ej. durante el bootstrap de
    encontrar_ancla, antes de tener una referencia confirmada) se usa el
    80% del pentagrama local como antes.

    Entre las candidatas que cruzan, se marcan como dudosas ('aceptada':
    False) las que se ensanchan en algún extremo (ver _es_barra_limpia) —
    plicas largas de notas fuera del pentagrama que casualmente cruzan todo
    el tramo, con una cabeza de nota pegada en la punta. Ninguna de las dos
    categorías es una detección definitiva — el usuario revisa ambas en la
    pantalla de ajuste (confirma dudosas, descarta aceptadas erróneas).

    Devuelve una lista de {'x': int, 'aceptada': bool}, ordenada por x.
    """
    binaria = _binarizar(img_bgr)
    h, w = binaria.shape
    margen_x = int(w * MARGEN_X_FRAC)
    y0, y1 = sistema_px['y0'], sistema_px['y1']
    banda = binaria[y0:y1, margen_x:w - margen_x] > 0
    if banda.shape[0] <= 0 or banda.shape[1] <= 0:
        return []

    extremos = _extremos_pentagrama(banda)
    if extremos is None:
        return []
    linea_top, linea_bottom = extremos
    span = linea_bottom - linea_top
    if span <= 0:
        return []

    # Referencia de escala del documento — de la ancla confirmada si está
    # disponible, o del pentagrama detectado localmente como respaldo (p.ej.
    # durante el bootstrap de encontrar_ancla, antes de tener una ancla).
    # Todas las distancias en píxeles de acá para abajo (franja para buscar
    # ensanchamiento, ventana de búsqueda, separación mínima entre
    # candidatas) se calculan como fracción de esta escala en vez de
    # píxeles fijos: el tamaño en píxeles del pentagrama varía mucho de una
    # partitura a otra (resolución de escaneo, tamaño de impresión), y una
    # distancia fija calibrada contra una sola muestra no generaliza.
    #
    # largo_min NO se calibra contra cuánto miden las plicas (varía nota a
    # nota, no es una propiedad estable) — se calibra contra cuánto puede
    # desviarse una BARRA REAL de la longitud exacta del ancla por ruido
    # normal de medición/escaneo. Lo que importa no es reconocer una plica,
    # es exigir que el candidato mida lo que el ancla ya demostró que mide
    # una barra real en este documento, con poco margen.
    escala = alto_referencia if alto_referencia else span
    largo_min = escala * 0.95 if alto_referencia else escala * 0.8
    margen = max(2, round(escala * 0.06))
    radio_nota = max(margen + 1, round(escala * 0.17))
    ventana = max(10, round(escala * 0.3))
    separacion_min = max(2, round(escala * 0.05))

    largos = np.array([
        _largo_max_en_rango(banda[:, x], linea_top, linea_bottom)
        for x in range(banda.shape[1])
    ])
    columnas = np.where(largos > largo_min)[0]
    if len(columnas) == 0:
        return []

    barras = []
    inicio = anterior = columnas[0]
    for col in columnas[1:]:
        if col - anterior > separacion_min:
            barras.append(int((inicio + anterior) / 2))
            inicio = col
        anterior = col
    barras.append(int((inicio + anterior) / 2))

    candidatas = []
    for x_local in barras:
        _, ini_rel, fin_rel = _mejor_corrida(banda[:, x_local], linea_top, linea_bottom)
        y_ini, y_fin = y0 + linea_top + ini_rel, y0 + linea_top + fin_rel
        limpia = _es_barra_limpia(binaria, x_local + margen_x, y_ini, y_fin, margen, radio_nota, ventana)
        candidatas.append({'x': x_local + margen_x, 'aceptada': limpia})
    return _fusionar_barras_dobles(candidatas)


def _fusionar_barras_dobles(candidatas, umbral_relativo=0.1):
    """
    Dos candidatas consecutivas (dentro del mismo sistema, ya vienen
    ordenadas por x) mucho más cerca entre sí que la separación típica del
    resto son casi seguro los dos trazos de una barra doble (fin de
    sección, repetición) — no dos compases distintos con uno invisible de
    largo casi cero en el medio. Se fusionan en una sola, quedándose con la
    aceptada si una de las dos lo es (y si no, con la primera).

    La separación "típica" se mide con la mediana de las distancias entre
    candidatas consecutivas — no el promedio, que un solo hueco de barra
    doble ya sesga hacia abajo. Con menos de 3 candidatas no hay suficiente
    referencia para distinguir "esta separación es rara" de "el sistema
    tiene pocos compases", así que no se intenta fusionar nada.
    """
    if len(candidatas) < 3:
        return candidatas
    xs = [c['x'] for c in candidatas]
    separaciones = [b - a for a, b in zip(xs, xs[1:])]
    tipica = float(np.median(separaciones))
    if tipica <= 0:
        return candidatas

    fusionadas = [candidatas[0]]
    for i in range(1, len(candidatas)):
        separacion = xs[i] - xs[i - 1]
        if separacion < tipica * umbral_relativo:
            if candidatas[i]['aceptada'] and not fusionadas[-1]['aceptada']:
                fusionadas[-1] = candidatas[i]
            # si no, se descarta candidatas[i] y queda la anterior
        else:
            fusionadas.append(candidatas[i])
    return fusionadas


def detectar_barras(img_bgr, sistema_px, alto_referencia=None):
    """Como detectar_barras_candidatas, pero devuelve sólo las posiciones x
    de las candidatas aceptadas (limpias) — para código que todavía no
    necesita mostrarle las dudosas al usuario (encontrar_ancla, etc.)."""
    candidatas = detectar_barras_candidatas(img_bgr, sistema_px, alto_referencia)
    return [c['x'] for c in candidatas if c['aceptada']]


def _inclinacion_barra(binaria, x_aprox, y0, y1, ventana=12):
    """
    Mide la inclinación (grados, + = sentido horario, misma convención que
    normalizacion.detectar_angulo_deskew) de una barra de compás específica,
    buscando en cada fila del pentagrama la columna con tinta más cercana a
    x_aprox dentro de una ventana angosta, y ajustando una recta a esos
    puntos. Devuelve None si no hay suficientes puntos para ajustar.
    """
    h, w = binaria.shape
    x0v, x1v = max(0, x_aprox - ventana), min(w, x_aprox + ventana)
    puntos = []
    for y in range(y0, y1):
        fila = binaria[y, x0v:x1v] > 0
        cols = np.where(fila)[0]
        if len(cols) == 0:
            continue
        centro = cols[np.argmin(np.abs(cols - (x_aprox - x0v)))]
        puntos.append((y, x0v + centro))
    if len(puntos) < max(int((y1 - y0) * 0.5), 5):
        return None
    ys = np.array([p[0] for p in puntos], dtype=np.float64)
    xs = np.array([p[1] for p in puntos], dtype=np.float64)
    pendiente, _ = np.polyfit(ys, xs, 1)  # x = pendiente*y + b
    return float(np.degrees(np.arctan(pendiente)))


def encontrar_ancla(img_bgr):
    """
    Encuentra una barra de compás confiable para usar como "ancla": no hace
    falta que sea la última del primer sistema (alcanza con cualquiera bien
    formada) — se evita la primera del sistema porque queda pegada a
    clave/armadura/compás, zona visualmente más sucia.
    Devuelve None si no se encontró ninguna, o un dict con posición,
    extremos del pentagrama e inclinación medida.
    """
    sistemas = detectar_sistemas(img_bgr)
    if not sistemas:
        return None
    binaria = _binarizar(img_bgr)
    h, w = binaria.shape

    for sistema in sistemas:
        barras_x = detectar_barras(img_bgr, sistema)
        candidatas = barras_x[1:] if len(barras_x) > 1 else barras_x
        for x in candidatas:
            angulo = _inclinacion_barra(binaria, x, sistema['y0'], sistema['y1'])
            if angulo is not None:
                return {
                    'x': x,
                    'y0': sistema['y0'],
                    'y1': sistema['y1'],
                    'angulo': angulo,
                    'alto_pentagrama': sistema['y1'] - sistema['y0'],
                }
    return None


def buscar_barra_en_rectangulo(img_bgr, x0, y0, x1, y1):
    """
    Búsqueda acotada: dado un rectángulo (en píxeles, sobre la imagen
    completa) que el usuario ubicó de forma aproximada alrededor de una
    barra de compás, busca ADENTRO una posición refinada. No hace falta que
    el rectángulo del usuario sea preciso — esta función lo ajusta.
    Devuelve None si no encuentra nada razonable (el rectángulo del usuario
    queda como respuesta final en ese caso, sin insistir).

    Entre las columnas con una corrida larga (candidatas plausibles), se
    prefiere la que además pase el filtro de "barra limpia" (ver
    _es_barra_limpia) — no alcanza con tomar la corrida más larga sin más,
    porque una plica con cabeza de nota pegada puede ser más larga que la
    barra real que está al lado. Sin este filtro, este refinamiento podía
    dar un resultado distinto (peor) al de la detección automática inicial
    (encontrar_ancla, que sí filtra plicas) para el mismo rectángulo —
    confundía al usuario ver un resultado y, al pedir "buscar" sobre esa
    misma zona, obtener otro.
    """
    binaria = _binarizar(img_bgr)
    h, w = binaria.shape
    x0, y0 = max(0, int(x0)), max(0, int(y0))
    x1, y1 = min(w, int(x1)), min(h, int(y1))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None

    recorte = binaria[y0:y1, x0:x1] > 0
    alto = recorte.shape[0]
    largos = np.array([_largo_max_en_rango(recorte[:, x], 0, alto - 1) for x in range(recorte.shape[1])])
    if largos.max() < alto * 0.5:
        return None

    escala = alto
    margen = max(2, round(escala * 0.06))
    radio_nota = max(margen + 1, round(escala * 0.17))
    ventana = max(10, round(escala * 0.3))

    orden = np.argsort(-largos)
    umbral_largo = largos.max() * 0.85
    mejor_x = None
    for x_local in orden:
        if largos[x_local] < umbral_largo:
            break
        _, ini_rel, fin_rel = _mejor_corrida(recorte[:, x_local], 0, alto - 1)
        if _es_barra_limpia(binaria, x0 + x_local, y0 + ini_rel, y0 + fin_rel, margen, radio_nota, ventana):
            mejor_x = int(x_local)
            break
    if mejor_x is None:
        mejor_x = int(orden[0])

    columna = recorte[:, mejor_x]
    filas_con_tinta = np.where(columna)[0]
    if len(filas_con_tinta) == 0:
        return None

    return {
        'x': x0 + mejor_x,
        'y0': y0 + int(filas_con_tinta[0]),
        'y1': y0 + int(filas_con_tinta[-1]),
    }


def _segmentos_no_vacios(perfil, total, umbral_frac=0.002, gap_min_frac=0.005):
    """
    Agrupa un perfil 1D (suma de tinta por fila o columna) en segmentos
    contiguos de "no vacío", fusionando huecos menores a gap_min_frac (ruido
    puntual entre trazos de un mismo bloque de contenido).
    """
    umbral = total * umbral_frac
    gap_min = max(int(len(perfil) * gap_min_frac), 8)
    no_vacio = perfil > umbral

    segmentos = []
    i, n = 0, len(perfil)
    while i < n:
        if no_vacio[i]:
            j = i
            while j < n and no_vacio[j]:
                j += 1
            segmentos.append([i, j])
            i = j
        else:
            i += 1

    fusionados = []
    for seg in segmentos:
        if fusionados and seg[0] - fusionados[-1][1] < gap_min:
            fusionados[-1][1] = seg[1]
        else:
            fusionados.append(seg)
    return [(a, b) for a, b in fusionados]


def _perfil_sostenido(perfil, ventana):
    """
    Mínimo en una ventana móvil centrada en cada punto (erosión 1D). Un pico
    angosto — más fino que la ventana, como una línea de pentagrama bien
    alineada, que ocupa 1-2 filas de altísima densidad — desaparece acá,
    porque sus vecinos inmediatos están casi vacíos. Una franja realmente
    sostenida (mancha, sombra de encuadernación) en muchas filas/columnas
    consecutivas seguidas NO se apaga, sea cual sea su largo total — a
    diferencia de comparar contra el pico crudo del perfil, esto no confunde
    "angosto pero muy denso en un punto" con "sostenido en el tiempo".
    """
    n = len(perfil)
    ventana = max(1, min(ventana, n))
    pad_izq = ventana // 2
    pad_der = ventana - 1 - pad_izq
    padded = np.pad(perfil, (pad_izq, pad_der), mode='edge')
    vistas = np.lib.stride_tricks.sliding_window_view(padded, ventana)
    return vistas.min(axis=1)


def _limites_contenido_real(perfil, total, umbral_artefacto=0.7):
    """
    Encuentra el primer y último extremo de contenido "real", descartando
    segmentos cuya densidad se mantiene SOSTENIDA por encima de
    umbral_artefacto en muchas posiciones consecutivas (ver
    _perfil_sostenido) — eso indica una franja casi sólida (sombra de
    encuadernación, borde oscuro de escaneo), no música ni texto real. No
    alcanza con mirar el pico crudo del segmento: una línea de pentagrama
    bien alineada también produce una fila (o columna) de densidad
    altísima, pero angosta, no sostenida.
    """
    ventana = max(9, int(len(perfil) * 0.003))
    sostenido = _perfil_sostenido(perfil, ventana)
    segmentos = _segmentos_no_vacios(perfil, total)
    reales = [s for s in segmentos if sostenido[s[0]:s[1]].max() < total * umbral_artefacto]
    if not reales:
        return 0, len(perfil) - 1
    return reales[0][0], reales[-1][1] - 1


def detectar_margenes(img_bgr):
    """
    Detecta el recuadro de contenido real de la página (dónde está la
    música/texto), excluyendo artefactos de escaneo tipo sombra de
    encuadernación. Es una propuesta — el usuario la termina de ajustar a
    mano, no se espera que sea exacta al píxel. Devuelve {'x0','y0','x1','y1'}
    relativos (0-1) a las dimensiones de la imagen.
    """
    binaria = _binarizar(img_bgr)
    h, w = binaria.shape
    col_profile = (binaria > 0).sum(axis=0)
    row_profile = (binaria > 0).sum(axis=1)

    x0, x1 = _limites_contenido_real(col_profile, h)
    y0, y1 = _limites_contenido_real(row_profile, w)

    return {'x0': x0 / w, 'y0': y0 / h, 'x1': x1 / w, 'y1': y1 / h}


def detectar_sistemas_y_compases(img_bgr):
    """
    Punto de entrada. Devuelve una lista de sistemas con coordenadas
    relativas (0-1) a las dimensiones de la imagen:
    [{orden, y, height, barras_x: [...], compases: [{x, y, width, height}]}]
    """
    h, w = img_bgr.shape[:2]
    sistemas_px = detectar_sistemas(img_bgr)

    resultado = []
    for orden, sistema in enumerate(sistemas_px):
        barras_x = detectar_barras(img_bgr, sistema)
        compases = [
            {
                'x': x0 / w,
                'y': sistema['y0'] / h,
                'width': (x1 - x0) / w,
                'height': (sistema['y1'] - sistema['y0']) / h,
            }
            for x0, x1 in zip(barras_x[:-1], barras_x[1:])
        ]
        resultado.append({
            'orden': orden,
            'y': sistema['y0'] / h,
            'height': (sistema['y1'] - sistema['y0']) / h,
            'barras_x': [x / w for x in barras_x],
            'compases': compases,
        })
    return resultado
