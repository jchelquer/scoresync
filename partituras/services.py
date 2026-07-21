"""Lógica de dominio que no depende de HTTP — guardado de compases a partir
de lo que el cliente ya calculó (geometría + numeración), el reajuste de
numeración entre páginas, la invalidación en cascada cuando se rehace una
etapa anterior del pipeline (orientación → márgenes → sistemas → ancla →
barras/compases), y la resolución del itinerario de ejecución de una obra
(herencia de campos en blanco + tiempo estimado a partir de bpm)."""

import re
from datetime import timedelta

from django.db.models import F

from .models import Barra, Compas, MarcaTiempoCompas, Segmento

_PATRON_INDICACION_COMPAS = re.compile(r'^[1-9]\d*/[1-9]\d*$')


_CAMPOS_ANCLA = [
    "ancla_x0", "ancla_y0", "ancla_x1", "ancla_y1",
    "ancla_linea_x", "ancla_linea_y0", "ancla_linea_y1", "ancla_confirmada",
]
_CAMPOS_BARRAS = ["barras_confirmadas", "compases_confirmados"]


def _limpiar_ancla(pagina):
    pagina.ancla_x0 = pagina.ancla_y0 = pagina.ancla_x1 = pagina.ancla_y1 = None
    pagina.ancla_linea_x = pagina.ancla_linea_y0 = pagina.ancla_linea_y1 = None
    pagina.ancla_confirmada = False


def _limpiar_barras(pagina):
    Compas.objects.filter(sistema__pagina=pagina).delete()
    Barra.objects.filter(sistema__pagina=pagina).delete()
    pagina.barras_confirmadas = False
    pagina.compases_confirmados = False


def invalidar_desde_ancla(pagina):
    """Al rehacer el ancla (ya confirmada antes): las barras se detectaron
    con la referencia de escala vieja — dejan de valer, no alcanza con
    desconfirmarlas."""
    _limpiar_barras(pagina)
    pagina.save(update_fields=_CAMPOS_BARRAS)


def invalidar_desde_sistemas(pagina):
    """Al rehacer sistemas (ya confirmados antes): el ancla y las
    barras/compases se ubicaron relativos a los sistemas viejos — cruzan
    coordenadas que ya no corresponden a nada."""
    _limpiar_ancla(pagina)
    _limpiar_barras(pagina)
    pagina.save(update_fields=_CAMPOS_ANCLA + _CAMPOS_BARRAS)


def invalidar_desde_margenes(pagina):
    """Al rehacer márgenes (ya confirmados antes): un margen distinto puede
    hacer que la detección de sistemas encuentre algo distinto (por ejemplo,
    si el margen viejo dejaba afuera o adentro contenido real) — se borran
    los sistemas (arrastra barras y compases por CASCADE) en vez de dejarlos
    colgados con coordenadas que ya no reflejan el margen actual."""
    pagina.sistemas.all().delete()  # CASCADE: Barra, Compas
    _limpiar_ancla(pagina)
    pagina.barras_confirmadas = False
    pagina.compases_confirmados = False
    pagina.save(update_fields=_CAMPOS_ANCLA + _CAMPOS_BARRAS)


def invalidar_desde_orientacion(pagina):
    """Al rehacer la orientación (rotación/desalineado, ya confirmada
    antes): TODAS las coordenadas guardadas más abajo (márgenes, sistemas,
    ancla, barras, compases) están calculadas sobre la imagen VIEJA —
    cambiar la rotación o el ángulo no sólo las desactualiza, las vuelve
    directamente erróneas (apuntan a otro lugar de la imagen nueva). Se
    resetea el margen al default de fábrica (no sólo se desconfirma) para
    que la próxima visita a esa etapa dispare una detección fresca, en vez
    de mostrar el recuadro viejo como si todavía tuviera sentido."""
    pagina.margen_confirmado = False
    pagina.margen_x0_detectado = pagina.margen_x0_aplicado = 0.0
    pagina.margen_y0_detectado = pagina.margen_y0_aplicado = 0.0
    pagina.margen_x1_detectado = pagina.margen_x1_aplicado = 1.0
    pagina.margen_y1_detectado = pagina.margen_y1_aplicado = 1.0
    pagina.sistemas.all().delete()  # CASCADE: Barra, Compas
    _limpiar_ancla(pagina)
    pagina.barras_confirmadas = False
    pagina.compases_confirmados = False
    pagina.save(update_fields=[
        "margen_confirmado",
        "margen_x0_detectado", "margen_y0_detectado", "margen_x1_detectado", "margen_y1_detectado",
        "margen_x0_aplicado", "margen_y0_aplicado", "margen_x1_aplicado", "margen_y1_aplicado",
    ] + _CAMPOS_ANCLA + _CAMPOS_BARRAS)


def numero_inicial_pagina(pagina):
    """Número que le correspondería al primer compás de esta página si no
    hay ninguno propio todavía — continúa desde el último compás de la
    página anterior (en toda la partitura), o arranca en 1 si no hay nada
    previo. Se le pasa al cliente para que sepa desde dónde numerar si
    arranca a construir compases de cero.

    Suma `repeticiones` del anterior, no siempre 1 — si ese último compás es
    un silencio de varios compases marcado a mano, el que sigue tiene que
    saltar la cantidad real, no sólo uno."""
    anterior = Compas.objects.filter(
        sistema__pagina__partitura=pagina.partitura,
        sistema__pagina__numero__lt=pagina.numero,
    ).order_by('-sistema__pagina__numero', '-sistema__orden', '-x').first()
    return (anterior.numero + anterior.repeticiones) if anterior else 1


