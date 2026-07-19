"""Lógica de dominio que no depende de HTTP — guardado de compases a partir
de lo que el cliente ya calculó (geometría + numeración), el reajuste de
numeración entre páginas, la invalidación en cascada cuando se rehace una
etapa anterior del pipeline (orientación → márgenes → sistemas → ancla →
barras/compases), y la resolución del itinerario de ejecución de una obra
(herencia de campos en blanco + tiempo estimado a partir de bpm)."""

import re
from datetime import timedelta

from django.db.models import F

from .models import Barra, Compas

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
    primero por un rango que no puede colisionar con nada evita eso."""
    segmentos = list(obra.segmentos.order_by('orden'))
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

        pulso_desde = seg.pulso_desde if seg.pulso_desde is not None else 1
        pulso_hasta = seg.pulso_hasta if seg.pulso_hasta is not None else pulsos_compas

        if not (bpm_inicio and pulsos_compas and pulso_hasta is not None and seg.compas_hasta is not None):
            tiempo_acumulado = None
            continue

        if seg.compas_desde == seg.compas_hasta:
            cantidad_pulsos = pulso_hasta - pulso_desde + 1
        else:
            compases_completos = seg.compas_hasta - seg.compas_desde - 1
            cantidad_pulsos = (pulsos_compas - pulso_desde + 1) + (compases_completos * pulsos_compas) + pulso_hasta

        duracion = max(cantidad_pulsos, 0) * (60.0 / bpm_inicio)
        info['duracion_calculada'] = duracion
        tiempo_acumulado += duracion

    return resueltos


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