def guardar_compases_pagina(pagina, compases_data):
    """Reemplaza los Compas de esta página exactamente por lo que manda el
    cliente — que ya calculó ahí mismo, al vuelo, la geometría (a partir de
    las barras aceptadas) y la numeración (insertando/borrando con +-1 sobre
    lo que había antes, nunca resecuenciando desde cero) — así una
    renumeración manual hecha en pantalla no se pisa por reconstruir del
    lado del servidor con otra lógica.

    Después ajusta hacia adelante si hace falta: si el primer compás de la
    página siguiente (la próxima que ya tenga compases construidos) no
    queda justo en +1 respecto al último de ésta, se le suma la diferencia
    a todos los compases desde ahí en adelante (cruzando páginas) — un
    desplazamiento parejo, no una resecuencia, para no romper ninguna otra
    numeración manual que hubiera más adelante en la partitura.
    """
    sistemas_por_id = {s.id: s for s in pagina.sistemas.all()}

    Compas.objects.filter(sistema__pagina=pagina).delete()
    nuevos = [
        Compas(
            sistema=sistemas_por_id[d['sistema_id']],
            numero=d['numero'],
            x=d['x'],
            y=sistemas_por_id[d['sistema_id']].y,
            width=d['width'],
            height=sistemas_por_id[d['sistema_id']].height,
            repeticiones=d.get('repeticiones', 1),
            origen='auto',
            confirmado=False,
        )
        for d in compases_data if d.get('sistema_id') in sistemas_por_id
    ]
    Compas.objects.bulk_create(nuevos)

    ultimo = Compas.objects.filter(sistema__pagina=pagina).order_by(
        '-sistema__orden', '-x',
    ).first()
    if ultimo is None:
        return

    siguientes = Compas.objects.filter(
        sistema__pagina__partitura=pagina.partitura,
        sistema__pagina__numero__gt=pagina.numero,
    ).order_by('sistema__pagina__numero', 'sistema__orden', 'x')
    primero_siguiente = siguientes.first()
    if primero_siguiente is None:
        return

    # Última + sus repeticiones, no siempre +1 — un cierre en silencio de
    # varios compases tiene que empujar la página siguiente la cantidad
    # real, no sólo un compás.
    desfasaje = (ultimo.numero + ultimo.repeticiones) - primero_siguiente.numero
    if desfasaje != 0:
        siguientes.update(numero=F('numero') + desfasaje)


# ── Itinerario de ejecución de una obra ────────────────────────────────────

def validar_indicacion_compas(texto):
    """Valida el formato de una indicación de compás — vacío es válido
    (significa "hereda de la fila anterior"). Levanta ValueError si no es
    numerador/denominador enteros positivos (ej: 4/4, 3/4, 6/8) — sin esto,
    un texto libre inválido pasaba sin aviso y _pulsos_por_compas
    simplemente no podía calcular nada más adelante, en silencio."""
    texto = (texto or '').strip()
    if not texto:
        return ''
    if not _PATRON_INDICACION_COMPAS.match(texto):
        raise ValueError(
            f'"{texto}" no es una indicación de compás válida — usá el formato '
            'numerador/denominador (ej: 4/4, 3/4, 6/8).'
        )
    return texto


def renumerar_segmentos(obra):
    """Renumera obra.segmentos de a 10 (10, 20, 30…), preservando el orden
    relativo actual — se corre en cada guardado del itinerario para que una
    inserción futura ("entre medio") siempre tenga hueco disponible, en vez
    de dejar que se vaya agotando de a poco con sucesivas inserciones.

    Se hace en dos pasadas: si se asignara el valor final directamente,
    orden=10 podría chocar contra otra fila que hoy YA tiene orden=10 y
    todavía no fue procesada (viola unique_together (obra, orden)). Pasar
    primero por un rango que no puede colisionar con nada evita eso.

    La fila de cierre (compas_desde vacío — ver docstring de Segmento)
    SIEMPRE queda última, sin importar qué valor de orden tenga o le hayan
    tipeado: si sólo se ordenara por orden a secas, un contenido nuevo con
    un orden más alto que el de la fila de cierre (fácil de tipear sin
    querer, esa fila no se distingue a simple vista salvo por tener Desde/
    Hasta vacíos) la dejaba encajada en el medio — y como resolver_segmentos
    corta la acumulación de tiempo apenas encuentra la fila de cierre, todo
    lo que quedara después se perdía en silencio (ni tiempo calculado, ni
    plan de ejecución)."""
    segmentos = sorted(obra.segmentos.all(), key=lambda s: (s.compas_desde is None, s.orden))
    OFFSET_TEMPORAL = 10_000_000
    for i, seg in enumerate(segmentos):
        seg.orden = OFFSET_TEMPORAL + i
        seg.save(update_fields=['orden'])
    for i, seg in enumerate(segmentos, start=1):
        seg.orden = i * 10
        seg.save(update_fields=['orden'])


def formatear_compas_pulso(compas, pulso, pulso_default):
    """Inversa de parsear_compas_pulso — (4, 1) con pulso_default=1 -> "4";
    (4, 1.5) -> "4,1.5". Se usa para reconstruir desde_texto/hasta_texto a
    partir de compas_desde/pulso_desde (o hasta) ya guardados, p.ej. si se
    cargaron por otra vía o hay que rehacer el backfill de una migración."""
    if compas is None:
        return ''
    if pulso is None or pulso == pulso_default:
        return str(compas)
    return f'{compas},{pulso:g}'


def parsear_compas_pulso(texto, pulso_default):
    """"4" -> (4, pulso_default); "4,1.5" -> (4, 1.5); "" -> (None, None).
    Coma separa compás de pulso, punto es el decimal DENTRO del pulso (no al
    revés) — así no hay ambigüedad entre "el separador" y "el decimal".
    Levanta ValueError si el formato no es válido."""
    texto = (texto or '').strip()
    if not texto:
        return None, None
    if ',' in texto:
        compas_str, pulso_str = texto.split(',', 1)
        return int(compas_str.strip()), float(pulso_str.strip())
    return int(texto), pulso_default


def _pulsos_por_compas(indicacion):
    """"4/4" -> 4.0, "6/8" -> 6.0 — el numerador tal cual, sin interpretar
    compases compuestos (6/8 dirigido "en 2" queda fuera de alcance)."""
    if not indicacion:
        return None
    try:
        return float(indicacion.split('/')[0])
    except (ValueError, IndexError):
        return None


def _pulso_bounds(seg, pulsos_compas):
    """(pulso_desde, pulso_hasta) resueltos de TODA la fila — 1 y
    pulsos_por_compas si vinieron en blanco (mismo criterio que en toda la
    app: vacío-desde es el primer pulso, vacío-hasta es hasta el último
    pulso completo del compás)."""
    pulso_desde = seg.pulso_desde if seg.pulso_desde is not None else 1
    pulso_hasta = seg.pulso_hasta if seg.pulso_hasta is not None else pulsos_compas
    return pulso_desde, pulso_hasta


def _cantidad_pulsos_fila(seg, pulsos_compas):
    """Total de pulsos que dura la fila entera, cruzando compases si
    corresponde."""
    pulso_desde, pulso_hasta = _pulso_bounds(seg, pulsos_compas)
    if seg.compas_desde == seg.compas_hasta:
        return pulso_hasta - pulso_desde + 1
    compases_completos = seg.compas_hasta - seg.compas_desde - 1
    return (pulsos_compas - pulso_desde + 1) + (compases_completos * pulsos_compas) + pulso_hasta


def _rango_pulsos_del_compas(seg, compas, pulsos_compas):
    """Pulso inicial y final (ambos inclusive) que le corresponden a UN
    compás puntual dentro de esta fila — 1..pulsos_compas salvo que sea el
    primer o el último compás de la fila y pulso_desde/pulso_hasta no lo
    cubran entero (la fila arranca o corta a mitad de compás)."""
    pulso_desde, pulso_hasta = _pulso_bounds(seg, pulsos_compas)
    ini = pulso_desde if compas == seg.compas_desde else 1
    fin = pulso_hasta if compas == seg.compas_hasta else pulsos_compas
    return int(ini), int(fin)


def _pulsos_antes_del_compas(seg, compas, pulsos_compas):
    """Cuántos pulsos de la fila (no del compás) ya transcurrieron antes de
    que arranque este compás puntual — ubica al compás dentro de la
    secuencia total de pulsos de la fila, para poder interpolar el tempo
    pulso a pulso en un accelerando/ritardando en vez de saltar de a un
    tempo fijo por compás."""
    if compas == seg.compas_desde:
        return 0
    ini_primero, fin_primero = _rango_pulsos_del_compas(seg, seg.compas_desde, pulsos_compas)
    primero = fin_primero - ini_primero + 1
    completos_entre = max(compas - seg.compas_desde - 1, 0)
    return primero + completos_entre * int(pulsos_compas)


def _pasadas_por_compas(obra):
    """Para cada ocurrencia de compás de la obra (fila, número), qué
    "pasada" le corresponde — mismo criterio que buscar_posicion (cuenta,
    en orden, cuántas filas navegables contienen ese número de compás) pero
    calculado una sola vez para toda la obra en vez de buscarlo cada vez.
    Usado tanto acá (construir_plan, para ubicar MarcaTiempoCompas) como en
    compases_desenrollados. Devuelve {(segmento_id, compas): pasada}."""
    contador = {}
    resultado = {}
    for seg in segmentos_navegables(obra):
        for compas in range(seg.compas_desde, seg.compas_hasta + 1):
            contador[compas] = contador.get(compas, 0) + 1
            resultado[(seg.id, compas)] = contador[compas]
    return resultado


def _perfil_y_anclas_fila(seg, bpm_inicio, pulsos_compas, marcas_por_compas_pasada,
                           pasadas_por_compas, tiempo_inicio_siguiente):
    """Para UNA fila: posiciones (tiempo calculado acumulado, en segundos
    desde el arranque de la fila, ANTES de cada pulso — posiciones[k] es el
    instante en que arranca el pulso k, 0-based; posiciones[-1] es la
    duración calculada total de la fila) y anclas (lista ordenada de
    (posición calculada, tiempo real) — combina el borde de la fila,
    ver Segmento.tiempo_inicio, con cualquier MarcaTiempoCompas puntual que
    caiga dentro de su rango de compases, ver construir_plan)."""
    total_pulsos_fila = int(_cantidad_pulsos_fila(seg, pulsos_compas))
    posiciones = [0.0]
    for idx in range(total_pulsos_fila):
        bpm_pulso = bpm_inicio
        if seg.bpm_llegada and total_pulsos_fila > 1:
            fraccion = idx / (total_pulsos_fila - 1)
            bpm_pulso = bpm_inicio + (seg.bpm_llegada - bpm_inicio) * fraccion
        posiciones.append(posiciones[-1] + 60.0 / bpm_pulso)
    duracion_calculada_fila = posiciones[-1]

    anclas = []
    if seg.tiempo_inicio is not None:
        anclas.append((0.0, seg.tiempo_inicio.total_seconds()))
    for compas in range(seg.compas_desde, seg.compas_hasta + 1):
        pasada = pasadas_por_compas.get((seg.id, compas))
        marca = marcas_por_compas_pasada.get((compas, pasada)) if pasada else None
        if marca is not None:
            idx_compas = _pulsos_antes_del_compas(seg, compas, pulsos_compas)
            anclas.append((posiciones[idx_compas], marca.total_seconds()))
    if tiempo_inicio_siguiente is not None:
        anclas.append((duracion_calculada_fila, tiempo_inicio_siguiente.total_seconds()))
    anclas.sort(key=lambda a: a[0])
    return posiciones, anclas


def _escala_en_posicion(anclas, posicion):
    """Factor para escalar la duración calculada de un pulso a duración
    real, según en qué tramo (entre qué par de anclas consecutivas) cae su
    posición — ver _perfil_y_anclas_fila. Fuera del tramo cubierto por las
    anclas (antes de la primera, después de la última, o con menos de dos
    anclas en total) devuelve 1.0: sin dato ahí, se usa el tiempo calculado
    tal cual — degrada solo, sin inventar una extrapolación que nadie
    pidió."""
    if len(anclas) < 2 or posicion < anclas[0][0] or posicion > anclas[-1][0]:
        return 1.0
    # Cada tramo es [pos_a, pos_b) — semiabierto — salvo el último, cerrado
    # de los dos lados: una posición que cae justo EN una ancla intermedia
    # pertenece al tramo que ARRANCA ahí, no al que termina ahí (un pulso
    # que empieza justo en una marca ya corre al ritmo nuevo).
    ultimo_tramo = len(anclas) - 2
    for i, ((pos_a, t_a), (pos_b, t_b)) in enumerate(zip(anclas, anclas[1:])):
        if pos_a <= posicion < pos_b or (i == ultimo_tramo and posicion == pos_b):
            return (t_b - t_a) / (pos_b - pos_a) if pos_b != pos_a else 1.0
    return 1.0


def resolver_segmentos(obra):
    """Recorre los segmentos de la obra en orden, resolviendo lo que en cada
    fila quedó en blanco (hereda de la última fila con un valor propio — ver
    ayuda de cada campo en el modelo Segmento) y calculando cuánto dura cada
    tramo a partir de bpm/bpm_llegada. Devuelve una lista de dicts, uno por
    segmento, en el mismo orden, cada uno con:
      segmento, indicacion_compas, bpm (resueltos),
      pulsos_por_compas, duracion_calculada (segundos), tiempo_inicio_calculado (segundos)

    Si en algún punto falta bpm o indicación de compás para calcular, la
    acumulación de tiempo se corta ahí — el resto de las filas quedan con
    tiempo_inicio_calculado en None en vez de inventar un valor con un
    tempo/indicación por defecto que nadie pidió."""
    segmentos = list(obra.segmentos.order_by('orden'))
    resueltos = []
    indicacion_vigente = None
    bpm_vigente = None
    tiempo_acumulado = 0.0

    for seg in segmentos:
        indicacion = seg.indicacion_compas or indicacion_vigente
        if indicacion:
            indicacion_vigente = indicacion
        bpm_inicio = seg.bpm or bpm_vigente
        pulsos_compas = _pulsos_por_compas(indicacion)

        info = {
            'segmento': seg,
            'indicacion_compas': indicacion,
            'bpm': bpm_inicio,
            'pulsos_por_compas': pulsos_compas,
            'duracion_calculada': None,
            'tiempo_inicio_calculado': tiempo_acumulado,
        }
        resueltos.append(info)

        if seg.bpm_llegada:
            bpm_vigente = seg.bpm_llegada
        elif seg.bpm:
            bpm_vigente = seg.bpm

        if seg.compas_desde is None:
            break  # fila de cierre: sólo el ancla de tiempo que ya se guardó arriba

        if tiempo_acumulado is None:
            continue  # ya se cortó la acumulación en una fila anterior

        pulso_desde, pulso_hasta = _pulso_bounds(seg, pulsos_compas) if pulsos_compas else (None, None)

        if not (bpm_inicio and pulsos_compas and pulso_hasta is not None and seg.compas_hasta is not None):
            tiempo_acumulado = None
            continue

        cantidad_pulsos = _cantidad_pulsos_fila(seg, pulsos_compas)
        duracion = max(cantidad_pulsos, 0) * (60.0 / bpm_inicio)
        info['duracion_calculada'] = duracion
        tiempo_acumulado += duracion

    return resueltos


# ── Navegador manual del itinerario ─────────────────────────────────────

def segmentos_navegables(obra):
    """Filas de obra.segmentos que se pueden "visitar" — con rango de
    compases propio completo. Excluye la fila de cierre (compas_desde
    null, sólo marca dónde termina el último compás) y cualquier fila a
    medio cargar (compas_hasta todavía vacío)."""
    return [
        s for s in obra.segmentos.order_by('orden')
        if s.compas_desde is not None and s.compas_hasta is not None
    ]


def buscar_posicion(obra, numero_compas, pasada=1):
    """Busca la posición para "compás X, Nda vez": el compás buscado puede
    caer en cualquier punto DENTRO del rango de una fila (no sólo en su
    borde), así que se filtra por contención — no por igualdad contra
    compas_desde/compas_hasta — y se toma la n-ésima fila navegable (en
    orden) cuyo rango lo contiene. Devuelve (segmento, numero_compas), o
    None si no hay ninguna coincidencia (o no llega a haber esa pasada)."""
    coincidencias = 0
    for seg in segmentos_navegables(obra):
        if seg.compas_desde <= numero_compas <= seg.compas_hasta:
            coincidencias += 1
            if coincidencias == pasada:
                return (seg, numero_compas)
    return None


def avanzar_compas(obra, segmento, compas_actual):
    """Compás siguiente: uno más dentro de la misma fila, o el primero de
    la próxima fila navegable si ya se llegó al final de ésta. None si
    era el último compás de toda la obra (no hay nada después)."""
    if compas_actual < segmento.compas_hasta:
        return (segmento, compas_actual + 1)
    navegables = segmentos_navegables(obra)
    idx = navegables.index(segmento)
    if idx + 1 < len(navegables):
        siguiente = navegables[idx + 1]
        return (siguiente, siguiente.compas_desde)
    return None


def retroceder_compas(obra, segmento, compas_actual):
    """Simétrico de avanzar_compas: un compás antes, cruzando a la fila
    navegable previa si hace falta. None si era el primer compás de toda
    la obra."""
    if compas_actual > segmento.compas_desde:
        return (segmento, compas_actual - 1)
    navegables = segmentos_navegables(obra)
    idx = navegables.index(segmento)
    if idx > 0:
        anterior = navegables[idx - 1]
        return (anterior, anterior.compas_hasta)
    return None


def geometria_partitura(partitura):
    """Geometría (sistemas y compases, por página) de toda una partitura —
    pensado para mandarse una sola vez al cliente como JSON, igual que
    construir_plan, y que el cursor sobre el score se dibuje ahí con lo
    que ya tiene en memoria en vez de volver a pedirle al servidor la
    posición de cada compás a medida que avanza la ejecución.

    Devuelve una lista de dicts, uno por página, con numero/margen_x0/
    margen_y0/margen_x1/margen_y1 (el recuadro de contenido real ya
    confirmado, ver Pagina.margen_*_aplicado)/sistemas (orden/y/height)/
    compases (numero/sistema_orden/x/y/width/height/repeticiones) — sin
    URL de imagen (eso lo arma la vista, que sí conoce las rutas).

    El margen de la página reemplaza al "borde de lo visible en pantalla"
    en el cálculo de la caja de un compás (ver calcularCaja en el
    cliente): así la caja de cualquier compás sale sólo de datos ya
    confirmados, sin depender de qué esté scrolleado/zoomeado en ese
    momento — funciona igual esté o no ese compás realmente en pantalla.

    `repeticiones` > 1 es un silencio de varios compases marcado a mano
    (ver Compas.repeticiones): una sola fila cubre varios números de
    compás reales con una única caja ancha — el cliente es quien sabe, al
    buscar un número intermedio, que le corresponde una porción
    proporcional de esa caja (no que "no existe")."""
    paginas = []
    for pagina in partitura.paginas.order_by('numero'):
        sistemas = []
        compases = []
        for sistema in pagina.sistemas.order_by('orden'):
            sistemas.append({'orden': sistema.orden, 'y': sistema.y, 'height': sistema.height})
            for compas in sistema.compases.order_by('x'):
                compases.append({
                    'numero': compas.numero,
                    'sistema_orden': sistema.orden,
                    'x': compas.x, 'y': compas.y,
                    'width': compas.width, 'height': compas.height,
                    'repeticiones': compas.repeticiones,
                })
        paginas.append({
            'numero': pagina.numero,
            'margen_x0': pagina.margen_x0_aplicado, 'margen_y0': pagina.margen_y0_aplicado,
            'margen_x1': pagina.margen_x1_aplicado, 'margen_y1': pagina.margen_y1_aplicado,
            'sistemas': sistemas, 'compases': compases,
        })
    return paginas


def construir_plan(obra, desde_compas, desde_pasada, hasta_compas, hasta_pasada,
                    desde_pulso=None, hasta_pulso=None):
    """Arma la lista de PULSOS (no de compases) entre "desde" y "hasta"
    (mismo criterio de compás+pasada que buscar_posicion), cada uno con su
    propia duración resuelta — pensado para mandarse una sola vez al
    cliente como JSON y que la ejecución en tiempo real la programe JS con
    un único reloj absoluto, en vez de pedirle un pulso a la vez al
    servidor a medida que avanza (eso dejaría que la variabilidad de red se
    fuera acumulando como desfasaje de tempo).

    desde_pulso/hasta_pulso (opcionales, notación parsear_compas_pulso) acotan
    el primer/último compás del rango a partir de un pulso puntual en vez del
    compás entero — se truncan a entero (mismo criterio que
    _rango_pulsos_del_compas) porque acá se itera pulso a pulso, no hay
    fracción de pulso que programar como tick propio. Sólo se aplican en la
    primera/última iteración del compás, no en los intermedios: si el rango
    cruza varios compases, todos los del medio se tocan enteros.

    Se arma a nivel de pulso, no de compás, por dos motivos:
    - En una fila con accelerando/ritardando (bpm_llegada propio), el tempo
      de cada PULSO se interpola linealmente entre bpm y bpm_llegada según
      su posición en la secuencia total de pulsos de la fila — si se
      interpolara por compás entero, el cambio de tempo saltaría en
      escalones en cada borde de compás en vez de sonar continuo.
    - El primer/último compás de una fila puede no arrancar en el pulso 1
      ni terminar en el último pulso del compás (pulso_desde/pulso_hasta) —
      a nivel de pulso eso sale solo, en vez de tener que tratarlo como
      caso especial en la duración de "ese compás".

    Devuelve (pulsos, completo): pulsos es la lista de dicts (uno por
    pulso, en orden) con segmento_id/compas/pulso/pulsos_por_compas/
    es_primer_pulso_compas/acento/indicacion_compas/bpm/
    variacion_tempo_display/bpm_llegada/descripcion/duracion (duracion en
    segundos, None si no se pudo resolver bpm o indicación para ese
    compás; pulso/pulsos_por_compas ubican al pulso DENTRO del compás —
    p.ej. para mover el punto del metrónomo dentro de un recuadro en vez
    de sólo flashear en el lugar; es_primer_pulso_compas marca cuándo
    corresponde refrescar el número de compás en pantalla; acento es el
    pulso 1 musical real, ver comentario más abajo); completo es False si
    algún pulso quedó sin duración — el cliente no debería reproducir en tiempo
    real un plan incompleto."""
    navegables = segmentos_navegables(obra)
    if not navegables:
        return [], True

    resueltos_por_id = {info['segmento'].id: info for info in resolver_segmentos(obra)}

    # tiempo_inicio (real) de la fila siguiente EN LA OBRA (no sólo entre
    # navegables — incluye la fila de cierre), la "pasada" de cada ocurrencia
    # de compás y las marcas puntuales por compás (ver MarcaTiempoCompas) —
    # todo esto se arma una sola vez acá (no adentro del loop de abajo)
    # porque necesita ver TODAS las filas/marcas en orden. Con esto,
    # _perfil_y_anclas_fila arma — perezoso, cacheado por fila más abajo —
    # las anclas reales de cada fila combinando ambas fuentes (ver esa
    # función y _escala_en_posicion): estas marcas puntuales tienen
    # prioridad donde existen, cae en el borde de fila donde no, y en el
    # tiempo calculado puro donde no hay ninguna de las dos.
    todos_los_segmentos = list(obra.segmentos.order_by('orden'))
    tiempo_inicio_siguiente_por_id = {
        s.id: (todos_los_segmentos[i + 1].tiempo_inicio if i + 1 < len(todos_los_segmentos) else None)
        for i, s in enumerate(todos_los_segmentos)
    }
    pasadas_por_compas = _pasadas_por_compas(obra)
    marcas_por_compas_pasada = {
        (m.compas, m.pasada): m.tiempo_inicio for m in obra.marcas_tiempo_compas.all()
    }
    perfiles_por_fila = {}  # cache: seg.id -> (posiciones, anclas), ver _perfil_y_anclas_fila

    pos_desde = buscar_posicion(obra, desde_compas, desde_pasada) or (navegables[0], navegables[0].compas_desde)
    if hasta_compas is not None:
        pos_hasta = buscar_posicion(obra, hasta_compas, hasta_pasada) or (navegables[-1], navegables[-1].compas_hasta)
    else:
        pos_hasta = (navegables[-1], navegables[-1].compas_hasta)

    pulsos = []
    completo = True
    pos = pos_desde
    primera_iteracion = True
    while pos is not None:
        seg, compas = pos
        es_ultima_iteracion = (seg.orden, compas) >= (pos_hasta[0].orden, pos_hasta[1])
        info = resueltos_por_id.get(seg.id, {})
        bpm_inicio = info.get('bpm')
        pulsos_compas = info.get('pulsos_por_compas')

        if not (bpm_inicio and pulsos_compas):
            completo = False
            pulsos.append({
                'segmento_id': seg.id,
                'compas': compas,
                'es_primer_pulso_compas': True,
                'acento': True,
                'indicacion_compas': info.get('indicacion_compas'),
                'bpm': bpm_inicio,
                'variacion_tempo_display': seg.get_variacion_tempo_display() if seg.variacion_tempo else '',
                'bpm_llegada': seg.bpm_llegada,
                'descripcion': seg.descripcion,
                'duracion': None,
            })
        else:
            pulso_ini, pulso_fin = _rango_pulsos_del_compas(seg, compas, pulsos_compas)
            # pulso_ini/pulso_fin (originales, de la fila) siguen siendo la
            # base de idx_en_fila más abajo — la interpolación de tempo
            # tiene que ubicar al pulso en la secuencia real de la fila,
            # aunque acá se emitan menos pulsos de los que la fila tiene.
            pulso_ini_emitir, pulso_fin_emitir = pulso_ini, pulso_fin
            if primera_iteracion and desde_pulso is not None:
                pulso_ini_emitir = max(pulso_ini, int(desde_pulso))
            if es_ultima_iteracion and hasta_pulso is not None:
                pulso_fin_emitir = min(pulso_fin, int(hasta_pulso))
            total_pulsos_fila = _cantidad_pulsos_fila(seg, pulsos_compas)
            offset_compas = _pulsos_antes_del_compas(seg, compas, pulsos_compas)

            if seg.id not in perfiles_por_fila:
                perfiles_por_fila[seg.id] = _perfil_y_anclas_fila(
                    seg, bpm_inicio, pulsos_compas, marcas_por_compas_pasada,
                    pasadas_por_compas, tiempo_inicio_siguiente_por_id.get(seg.id),
                )
            posiciones_fila, anclas_fila = perfiles_por_fila[seg.id]

            for p in range(pulso_ini_emitir, pulso_fin_emitir + 1):
                idx_en_fila = offset_compas + (p - pulso_ini)
                bpm_pulso = bpm_inicio
                if seg.bpm_llegada and total_pulsos_fila > 1:
                    fraccion = idx_en_fila / (total_pulsos_fila - 1)
                    bpm_pulso = bpm_inicio + (seg.bpm_llegada - bpm_inicio) * fraccion
                duracion_pulso = 60.0 / bpm_pulso
                pulsos.append({
                    'segmento_id': seg.id,
                    'compas': compas,
                    'pulso': p,
                    'pulsos_por_compas': int(pulsos_compas),
                    # Primer pulso EMITIDO de este compás (no necesariamente
                    # el primero de la fila: puede haberse acotado más con
                    # desde_pulso) — es lo que usa el cliente para saber
                    # dónde arranca cada ocurrencia de compás en el plan.
                    'es_primer_pulso_compas': p == pulso_ini_emitir,
                    # El acento del metrónomo (click agudo) es el pulso 1
                    # MUSICAL del compás — no el primer pulso de la fila.
                    # Si la fila arranca a mitad de compás (pulso_ini > 1,
                    # p.ej. un ritardando que empieza en el pulso 2), ese
                    # punto de arranque no es el pulso 1 real y no debe
                    # marcarse como acentuado.
                    'acento': p == 1,
                    'indicacion_compas': info.get('indicacion_compas'),
                    'bpm': round(bpm_pulso),
                    'variacion_tempo_display': seg.get_variacion_tempo_display() if seg.variacion_tempo else '',
                    'bpm_llegada': seg.bpm_llegada,
                    'descripcion': seg.descripcion,
                    'duracion': duracion_pulso,
                    # Duración a usar cuando el navegador ejecuta guiado por
                    # el audio real en vez del tempo calculado (ver
                    # "Ejecutar con audio"): la calculada, escalada según el
                    # tramo de anclas reales (MarcaTiempoCompas puntuales y/o
                    # el borde de la fila, ver _perfil_y_anclas_fila) donde
                    # cae este pulso — 1.0 (sin escalar) donde no hay ningún
                    # dato real todavía.
                    'duracion_real': duracion_pulso * _escala_en_posicion(anclas_fila, posiciones_fila[idx_en_fila]),
                })

        primera_iteracion = False
        if es_ultima_iteracion:
            break
        pos = avanzar_compas(obra, seg, compas)

    return pulsos, completo


def compases_desenrollados(obra):
    """Una entrada por CADA ocurrencia de compás de la obra completa (a
    diferencia de segmentos_navegables, que trabaja a nivel de fila del
    itinerario) — cada repetición (2da vez, D.C., etc.) es su propia
    entrada, con su propia 'pasada'. Pensada para sincronizar_compases (tap
    compás a compás, sincronización fina — ver MarcaTiempoCompas).

    Devuelve (entradas, completo): entradas es una lista de dicts, uno por
    ocurrencia, con segmento_id/compas/pasada/indicacion_compas/bpm/
    tiempo_inicio_calculado (acumulado desde el primer compás de la obra,
    en segundos — None de ahí en más si en algún punto faltó bpm o
    indicación)/tiempo_inicio (real, si ya está marcado)/es_cierre; completo
    es False si algún compás quedó sin bpm/indicación resueltos (mismo
    criterio que construir_plan).

    La ÚLTIMA entrada, si la obra tiene fila de cierre (ver Segmento —
    compas_desde null, marca dónde termina el último compás de verdad), es
    esa fila de cierre en vez de un compás puntual (compas/pasada quedan en
    None, es_cierre en True) — mismo tiempo_inicio que usa
    sincronizar_audio.html (por fila), no uno nuevo: tapear esta entrada
    hay que guardarla con marcar_tiempo_segmento, no marcar_tiempo_compas."""
    navegables = segmentos_navegables(obra)
    if not navegables:
        return [], True

    pulsos, completo = construir_plan(obra, navegables[0].compas_desde, 1, None, None)
    pasadas = _pasadas_por_compas(obra)
    marcas = {(m.compas, m.pasada): m.tiempo_inicio for m in obra.marcas_tiempo_compas.all()}

    entradas = []
    acumulado = 0.0
    for p in pulsos:
        if p.get('es_primer_pulso_compas'):
            pasada = pasadas.get((p['segmento_id'], p['compas']), 1)
            entradas.append({
                'segmento_id': p['segmento_id'],
                'compas': p['compas'],
                'pasada': pasada,
                'indicacion_compas': p['indicacion_compas'],
                'bpm': p['bpm'],
                'tiempo_inicio_calculado': acumulado,
                'tiempo_inicio': marcas.get((p['compas'], pasada)),
                'es_cierre': False,
            })
        if acumulado is not None:
            acumulado = (acumulado + p['duracion']) if p['duracion'] is not None else None

    cierre = obra.segmentos.filter(compas_desde__isnull=True).first()
    if cierre:
        entradas.append({
            'segmento_id': cierre.id,
            'compas': None,
            'pasada': None,
            'indicacion_compas': None,
            'bpm': None,
            'tiempo_inicio_calculado': acumulado,
            'tiempo_inicio': cierre.tiempo_inicio,
            'es_cierre': True,
        })
    return entradas, completo


def tiempo_real_ancla(obra, segmento_id, compas):
    """Mejor tiempo real disponible para el ARRANQUE de una ocurrencia de
    compás puntual — pensado para anclar "Ejecutar con audio" en
    navegador_obra.html (ver plan_obra): la MarcaTiempoCompas si existe (más
    precisa), si no el borde de la fila (Segmento.tiempo_inicio, sólo sirve
    si compas es justo el primer compás de esa fila), si no None. Misma
    prioridad que usa construir_plan internamente, pero para UN solo punto
    en vez de todo el plan."""
    pasada = _pasadas_por_compas(obra).get((segmento_id, compas))
    if pasada is not None:
        marca = MarcaTiempoCompas.objects.filter(obra=obra, compas=compas, pasada=pasada).first()
        if marca is not None:
            return marca.tiempo_inicio.total_seconds()
    segmento = Segmento.objects.filter(pk=segmento_id).first()
    if segmento and segmento.tiempo_inicio is not None and segmento.compas_desde == compas:
        return segmento.tiempo_inicio.total_seconds()
    return None


def desplazar_marcas_compas(obra, delta_segundos, objetivos=None):
    """Corre una misma fracción de segundos (positiva o negativa) las
    MarcaTiempoCompas de la obra — para corregir un desfasaje del tap
    (p.ej. el tiempo de reacción de la persona tapeando) sin retapear.
    objetivos: iterable opcional de (compas, pasada) — sólo se corren esas
    marcas puntuales (selección múltiple en sincronizar_compases.html); si
    no se pasa, se corren TODAS las de la obra. No toca Segmento.tiempo_inicio
    (mecanismo aparte, por fila) ni baja de 0. Devuelve cuántas se ajustaron."""
    marcas = obra.marcas_tiempo_compas.all()
    if objetivos is not None:
        objetivos = set(objetivos)
        marcas = [m for m in marcas if (m.compas, m.pasada) in objetivos]
    else:
        marcas = list(marcas)
    for marca in marcas:
        nuevo = max(marca.tiempo_inicio.total_seconds() + delta_segundos, 0)
        marca.tiempo_inicio = timedelta(seconds=nuevo)
    MarcaTiempoCompas.objects.bulk_update(marcas, ["tiempo_inicio"])
    return len(marcas)


def recalcular_tiempos_calculados(obra):
    """Corre resolver_segmentos y guarda tiempo_inicio_calculado en cada
    Segmento de la obra — se llama cada vez que se guarda el itinerario, así
    queda como referencia independiente de tiempo_inicio (el real, sincronizado
    con audio/video). Devuelve la lista de dicts de resolver_segmentos, para
    que la vista pueda además chequear los límites de pulso sin recalcular
    todo de nuevo."""
    resueltos = resolver_segmentos(obra)
    for info in resueltos:
        seg = info['segmento']
        segundos = info['tiempo_inicio_calculado']
        nuevo = timedelta(seconds=segundos) if segundos is not None else None
        if seg.tiempo_inicio_calculado != nuevo:
            seg.tiempo_inicio_calculado = nuevo
            seg.save(update_fields=['tiempo_inicio_calculado'])
    return resueltos
