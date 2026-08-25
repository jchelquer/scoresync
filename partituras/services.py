"""Lógica de dominio que no depende de HTTP — guardado de compases a partir
de lo que el cliente ya calculó (geometría + numeración), el reajuste de
numeración entre páginas, la invalidación en cascada cuando se rehace una
etapa anterior del pipeline (orientación → márgenes → sistemas → ancla →
barras/compases), y la resolución del itinerario de ejecución de una obra
(herencia de campos en blanco + tiempo estimado a partir de bpm)."""

import re
from datetime import timedelta

from django.db.models import F
from django.utils import timezone

from .models import Barra, Compas, MarcaTiempoCompas, Obra, Segmento

_PATRON_INDICACION_COMPAS = re.compile(r'^[1-9]\d*/[1-9]\d*$')

# Armadura de clave (MarcaNotacion tipo='armadura'): 'b'/'#' (bemoles/
# sostenidos) + cantidad opcional 1-7 (bare 'b'/'#' = 1) — acepta el número
# antes del símbolo también ('2b'), y los símbolos ♭/♯ como sinónimos de
# b/#. Vacío = Do mayor/La menor (sin alteraciones).
_PATRON_ARMADURA = re.compile(
    r'^(?:(?P<simbolo1>[b#♭♯])(?P<n1>[1-7])?|(?P<n2>[1-7])(?P<simbolo2>[b#♭♯]))$'
)
_SINONIMOS_ARMADURA = {'♭': 'b', '♯': '#'}


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

    EXCEPTO más allá de una pausa (ver EfectoTempo tipo 'pausa'): su
    compas_desde es un umbral de numeración inamovible (la convención del
    usuario de arrancar cada movimiento en 201/301/...) — nada con numero
    >= ese umbral se corre nunca, aunque el cálculo de acá arriba diga lo
    contrario. Antes de esto, CUALQUIER edición en una página anterior
    corría en silencio el arranque de todos los movimientos siguientes en
    cada reconfirmación — bug real, confirmado en vivo. Mismo criterio del
    lado del cliente, dentro de una sola página, en
    ajuste_barras.difundirNumeros().
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

    # Freno de pausa: nada con numero >= el umbral de la próxima pausa se
    # toca — ver docstring de arriba. Una parte suelta (sin obra todavía,
    # ver Partitura.obra) no tiene EfectoTempo que consultar — sin freno.
    if pagina.partitura.obra_id is not None:
        umbral = next(
            (p['compas_desde'] for p in indice_pausas(pagina.partitura.obra) if p['compas_desde'] > ultimo.numero),
            None,
        )
        if umbral is not None:
            siguientes = siguientes.filter(numero__lt=umbral)

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


def normalizar_armadura(texto):
    """Valida y normaliza el texto de armadura de clave (MarcaNotacion
    tipo='armadura', valor de CONCIERTO — ver armadura_transportada para la
    de cada parte). Vacío = Do mayor/La menor (sin alteraciones). Si no,
    'b'/'#' (bemoles/sostenidos) + cantidad 1-7 (bare 'b' o '#' = 1) — se
    limpian espacios, se acepta el número antes del símbolo ('2b' -> 'b2')
    y los símbolos ♭/♯ como sinónimos de b/#. Levanta ValueError si no
    matchea ese formato."""
    texto = (texto or '').strip()
    if not texto:
        return ''
    compacto = texto.replace(' ', '')
    m = _PATRON_ARMADURA.match(compacto)
    if not m:
        raise ValueError(
            f'"{texto}" no es una armadura válida — usá "b" o "#" seguido opcionalmente '
            'de la cantidad (ej: "b", "b2", "#3"), o dejalo vacío para Do mayor/La menor.'
        )
    simbolo = _SINONIMOS_ARMADURA.get(m.group('simbolo1') or m.group('simbolo2'), m.group('simbolo1') or m.group('simbolo2'))
    cantidad = int(m.group('n1') or m.group('n2') or 1)
    return simbolo if cantidad == 1 else f'{simbolo}{cantidad}'


def _armadura_a_entero(texto):
    """'' -> 0, 'b' -> -1, 'b2' -> -2, '#' -> 1, '#7' -> 7 — asume texto ya
    normalizado (ver normalizar_armadura), no vuelve a validar el formato."""
    texto = (texto or '').strip()
    if not texto:
        return 0
    signo = -1 if texto[0] == 'b' else 1
    resto = texto[1:]
    return signo * (int(resto) if resto else 1)


def _entero_a_armadura(n):
    """Inverso de _armadura_a_entero."""
    if n == 0:
        return ''
    simbolo = 'b' if n < 0 else '#'
    n = abs(n)
    return simbolo if n == 1 else f'{simbolo}{n}'


def armadura_transportada(armadura_concierto, transposicion_semitonos):
    """Arma la armadura ESCRITA (la que lee el instrumentista de esta
    parte) a partir de la armadura de CONCIERTO guardada en la obra
    (MarcaNotacion tipo='armadura') y la transposición del instrumento de
    esa parte (Instrumento.transposicion_semitonos — convención ya usada en
    ensayos/afinación: concierto = escrito + transposicion_semitonos).

    No identifica notas (no hace falta, ver pedido del usuario): sólo corre
    la cantidad de sostenidos/bemoles la cantidad de "quintas" que
    corresponde a la transposición. Cada quinta justa (7 semitonos) hacia
    arriba suma un sostenido (o resta un bemol) — como 7 y 12 son coprimos,
    cada semitono de transposición tiene un desplazamiento equivalente
    único en quintas (mod 12). Sin dato de transposición (instrumento sin
    cargar, o instrumento en Do), devuelve la de concierto tal cual.

    Ejemplo real (clarinete Bb, transposicion_semitonos=-2): armadura de
    concierto "b2" (Sib mayor) → escrita "" (Do mayor) — 2 bemoles de
    concierto no necesitan ninguna alteración para este instrumento."""
    if not transposicion_semitonos:
        return armadura_concierto
    try:
        n_concierto = _armadura_a_entero(armadura_concierto)
    except (ValueError, IndexError):
        # Texto libre de antes de normalizar_armadura (obras viejas, ver
        # MarcaNotacion) — no se puede transportar sin saber cuántas
        # alteraciones son, se muestra tal cual en vez de romper.
        return armadura_concierto
    pasos_quintas = (7 * -transposicion_semitonos) % 12
    if pasos_quintas > 6:
        pasos_quintas -= 12
    n_escrito = n_concierto + pasos_quintas
    n_escrito = ((n_escrito + 6) % 12) - 6
    return _entero_a_armadura(n_escrito)


def renumerar_segmentos(obra):
    """Renumera obra.segmentos de a 10 (10, 20, 30…), preservando el orden
    relativo actual — se corre en cada guardado del itinerario para que una
    inserción futura ("entre medio") siempre tenga hueco disponible, en vez
    de dejar que se vaya agotando de a poco con sucesivas inserciones.

    Se hace en dos pasadas: si se asignara el valor final directamente,
    orden=10 podría chocar contra otra fila que hoy YA tiene orden=10 y
    todavía no fue procesada (viola unique_together (obra, orden)). Pasar
    primero por un rango que no puede colisionar con nada evita eso.

    Se ordena PURO por orden. Antes había un criterio extra que forzaba
    cualquier fila de cierre (compas_desde vacío) siempre al final, sin
    importar su propio orden — tenía sentido cuando sólo podía haber UNA
    fila de cierre (la del fin de la obra) y resolver_segmentos cortaba la
    acumulación de tiempo apenas encontraba la primera. Ahora puede haber
    más de una (una interna, en medio del itinerario, para una pausa entre
    movimientos — ver EfectoTempo tipo 'pausa') y resolver_segmentos ya no
    corta ahí (sigue acumulando después de cada cierre) — forzarlas
    siempre al final volvía a romper exactamente lo que esto necesita:
    bug real, encontrado 2026-07-30 con una obra real de varios
    movimientos, la fila de cierre interna terminaba después del
    movimiento siguiente en vez de entre los dos."""
    segmentos = sorted(obra.segmentos.all(), key=lambda s: s.orden)
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


def _rango_pulsos_del_compas(seg, compas, pulsos_compas):
    """Pulso inicial y final (ambos inclusive) que le corresponden a UN
    compás puntual dentro de esta fila — 1..pulsos_compas salvo que sea el
    primer o el último compás de la fila y pulso_desde/pulso_hasta no lo
    cubran entero (la fila arranca o corta a mitad de compás)."""
    pulso_desde, pulso_hasta = _pulso_bounds(seg, pulsos_compas)
    ini = pulso_desde if compas == seg.compas_desde else 1
    fin = pulso_hasta if compas == seg.compas_hasta else pulsos_compas
    return int(ini), int(fin)


def _pulsos_antes_del_compas(seg, compas, pulsos_por_compas):
    """Cuántos pulsos de la fila (no del compás) ya transcurrieron antes de
    que arranque este compás puntual — ubica al compás dentro de la
    secuencia total de pulsos de la fila, para poder interpolar el tempo
    pulso a pulso en un accelerando/ritardando en vez de saltar de a un
    tempo fijo por compás. pulsos_por_compas es {compas: valor}."""
    total = 0
    for c in range(seg.compas_desde, compas):
        ini, fin = _rango_pulsos_del_compas(seg, c, pulsos_por_compas[c])
        total += fin - ini + 1
    return total


def _pasadas_por_compas(obra):
    """Para cada ocurrencia de compás de la obra (fila, número), qué
    "pasada" le corresponde — en general, cuenta en orden cuántas filas
    navegables contienen ese número de compás (misma idea que usa
    buscar_posicion, que reutiliza este mismo diccionario — única fuente de
    verdad). Calculado una sola vez para toda la obra en vez de buscarlo
    cada vez. Usado acá (construir_plan/resolver_segmentos, para ubicar
    MarcaTiempoCompas/MarcaNotacion puntual) y en compases_desenrollados.

    EXCEPCIÓN — continuación, no repetición: si una fila termina
    EXPLÍCITAMENTE a mitad de compás (pulso_hasta propio) y la fila
    SIGUIENTE en el itinerario, sin nada en el medio, retoma ESE MISMO
    compás desde donde quedó (pulso_desde > 1), es un salto/repetición que
    cae a mitad de compás — no un compás que se vuelve a tocar de cero. Le
    corresponde la MISMA pasada que la fila anterior, sin incrementar el
    contador. Una repetición real de un compás entero siempre tiene otras
    filas en el medio (el tramo repetido), así que nunca se confunde con
    este caso.

    Devuelve {(segmento_id, compas): pasada}."""
    contador = {}
    resultado = {}
    seg_anterior = None
    for seg in segmentos_navegables(obra):
        continua_de_anterior = (
            seg_anterior is not None
            and seg_anterior.compas_hasta == seg.compas_desde
            and seg_anterior.pulso_hasta is not None
            and seg.pulso_desde is not None and seg.pulso_desde > 1
        )
        for compas in range(seg.compas_desde, seg.compas_hasta + 1):
            if continua_de_anterior and compas == seg.compas_desde:
                pasada = resultado[(seg_anterior.id, compas)]
            else:
                contador[compas] = contador.get(compas, 0) + 1
                pasada = contador[compas]
            resultado[(seg.id, compas)] = pasada
        seg_anterior = seg
    return resultado


def _pulsos_por_compas_de_fila(seg, notacion_por_compas):
    """{compas: pulsos_por_compas} de TODOS los compases de esta fila —
    sólo llamar cuando ya se sabe que la fila resuelve completa (ver
    _posiciones_calculadas_fila, que devuelve None si no)."""
    return {
        compas: notacion_por_compas[(seg.id, compas)]['pulsos_compas']
        for compas in range(seg.compas_desde, seg.compas_hasta + 1)
    }


def _posiciones_calculadas_fila(seg, notacion_por_compas, bpm_por_pulso, factor_por_pulso):
    """Posiciones (tiempo calculado acumulado, en segundos desde el
    arranque de la fila, ANTES de cada pulso — posiciones[k] es el
    instante en que arranca el pulso k, 0-based; posiciones[-1] es la
    duración calculada total de la fila). bpm/indicación de compás se
    resuelven COMPÁS A COMPÁS contra notacion_por_compas (pueden cambiar a
    mitad de la fila, ver MarcaNotacion/_resolver_notacion_por_compas) en
    vez de asumir un único valor para toda la fila.

    bpm_por_pulso/factor_por_pulso (ver _resolver_bpm_por_pulso) ya traen
    cualquier rampa de EfectoTempo (accelerando/ritardando) y factor de
    calderón aplicados — esta función no necesita saber nada de esa lógica,
    sólo hace el lookup por (segmento_id, compas, pulso), con el 'bpm' de
    notacion_por_compas como fallback si el pulso no está en el dict (fuera
    de cualquier rampa/propagación).

    None si la fila no tiene compas_hasta, o si CUALQUIER compás de su
    rango no resuelve bpm/pulsos_por_compas — señal única de "incompleto"
    (antes esto sólo se chequeaba en compas_desde, asumiendo que el resto
    de la fila resolvía igual; ver construir_plan/_anclas_globales, que
    usan este None para saltear/marcar incompleta TODA la fila).

    Usado por _anclas_globales (encadenando fila tras fila para la
    posición global de las dos fuentes) y por construir_plan/
    tiempo_real_ancla (para ubicar un pulso puntual dentro de su propia
    fila)."""
    if seg.compas_hasta is None:
        return None
    pulsos_por_compas = {}
    bpm_por_compas = {}
    for compas in range(seg.compas_desde, seg.compas_hasta + 1):
        info_c = notacion_por_compas.get((seg.id, compas))
        if not info_c or not info_c.get('bpm') or not info_c.get('pulsos_compas'):
            return None
        pulsos_por_compas[compas] = info_c['pulsos_compas']
        bpm_por_compas[compas] = info_c['bpm']

    posiciones = [0.0]
    for compas in range(seg.compas_desde, seg.compas_hasta + 1):
        ini, fin = _rango_pulsos_del_compas(seg, compas, pulsos_por_compas[compas])
        for p in range(ini, fin + 1):
            bpm_pulso = bpm_por_pulso.get((seg.id, compas, p), bpm_por_compas[compas])
            factor = factor_por_pulso.get((seg.id, compas, p), 1.0)
            posiciones.append(posiciones[-1] + 60.0 / bpm_pulso * factor)
    return posiciones


def _anclas_globales(todos_los_segmentos, notacion_por_compas, pasadas_por_compas, marcas_por_compas_pasada,
                      bpm_por_pulso, factor_por_pulso, marcas_pulso_por_compas_pasada, pausas=None):
    """Anclas reales "por compases", para la obra ENTERA de una sola vez, en
    la MISMA posición calculada por bpm (ver _posiciones_calculadas_fila) —
    acumulada globalmente, cruzando filas del itinerario sin resetear (mismo
    criterio que tiempo_inicio_calculado de compases_desenrollados). Ver
    _tiempo_real_en_posicion:

    - anclas_compases: (posición, tiempo real) de cada MarcaTiempoCompas
      puntual, MÁS cada MarcaTiempoPulso puntual (ver
      marcas_pulso_por_compas_pasada) — un pulso corregido a mano es sólo
      un punto más PRECISO de la misma lista, nunca una fuente aparte: dos
      compases vecinos en la reproducción real pueden interpolar entre sí
      siguiendo la curva de bpm, aunque el itinerario los haya partido en
      filas distintas (p.ej. un compás de anacrusis solo en su propia fila,
      o un acelerando que cruza un borde de fila).

    todos_los_segmentos: TODAS las filas de la obra en orden, no sólo las
    navegables (con contenido) — a diferencia de antes, esta función
    necesita ver también las filas de cierre (Segmento.compas_hasta None)
    para intercalar sus anclas reales en el punto exacto donde ocurren, no
    sólo al final: hoy puede haber más de una (una fila de PAUSA entre
    movimientos, además de la final de la obra — ver Segmento). Una fila
    de cierre es cualquiera con compas_hasta vacío — compas_desde vacío
    también es la fila de cierre FINAL (fin de la obra); compas_desde con
    un número es una PAUSA, y ese número es el mismo umbral que su
    EfectoTempo tipo 'pausa' correspondiente (ver docstring de ese
    modelo) — así el emparejamiento es directo (por número exacto), no
    posicional.

    pausas (opcional, ver indice_pausas): da una duración ESTIMADA para el
    hueco de una fila de PAUSA que todavía no tiene tiempo real tapeado —
    sólo para que la posición estimada de lo que sigue no ignore por
    completo el hueco (evita que un tramo de silencio real se reparta como
    si fuera duración de los pocos pulsos reales vecinos). No genera una
    ancla propia: una vez que el hueco SÍ tiene tiempo real, ese real es la
    única ancla ahí, la estimación no compite con él.

    Devuelve (anclas_compases, posicion_inicio: {(segmento_id, compas):
    posición calculada del primer pulso de esa ocurrencia}) — filas sin bpm/
    indicación resuelta (plan incompleto, ver construir_plan) se saltean: no
    se puede calcular sus posiciones."""
    anclas_compases = []
    posicion_inicio = {}
    pasadas_ya_ancladas = set()  # (compas, pasada) — ver nota abajo
    valor_por_umbral = {p['compas_desde']: p['valor_segundos'] for p in (pausas or [])}
    offset_global = 0.0
    for seg in todos_los_segmentos:
        if seg.compas_hasta is None:
            if seg.tiempo_inicio is not None:
                anclas_compases.append((offset_global, seg.tiempo_inicio.total_seconds()))
            # El offset ESTIMADO avanza igual haya o no tap real — es el
            # reloj puramente nominal (por bpm), no se corrige con lo real
            # (eso lo hace la interpolación entre anclas, aparte) — sin
            # este avance, las posiciones de todo lo que sigue quedarían
            # cortas por la duración de la pausa, distorsionando cualquier
            # ancla real MÁS ADELANTE que dependa de una interpolación
            # cruzando este punto.
            if seg.compas_desde is not None:
                valor = valor_por_umbral.get(seg.compas_desde)
                if valor is not None:
                    offset_global += valor
            continue

        posiciones_fila = _posiciones_calculadas_fila(seg, notacion_por_compas, bpm_por_pulso, factor_por_pulso)
        if posiciones_fila is None:
            continue
        pulsos_por_compas_fila = _pulsos_por_compas_de_fila(seg, notacion_por_compas)
        for compas in range(seg.compas_desde, seg.compas_hasta + 1):
            idx_compas = _pulsos_antes_del_compas(seg, compas, pulsos_por_compas_fila)
            posicion = offset_global + posiciones_fila[idx_compas]
            posicion_inicio[(seg.id, compas)] = posicion
            pasada = pasadas_por_compas.get((seg.id, compas))
            marca = marcas_por_compas_pasada.get((compas, pasada)) if pasada else None
            # Un compás partido en dos filas por continuidad comparte la
            # MISMA pasada fusionada (ver _pasadas_por_compas) — sin este
            # chequeo, el segundo fragmento agregaría la misma marca real
            # de nuevo a una posición calculada distinta, creando un tramo
            # de duración real CERO entre ambas (mismo tiempo, dos
            # posiciones). Anclar sólo la primera vez reparte el tiempo
            # real proporcionalmente entre los dos fragmentos, contra la
            # próxima ancla real de verdad.
            if marca is not None and (compas, pasada) not in pasadas_ya_ancladas:
                anclas_compases.append((posicion, marca.total_seconds()))
                pasadas_ya_ancladas.add((compas, pasada))

            marcas_pulso = marcas_pulso_por_compas_pasada.get((compas, pasada)) if pasada else None
            if marcas_pulso:
                pulso_ini_compas, pulso_fin_compas = _rango_pulsos_del_compas(
                    seg, compas, pulsos_por_compas_fila[compas],
                )
                for pulso, tiempo_pulso in marcas_pulso.items():
                    # Igual que con las marcas de compás: un compás partido
                    # en dos filas por continuidad reparte sus pulsos en
                    # rangos SIN solapar entre los fragmentos — sólo el
                    # fragmento que de verdad cubre este pulso lo ancla.
                    if pulso_ini_compas <= pulso <= pulso_fin_compas:
                        idx_en_fila = idx_compas + (pulso - pulso_ini_compas)
                        anclas_compases.append((offset_global + posiciones_fila[idx_en_fila], tiempo_pulso))
        offset_global += posiciones_fila[-1]
    anclas_compases.sort(key=lambda a: a[0])
    return anclas_compases, posicion_inicio


def _bracket_anclas_invertido(anclas, posicion):
    """True si el par de anclas consecutivas que encierran `posicion` (mismo
    criterio de búsqueda que _tiempo_real_en_posicion) tiene el tiempo real
    de la ancla de ADELANTE menor que el de la de ATRÁS — un tapeo
    contradictorio (la que va después en la partitura quedó marcada con un
    tiempo real anterior). None/False si coincide exacto con una ancla (no
    hay bracket que evaluar) o si la posición queda fuera del tramo
    cubierto (mismos casos en los que _tiempo_real_en_posicion no
    interpola nada)."""
    for pos_a, _t_a in anclas:
        if pos_a == posicion:
            return False
    if len(anclas) < 2 or posicion < anclas[0][0] or posicion > anclas[-1][0]:
        return False
    ultimo_tramo = len(anclas) - 2
    for i, ((pos_a, t_a), (pos_b, t_b)) in enumerate(zip(anclas, anclas[1:])):
        if pos_a <= posicion < pos_b or (i == ultimo_tramo and posicion == pos_b):
            return t_b < t_a
    return False


_EPSILON_POSICION = 1e-4  # ver _tiempo_real_en_posicion — ruido de punto flotante
# acumulado tras cientos de sumas (60/bpm por pulso), NUNCA una duración real:
# dos sumas matemáticamente iguales pueden diferir en el último bit según el
# orden en que se hicieron (posiciones_fila acumulado vs. posicion_pulso +
# duracion_pulso recalculado — ver construir_plan). Sin esta tolerancia, el
# ÚLTIMO pulso de la obra puede quedar a 1e-14 de su propia ancla de cierre y
# devolver None (visto en producción, obra "Y ahora soy feliz": el pulso caía
# a 5.68e-14 de la ancla, y el corte estricto lo descartaba igual).


def _tiempo_real_en_posicion(anclas, posicion):
    """Tiempo real interpolado linealmente en una posición calculada
    arbitraria (no necesariamente el arranque de un pulso, puede caer a
    mitad de uno) — busca en qué tramo (entre qué par de anclas
    consecutivas, ver _anclas_globales) cae la posición e interpola.
    None si la posición cae fuera del tramo cubierto (o hay menos de dos
    anclas) — sin inventar una extrapolación. Excepción: si la posición
    coincide con una ancla (exacto, o a menos de _EPSILON_POSICION — ver esa
    constante), se devuelve directo su tiempo real, aunque sea la única que
    haya — ahí no hace falta una segunda para interpolar nada, ya está
    marcada."""
    if anclas:
        if abs(posicion - anclas[0][0]) < _EPSILON_POSICION:
            posicion = anclas[0][0]
        elif abs(posicion - anclas[-1][0]) < _EPSILON_POSICION:
            posicion = anclas[-1][0]
    for pos_a, t_a in anclas:
        if pos_a == posicion:
            return t_a
    if len(anclas) < 2 or posicion < anclas[0][0] or posicion > anclas[-1][0]:
        return None
    ultimo_tramo = len(anclas) - 2
    for i, ((pos_a, t_a), (pos_b, t_b)) in enumerate(zip(anclas, anclas[1:])):
        if pos_a <= posicion < pos_b or (i == ultimo_tramo and posicion == pos_b):
            if pos_b == pos_a:
                return t_a
            frac = (posicion - pos_a) / (pos_b - pos_a)
            return t_a + frac * (t_b - t_a)
    return None


def _indice_notacion(obra):
    """Marcas de MarcaNotacion de la obra, separadas por tipo, listas para
    resolver "qué rige en el compás N": generales (pasada=None, ordenadas
    por compás — se busca la última con compas <= N) y puntuales (override
    de una (compas, pasada) exacta, ver _resolver_marca_notacion)."""
    generales = {}
    puntuales = {}
    for m in obra.marcas_notacion.order_by('compas'):
        if m.pasada is None:
            generales.setdefault(m.tipo, []).append((m.compas, m.valor))
        else:
            puntuales[(m.tipo, m.compas, m.pasada)] = m.valor
    return generales, puntuales


def _resolver_marca_notacion(indice, tipo, compas, pasada):
    """Valor vigente de `tipo` (compas/armadura/tempo) en un compás puntual
    de la obra — override de (compas, pasada) si existe, si no la última
    marca general con compas_marca <= compas (búsqueda hacia atrás: esto ES
    la herencia, por posición real en la obra, no por orden de itinerario).
    None si no hay ninguna marca aplicable. pasada=None (fila a medio
    cargar, sin compas_hasta todavía) sólo mira las generales, nunca un
    override puntual."""
    if compas is None:
        return None
    generales, puntuales = indice
    if pasada is not None:
        valor = puntuales.get((tipo, compas, pasada))
        if valor is not None:
            return int(valor) if tipo == 'tempo' else valor
    valor = None
    for compas_marca, v in generales.get(tipo, []):
        if compas_marca <= compas:
            valor = v
        else:
            break
    return int(valor) if (valor is not None and tipo == 'tempo') else valor


def _resolver_notacion_por_compas(segmentos, indice, pasadas_por_compas):
    """indicacion_compas/armadura/bpm resueltos COMPÁS A COMPÁS (no una sola
    vez por fila) contra la capa de notación (MarcaNotacion) — a diferencia
    de mirar sólo seg.compas_desde, cualquier compás a mitad de una fila
    recibe la marca que le corresponda por POSICIÓN, sin depender de que
    haya un borde de fila justo ahí (bug real: una fila de compás 1 a 77
    con una marca de tempo nueva en compás 10 quedaba pegada al tempo de
    compás 1 para todo el tramo, porque antes sólo se resolvía en
    compas_desde).

    `segmentos`: TODOS los Segmento de la obra en orden (no sólo los
    navegables) — así el "vigente" de fallback (ver abajo) sale igual que
    si se recorriera fila por fila, incluida la de cierre.

    "Vigente" (cuando _resolver_marca_notacion no encuentra ninguna marca
    aplicable — sólo pasa si falta la marca general del principio de la
    obra) se lleva COMPÁS A COMPÁS, cruzando también fronteras de fila —
    generalización estricta del mecanismo que antes se llevaba una vez por
    fila: da resultado idéntico a cuando no hay ningún cambio a mitad de
    fila (ningún caso real tenía uno hasta ahora).

    NO conoce nada de accelerando/ritardando/calderón (EfectoTempo, ver
    _resolver_bpm_por_pulso) — el 'bpm' que devuelve es el marcado/vigente
    puro de MarcaNotacion, sin ninguna rampa aplicada todavía (antes de
    Fase 2, Segmento.bpm_llegada congelaba este valor para toda su fila;
    ya no hace falta, la rampa se aplica río abajo, por pulso, sin que
    esta función tenga que saber nada de eso).

    Devuelve (por_fila, por_compas):
    - por_fila: {segmento_id: {indicacion_compas, armadura, bpm,
      pulsos_compas}} — el valor en compas_desde de cada fila (incluye
      filas a medio cargar, sin compas_hasta todavía, y la de cierre con
      los valores heredados del último vigente) — mismo contrato que el
      'info' que devolvía resolver_segmentos antes de este cambio.
    - por_compas: {(segmento_id, compas): {...}} — todos los compases de
      cada fila CON RANGO COMPLETO (compas_hasta no vacío) — nuevo."""
    por_fila = {}
    por_compas = {}
    indicacion_vigente = None
    bpm_vigente = None
    for seg in segmentos:
        if seg.compas_hasta is None:
            # Fila de cierre: sin compás propio (sea la final de la obra,
            # compas_desde vacío, o una pausa entre movimientos, con su
            # umbral en compas_desde — ninguna de las dos tiene un compás
            # real que resolver) — pero resolver_segmentos siempre le
            # calculaba un 'info' heredado del vigente (aunque nada más lo
            # consuma hoy) — se replica para no cambiar su contrato
            # público.
            por_fila[seg.id] = {
                'indicacion_compas': indicacion_vigente,
                'armadura': None,
                'bpm': bpm_vigente,
                'pulsos_compas': _pulsos_por_compas(indicacion_vigente),
            }
            continue

        for i, compas in enumerate(range(seg.compas_desde, seg.compas_hasta + 1)):
            pasada = pasadas_por_compas.get((seg.id, compas))

            propio_indicacion = _resolver_marca_notacion(indice, 'compas', compas, pasada)
            indicacion = propio_indicacion or indicacion_vigente
            if indicacion:
                indicacion_vigente = indicacion

            propio_bpm = _resolver_marca_notacion(indice, 'tempo', compas, pasada)
            bpm_marcado = propio_bpm or bpm_vigente
            if propio_bpm:
                bpm_vigente = propio_bpm

            armadura = _resolver_marca_notacion(indice, 'armadura', compas, pasada)

            entrada = {
                'indicacion_compas': indicacion,
                'armadura': armadura,
                'bpm': bpm_marcado,
                'pulsos_compas': _pulsos_por_compas(indicacion),
            }
            if i == 0:
                por_fila[seg.id] = entrada
            if seg.compas_hasta is not None:
                por_compas[(seg.id, compas)] = entrada
    return por_fila, por_compas


def _pulsos_compas_global(notacion_por_compas):
    """{compas: pulsos_compas}, aplanado de notacion_por_compas (indexado
    por (segmento_id, compas)) — pulsos_compas es una propiedad de POSICIÓN
    (viene de la indicación de compás vigente ahí, ver
    _resolver_notacion_por_compas), da lo mismo qué fila lo resolvió. Se
    usa para medir el largo en pulsos de un EfectoTempo, que no conoce de
    qué fila viene (ver _pulsos_entre_posiciones)."""
    return {
        compas: info['pulsos_compas']
        for (_, compas), info in notacion_por_compas.items()
        if info.get('pulsos_compas')
    }


def _pulsos_entre_posiciones(compas_desde, pulso_desde, compas_hasta, pulso_hasta, pulsos_compas_por_compas):
    """Cantidad de pulsos ENTEROS entre dos posiciones compás+pulso de la
    obra (ambos extremos inclusive, truncados a entero — un EfectoTempo
    siempre queda anclado a un pulso entero, igual que _rango_pulsos_del_compas).
    None si falta resolver algún compás del rango (notación incompleta ahí,
    ver _pulsos_compas_global) — el efecto se descarta en ese caso, ver
    _resolver_rampas."""
    total = 0
    for compas in range(compas_desde, compas_hasta + 1):
        pulsos_compas = pulsos_compas_por_compas.get(compas)
        if not pulsos_compas:
            return None
        ini = int(pulso_desde) if (compas == compas_desde and pulso_desde) else 1
        fin = int(pulso_hasta) if (compas == compas_hasta and pulso_hasta) else int(pulsos_compas)
        total += fin - ini + 1
    return total


def _resolver_rampas(obra, notacion_por_compas):
    """EfectoTempo accelerando/ritardando de la obra, con su cantidad total
    de pulsos ya resuelta (ver _pulsos_entre_posiciones) — usada más abajo
    para la fracción de interpolación de bpm (ver _resolver_bpm_por_pulso).
    Descarta en silencio (no rampea) los que caigan en un rango sin
    notación resuelta. Ordenadas por posición de arranque."""
    pulsos_compas_global = _pulsos_compas_global(notacion_por_compas)
    rampas = []
    for e in obra.efectos_tempo.exclude(tipo__in=('calderon', 'pausa')).order_by('compas_desde', 'pulso_desde'):
        pulso_desde = e.pulso_desde if e.pulso_desde is not None else 1.0
        if e.compas_hasta is None:
            continue
        total = _pulsos_entre_posiciones(e.compas_desde, pulso_desde, e.compas_hasta, e.pulso_hasta, pulsos_compas_global)
        if total is None:
            continue
        pulso_hasta_resuelto = int(e.pulso_hasta) if e.pulso_hasta is not None else int(pulsos_compas_global[e.compas_hasta])
        try:
            bpm_llegada = int(e.valor)
        except (TypeError, ValueError):
            continue
        rampas.append({
            'compas_desde': e.compas_desde,
            'pulso_desde': int(pulso_desde),
            'compas_hasta': e.compas_hasta,
            'pulso_hasta': pulso_hasta_resuelto,
            'bpm_llegada': bpm_llegada,
            'tipo': e.tipo,
            'tipo_display': e.get_tipo_display(),
            'total_pulsos': total,
        })
    rampas.sort(key=lambda r: (r['compas_desde'], r['pulso_desde']))
    return rampas


def _resolver_saltos(obra):
    """Puntos de salto del itinerario: donde una fila de Segmento no
    continúa exactamente donde terminó la anterior (ver Segmento.__doc__)
    — repetición, D.C./D.S., al Coda, lo que sea; no importa el motivo,
    sólo el hueco real entre el fin de una fila y el arranque de la
    siguiente. Reutiliza el mismo criterio de continuidad a mitad de
    compás que _pasadas_por_compas (continua_mismo_compas más abajo) para
    no confundir un corte de fila por motivos internos con un salto real.

    Uno por cada PAR distinto (compas_desde, compas_hasta) — aunque el
    itinerario pase por el mismo salto más de una vez (p.ej. una
    repetición visitada varias veces en un rondó), es el mismo lugar de la
    partitura y no tiene sentido marcarlo dos veces. Pensada para el
    dibujo de partida/llegada en el navegador (ver dibujarSaltos en
    navegador_obra.html), independiente de cambios/rampas."""
    navegables = segmentos_navegables(obra)
    vistos = set()
    saltos = []
    anterior = None
    for seg in navegables:
        if anterior is not None:
            continua_mismo_compas = (
                anterior.compas_hasta == seg.compas_desde
                and anterior.pulso_hasta is not None
                and seg.pulso_desde is not None and seg.pulso_desde > 1
            )
            continua_siguiente_compas = (
                seg.compas_desde == anterior.compas_hasta + 1
                and anterior.pulso_hasta is None
                and (seg.pulso_desde is None or seg.pulso_desde <= 1)
            )
            if not (continua_mismo_compas or continua_siguiente_compas):
                par = (anterior.compas_hasta, seg.compas_desde)
                if par not in vistos:
                    vistos.add(par)
                    saltos.append({'compas_desde': par[0], 'compas_hasta': par[1]})
        anterior = seg
    return saltos


def _indice_calderones(obra):
    """{(compas, pulso_entero): factor} de los EfectoTempo tipo calderón —
    ver _resolver_bpm_por_pulso."""
    calderones = {}
    for e in obra.efectos_tempo.filter(tipo='calderon'):
        pulso = int(e.pulso_desde) if e.pulso_desde is not None else 1
        try:
            calderones[(e.compas_desde, pulso)] = float(e.valor)
        except (TypeError, ValueError):
            continue
    return calderones


def indice_pausas(obra):
    """EfectoTempo tipo 'pausa' de la obra, resueltas a dicts simples
    {compas_desde, valor_segundos} y ordenadas por compas_desde — a
    diferencia de calderón/rampas, acá compas_desde NO es la posición de
    un compás real: es el umbral inamovible de numeración elegido a mano
    (ver docstring de EfectoTempo). Consumidas en ese orden por
    _anclas_globales/resolver_segmentos/compases_desenrollados, que le dan
    su duración ESTIMADA (valor_segundos) al hueco de la fila de cierre
    (Segmento.compas_desde None) que cae justo antes del primer compás
    real numerado >= ese umbral — sólo mientras esa fila no tenga todavía
    un tiempo real tapeado (ahí el real gana siempre)."""
    pausas = []
    for e in obra.efectos_tempo.filter(tipo='pausa').order_by('compas_desde'):
        try:
            valor = float(e.valor)
        except (TypeError, ValueError):
            continue
        pausas.append({'compas_desde': e.compas_desde, 'valor_segundos': valor})
    return pausas


def _indice_marcas_pulso(obra):
    """{(compas, pasada): {pulso: segundos}} de MarcaTiempoPulso — ancla más
    precisa de la misma fuente "por compases" (ver _anclas_globales), nunca
    una fuente aparte."""
    marcas_pulso = {}
    for m in obra.marcas_tiempo_pulso.all():
        marcas_pulso.setdefault((m.compas, m.pasada), {})[m.pulso] = m.tiempo_inicio.total_seconds()
    return marcas_pulso


def _resolver_bpm_por_pulso(obra, navegables, notacion_por_compas, pasadas_por_compas, indice, rampas, calderones):
    """Recorre TODA la obra pulso a pulso, en el mismo orden real de
    reproducción que camina construir_plan (cruzando filas del itinerario,
    ver avanzar_compas), UNA sola vez — necesario porque una rampa de
    EfectoTempo puede cruzar un límite de fila, y hace falta saber cuántos
    de sus pulsos ya pasaron de forma acumulada, no aislada por fila (a
    diferencia de la vieja Segmento.bpm_llegada, que sólo rampeaba dentro
    de su propia fila).

    También propaga hacia adelante el bpm de LLEGADA de la última rampa
    concluida, hasta la próxima MarcaNotacion de tempo fresca o la próxima
    rampa — mismo criterio que antes tenía Segmento.bpm_llegada -> bpm_vigente
    en _resolver_notacion_por_compas, pero por posición en vez de por fila.

    Devuelve (bpm_por_pulso, factor_por_pulso, variacion_por_pulso), todos
    con clave (segmento_id, compas, pulso):
    - bpm_por_pulso: bpm efectivo de CADA pulso visitado (con cualquier
      rampa/propagación ya aplicada) — el llamador ya no necesita mirar
      notacion_por_compas para el bpm de un pulso puntual.
    - factor_por_pulso: factor de calderón (ausente = 1.0, sin calderón ahí).
    - variacion_por_pulso: (tipo_display, bpm_llegada) sólo en los pulsos
      dentro de una rampa activa — para los campos de display que antes
      salían de seg.get_variacion_tempo_display()/seg.bpm_llegada."""
    bpm_por_pulso = {}
    factor_por_pulso = {}
    variacion_por_pulso = {}
    if not navegables:
        return bpm_por_pulso, factor_por_pulso, variacion_por_pulso

    generales, puntuales = indice
    marcas_tempo_frescas = {compas_marca for compas_marca, _ in generales.get('tempo', [])}

    def _hay_marca_tempo_fresca(compas, pasada):
        # A diferencia de _resolver_marca_notacion (que devuelve el valor
        # VIGENTE, heredado de una marca anterior), acá hace falta saber si
        # hay una marca NUEVA justo en este compás — si no, cualquier
        # compás posterior a una rampa ya concluida pisaría el bpm de
        # llegada propagado (bpm_vigente) con el mismo valor heredado de
        # siempre, en vez de dejarlo como quedó la rampa.
        if pasada is not None and puntuales.get(('tempo', compas, pasada)) is not None:
            return True
        return compas in marcas_tempo_frescas

    bpm_vigente = None
    rampa_activa = None
    bpm_inicio_rampa = None
    pulsos_en_rampa = 0

    pos = (navegables[0], navegables[0].compas_desde)
    while pos is not None:
        seg, compas = pos
        info_c = notacion_por_compas.get((seg.id, compas))
        if not info_c or not info_c.get('bpm') or not info_c.get('pulsos_compas'):
            pos = avanzar_compas(obra, seg, compas)
            continue

        pasada = pasadas_por_compas.get((seg.id, compas))
        pulso_ini, pulso_fin = _rango_pulsos_del_compas(seg, compas, info_c['pulsos_compas'])

        if _hay_marca_tempo_fresca(compas, pasada) and rampa_activa is None:
            bpm_vigente = info_c['bpm']

        for p in range(pulso_ini, pulso_fin + 1):
            if rampa_activa is None:
                for r in rampas:
                    if r['compas_desde'] == compas and r['pulso_desde'] == p:
                        rampa_activa = r
                        pulsos_en_rampa = 0
                        bpm_inicio_rampa = bpm_vigente if bpm_vigente is not None else info_c['bpm']
                        break

            if rampa_activa is not None:
                total = rampa_activa['total_pulsos']
                fraccion = min(1.0, pulsos_en_rampa / (total - 1)) if total > 1 else 1.0
                bpm_pulso = bpm_inicio_rampa + (rampa_activa['bpm_llegada'] - bpm_inicio_rampa) * fraccion
                variacion_por_pulso[(seg.id, compas, p)] = (rampa_activa['tipo_display'], rampa_activa['bpm_llegada'])
                pulsos_en_rampa += 1
            else:
                bpm_pulso = bpm_vigente if bpm_vigente is not None else info_c['bpm']

            bpm_por_pulso[(seg.id, compas, p)] = bpm_pulso

            factor = calderones.get((compas, p))
            if factor is not None:
                factor_por_pulso[(seg.id, compas, p)] = factor

            if rampa_activa is not None and rampa_activa['compas_hasta'] == compas and rampa_activa['pulso_hasta'] == p:
                bpm_vigente = rampa_activa['bpm_llegada']
                rampa_activa = None

        pos = avanzar_compas(obra, seg, compas)

    return bpm_por_pulso, factor_por_pulso, variacion_por_pulso


def resolver_segmentos(obra):
    """Recorre los segmentos de la obra en orden, resolviendo lo que en cada
    fila quedó en blanco (hereda de la última fila con un valor propio — ver
    ayuda de cada campo en el modelo Segmento) y calculando cuánto dura cada
    tramo a partir de bpm/bpm_llegada. Devuelve una lista de dicts, uno por
    segmento, en el mismo orden, cada uno con:
      segmento, indicacion_compas, bpm, armadura (resueltos EN compas_desde
      — ver _resolver_notacion_por_compas para resolver un compás puntual
      cualquiera dentro del rango de una fila, dado que puede cambiar a
      mitad de tramo),
      pulsos_por_compas, duracion_calculada (segundos), tiempo_inicio_calculado (segundos)

    indicacion_compas/bpm/armadura salen de MarcaNotacion (hecho de la
    partitura, por POSICIÓN — ver ese modelo), no de campos de Segmento; lo
    mismo vale para accelerando/ritardando/calderón (EfectoTempo, ver
    _resolver_bpm_por_pulso) — nada de esto depende de la fila del
    itinerario, sólo de la posición real en la obra.

    Si en algún punto falta bpm o indicación de compás para calcular, la
    acumulación de tiempo se corta ahí — el resto de las filas quedan con
    tiempo_inicio_calculado en None en vez de inventar un valor con un
    tempo/indicación por defecto que nadie pidió.

    Una fila de cierre (compas_hasta vacío) puede aparecer más de una vez,
    no sólo al final (ver Segmento) — compas_desde vacío es la fila de
    cierre FINAL; compas_desde con un número es una PAUSA, y ese número es
    el mismo umbral que su EfectoTempo tipo 'pausa' — al llegar a una, si
    existe esa pausa, su duración ESTIMADA (valor_segundos) se suma antes
    de seguir acumulando, para que el resto de la obra no arranque como si
    la pausa durara cero segundos."""
    segmentos = list(obra.segmentos.order_by('orden'))
    indice = _indice_notacion(obra)
    pasadas_por_compas = _pasadas_por_compas(obra)
    por_fila, por_compas = _resolver_notacion_por_compas(segmentos, indice, pasadas_por_compas)
    navegables = segmentos_navegables(obra)
    rampas = _resolver_rampas(obra, por_compas)
    calderones = _indice_calderones(obra)
    valor_por_umbral = {p['compas_desde']: p['valor_segundos'] for p in indice_pausas(obra)}
    bpm_por_pulso, factor_por_pulso, _ = _resolver_bpm_por_pulso(
        obra, navegables, por_compas, pasadas_por_compas, indice, rampas, calderones,
    )

    resueltos = []
    tiempo_acumulado = 0.0

    for seg in segmentos:
        entrada = por_fila.get(seg.id, {})
        info = {
            'segmento': seg,
            'indicacion_compas': entrada.get('indicacion_compas'),
            'bpm': entrada.get('bpm'),
            'armadura': entrada.get('armadura'),
            'pulsos_por_compas': entrada.get('pulsos_compas'),
            'duracion_calculada': None,
            'tiempo_inicio_calculado': tiempo_acumulado,
        }
        resueltos.append(info)

        if seg.compas_hasta is None:
            if tiempo_acumulado is not None and seg.compas_desde is not None:
                valor = valor_por_umbral.get(seg.compas_desde)
                if valor is not None:
                    tiempo_acumulado += valor
            continue

        if tiempo_acumulado is None:
            continue  # ya se cortó la acumulación en una fila anterior

        posiciones = _posiciones_calculadas_fila(seg, por_compas, bpm_por_pulso, factor_por_pulso)
        if posiciones is None:
            tiempo_acumulado = None
            continue

        duracion = posiciones[-1]
        info['duracion_calculada'] = duracion
        tiempo_acumulado += duracion

    return resueltos


def resolver_notacion_en_compas(obra, segmento, compas):
    """indicacion_compas/armadura/bpm/pulsos_compas vigentes en UN compás
    puntual dentro del rango de `segmento` — a diferencia de
    resolver_segmentos, que sólo resuelve el ARRANQUE de cada fila (ver
    _resolver_notacion_por_compas). Pensado para el primer render (del
    lado del servidor) de navegador_obra.html en compas_actual, que puede
    no ser compas_desde de su fila."""
    indice = _indice_notacion(obra)
    pasadas_por_compas = _pasadas_por_compas(obra)
    todos_los_segmentos = list(obra.segmentos.order_by('orden'))
    _, por_compas = _resolver_notacion_por_compas(todos_los_segmentos, indice, pasadas_por_compas)
    return por_compas.get((segmento.id, compas), {})


def resolver_efecto_tempo_en_compas(obra, segmento, compas):
    """(tipo_display, bpm_llegada) de la rampa de EfectoTempo activa en el
    pulso 1 de este compás puntual, o (None, None) si no hay ninguna activa
    ahí — mismo propósito que resolver_notacion_en_compas (primer render del
    lado del servidor de navegador_obra.html), pero para accelerando/
    ritardando en vez de compás/armadura/tempo (ver _resolver_bpm_por_pulso)."""
    indice = _indice_notacion(obra)
    pasadas_por_compas = _pasadas_por_compas(obra)
    todos_los_segmentos = list(obra.segmentos.order_by('orden'))
    _, notacion_por_compas = _resolver_notacion_por_compas(todos_los_segmentos, indice, pasadas_por_compas)
    navegables = segmentos_navegables(obra)
    rampas = _resolver_rampas(obra, notacion_por_compas)
    calderones = _indice_calderones(obra)
    _, _, variacion_por_pulso = _resolver_bpm_por_pulso(
        obra, navegables, notacion_por_compas, pasadas_por_compas, indice, rampas, calderones,
    )
    return variacion_por_pulso.get((segmento.id, compas, 1), (None, None))


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
    compas_desde/compas_hasta. Se apoya en _pasadas_por_compas (única
    fuente de verdad para qué pasada le toca a cada fila) en vez de contar
    coincidencias por su cuenta — así, si un compás está partido en dos
    filas por continuidad (misma pasada fusionada, ver _pasadas_por_compas),
    esto devuelve naturalmente la PRIMERA fila (donde esa pasada realmente
    empieza), no un fragmento suelto a mitad de compás.

    Devuelve (segmento, numero_compas), o None si no hay ninguna
    coincidencia (o no llega a haber esa pasada)."""
    pasadas = _pasadas_por_compas(obra)
    for seg in segmentos_navegables(obra):
        if seg.compas_desde <= numero_compas <= seg.compas_hasta:
            if pasadas.get((seg.id, numero_compas)) == pasada:
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
    compás entero — la UNIDAD del plan sigue siendo el pulso entero (se
    truncan a entero para decidir CUÁL es el primer/último pulso emitido,
    mismo criterio que _rango_pulsos_del_compas), pero si desde_pulso tiene
    parte fraccionaria (p.ej. "5,2.5" = compás 5, mitad del pulso 2), la
    DURACIÓN de ese primer pulso emitido se reduce a la fracción que queda
    por correr (0.5 en el ejemplo) — ver más abajo. Sólo se aplican en la
    primera/última iteración del compás, no en los intermedios: si el rango
    cruza varios compases, todos los del medio se tocan enteros.

    Se arma a nivel de pulso, no de compás, por dos motivos:
    - Un EfectoTempo accelerando/ritardando interpola el bpm de cada PULSO
      linealmente según su posición en la secuencia total de pulsos del
      EFECTO (ver _resolver_bpm_por_pulso) — si se interpolara por compás
      entero, el cambio de tempo saltaría en escalones en cada borde de
      compás en vez de sonar continuo.
    - El primer/último compás de una fila puede no arrancar en el pulso 1
      ni terminar en el último pulso del compás (pulso_desde/pulso_hasta) —
      a nivel de pulso eso sale solo, en vez de tener que tratarlo como
      caso especial en la duración de "ese compás".

    Devuelve (pulsos, completo, cambios, rampas, saltos): pulsos es la lista de dicts (uno
    por pulso, en orden) con segmento_id/compas/pulso/pulsos_por_compas/
    es_primer_pulso_compas/acento/indicacion_compas/bpm/
    variacion_tempo_display/bpm_llegada/descripcion/duracion/
    duracion_compases (duracion en segundos, None si no se pudo resolver
    bpm o indicación para ese compás — plan incompleto, ver "completo" más
    abajo; duracion_compases es la duración escalada SÓLO contra
    MarcaTiempoCompas, None fuera del tramo cubierto por al menos dos
    marcas reales — "no sé", el cliente no debe inventar una posición ahí,
    ver navegador_obra.html; ver _anclas_globales/_tiempo_real_en_posicion.
    pulso/pulsos_por_compas ubican al pulso DENTRO del compás — p.ej. para mover
    el punto del metrónomo dentro de un recuadro en vez de sólo flashear en
    el lugar; es_primer_pulso_compas marca cuándo corresponde refrescar el
    número de compás en pantalla; acento es el pulso 1 musical real, ver
    comentario más abajo); completo es False si algún pulso quedó sin
    duración — el cliente no debería reproducir en tiempo real un plan
    incompleto (esto es independiente de que duracion_compases sea None en
    algún tramo, que es un estado válido, no un plan incompleto); cambios
    es una lista de dicts {compas, tipo, valor} — tipo en
    'compas'/'armadura'/'tempo', uno por cada compás (navegado, no de toda
    la obra) donde ese valor difiere del compás anterior — pensado para el
    aviso visual del navegador (ver dibujarAvisosCambio en
    navegador_obra.html), no confundir con variacion_tempo_display/
    bpm_llegada (que son de un accelerando/ritardando en curso, pulso a
    pulso — acá 'tempo' sólo marca una MarcaNotacion nueva de verdad);
    rampas es la salida de _resolver_rampas tal cual (uno por cada
    EfectoTempo accelerando/ritardando de TODA la obra, no sólo del tramo
    navegado — a diferencia de cambios, no se construye caminando el
    rango, es un índice de posición ya resuelto) — pensada para dibujar
    la raya de "hasta dónde llega" en el navegador (ver dibujarRampas en
    navegador_obra.html), independiente de cambios; saltos es la salida de
    _resolver_saltos tal cual (uno por cada punto distinto de la obra
    ENTERA donde el itinerario no sigue derecho, no sólo del tramo
    navegado — mismo criterio que rampas), pensada para marcar partida/
    llegada en el navegador (ver dibujarSaltos)."""
    navegables = segmentos_navegables(obra)
    if not navegables:
        return [], True, [], [], []

    # La "pasada" de cada ocurrencia de compás, la notación (compás/
    # armadura/tempo) resuelta compás a compás, y las marcas puntuales por
    # compás (ver MarcaTiempoCompas), más la fila de cierre — se arma una
    # sola vez acá (no adentro del loop de abajo) porque _anclas_globales
    # necesita ver TODAS las filas/marcas en orden para ubicar la posición
    # calculada global de cada una.
    todos_los_segmentos = list(obra.segmentos.order_by('orden'))
    indice = _indice_notacion(obra)
    pasadas_por_compas = _pasadas_por_compas(obra)
    _, notacion_por_compas = _resolver_notacion_por_compas(todos_los_segmentos, indice, pasadas_por_compas)
    marcas_por_compas_pasada = {
        (m.compas, m.pasada): m.tiempo_inicio for m in obra.marcas_tiempo_compas.all()
    }
    marcas_pulso_por_compas_pasada = _indice_marcas_pulso(obra)
    pausas = indice_pausas(obra)
    rampas = _resolver_rampas(obra, notacion_por_compas)
    saltos = _resolver_saltos(obra)
    calderones = _indice_calderones(obra)
    bpm_por_pulso, factor_por_pulso, variacion_por_pulso = _resolver_bpm_por_pulso(
        obra, navegables, notacion_por_compas, pasadas_por_compas, indice, rampas, calderones,
    )
    anclas_compases_globales, posicion_inicio_por_seg_compas = _anclas_globales(
        todos_los_segmentos, notacion_por_compas, pasadas_por_compas, marcas_por_compas_pasada,
        bpm_por_pulso, factor_por_pulso, marcas_pulso_por_compas_pasada, pausas,
    )
    posiciones_por_fila = {}  # cache: seg.id -> posiciones (ver _posiciones_calculadas_fila)
    pulsos_por_compas_por_fila = {}  # cache: seg.id -> {compas: pulsos_por_compas}

    # Parte fraccionaria de desde_pulso (ej. 2.5 -> 0.5) — cuánto del primer
    # pulso EMITIDO ya "pasó" antes del punto de arranque pedido; se le
    # resta a su duración más abajo, en las dos fuentes, para que el
    # reloj visual y el audio (posicionado aparte, ver tiempo_real_ancla)
    # queden de acuerdo en cuánto falta de ese pulso.
    fraccion_inicial = (desde_pulso - int(desde_pulso)) if desde_pulso is not None else 0.0

    pos_desde = buscar_posicion(obra, desde_compas, desde_pasada) or (navegables[0], navegables[0].compas_desde)
    if hasta_compas is not None:
        pos_hasta = buscar_posicion(obra, hasta_compas, hasta_pasada) or (navegables[-1], navegables[-1].compas_hasta)
    else:
        pos_hasta = (navegables[-1], navegables[-1].compas_hasta)

    # Cambios de indicación de compás/armadura/tempo respecto del compás
    # NAVEGADO anterior (no del anterior en la partitura entera — si el
    # rango pedido arranca a mitad de obra, el primer compás emitido nunca
    # cuenta como "cambio", no hay nada previo con qué compararlo en este
    # plan). Usa el 'bpm' NOMINAL de notacion_por_compas (la marca vigente),
    # no el bpm ya interpolado pulso a pulso (bpm_por_pulso) — un
    # accelerando/ritardando no dispara un aviso por cada compás de la
    # rampa, sólo una marca de tempo nueva de verdad cambia esto. Pensado
    # para el aviso visual en el navegador (ver navegador_obra.html).
    cambios = []
    cambios_ya_emitidos = set()  # (compas, tipo, valor) — ver comentario abajo
    info_anterior = None

    pulsos = []
    completo = True
    pos = pos_desde
    primera_iteracion = True
    while pos is not None:
        seg, compas = pos
        es_ultima_iteracion = (seg.orden, compas) >= (pos_hasta[0].orden, pos_hasta[1])
        info_c = notacion_por_compas.get((seg.id, compas), {})
        bpm_inicio = info_c.get('bpm')
        pulsos_compas = info_c.get('pulsos_compas')

        if info_anterior is not None:
            for campo, tipo in (('indicacion_compas', 'compas'), ('armadura', 'armadura'), ('bpm', 'tempo')):
                valor_nuevo = info_c.get(campo)
                # Sin este chequeo, un compás que el itinerario revisita más
                # de una vez (una repetición que vuelve a un compás ya
                # tocado antes) generaba el mismo aviso una vez por cada
                # pasada — la partitura física sólo tiene ese compás
                # dibujado una vez, así que el aviso visual (pensado para
                # esa vista física, ver navegador_obra.html) no debe
                # duplicarse aunque la ejecución sí pase dos veces por ahí.
                clave = (compas, tipo, valor_nuevo)
                if valor_nuevo and valor_nuevo != info_anterior.get(campo) and clave not in cambios_ya_emitidos:
                    cambios.append({'compas': compas, 'tipo': tipo, 'valor': valor_nuevo})
                    cambios_ya_emitidos.add(clave)
        info_anterior = info_c

        if seg.id not in posiciones_por_fila:
            posiciones_por_fila[seg.id] = _posiciones_calculadas_fila(seg, notacion_por_compas, bpm_por_pulso, factor_por_pulso)
        posiciones_fila = posiciones_por_fila[seg.id]

        # El gate de completitud no puede mirar sólo el compás actual: un
        # compás roto en OTRO punto de la MISMA fila (falta notación ahí)
        # tira abajo el cálculo de posiciones de toda la fila (ver
        # _posiciones_calculadas_fila), aunque el compás que se está
        # emitiendo acá sí resuelva bien — mismo criterio todo-o-nada que
        # ya usa _anclas_globales.
        if not (bpm_inicio and pulsos_compas and posiciones_fila is not None):
            completo = False
            # Aunque falte bpm (o la fila esté rota en otro punto), si la
            # INDICACIÓN de compás sí resolvió (pulsos_compas truthy) ya se
            # sabe el rango real de pulsos de este compás — emitirlos todos
            # (no un único pulso 1 inventado) para que pulso_inicial/
            # pulso_final en compases_desenrollados reflejen el compás
            # COMPLETO en vez de mostrarlo como si cortara a mitad de camino
            # (bug real: sin esto, un compás con indicación cargada pero
            # tempo todavía sin cargar en Notación se veía en
            # sincronizar_compases.html como "hasta pulso 1" aunque fuera un
            # compás entero de 4/4).
            if pulsos_compas:
                pulso_ini, pulso_fin = _rango_pulsos_del_compas(seg, compas, pulsos_compas)
            else:
                pulso_ini = pulso_fin = 1
            for p in range(pulso_ini, pulso_fin + 1):
                pulsos.append({
                    'segmento_id': seg.id,
                    'compas': compas,
                    'pasada': pasadas_por_compas.get((seg.id, compas)),
                    'pulso': p,
                    'pulsos_por_compas': int(pulsos_compas) if pulsos_compas else None,
                    'es_primer_pulso_compas': p == pulso_ini,
                    'acento': p == 1,
                    'indicacion_compas': info_c.get('indicacion_compas'),
                    'armadura': info_c.get('armadura'),
                    'bpm': bpm_inicio,
                    'variacion_tempo_display': '',
                    'bpm_llegada': None,
                    'descripcion': seg.descripcion,
                    'duracion': None,
                    'duracion_compases': None,
                })
        else:
            if seg.id not in pulsos_por_compas_por_fila:
                pulsos_por_compas_por_fila[seg.id] = _pulsos_por_compas_de_fila(seg, notacion_por_compas)
            pulsos_por_compas_fila = pulsos_por_compas_por_fila[seg.id]

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
            offset_compas = _pulsos_antes_del_compas(seg, compas, pulsos_por_compas_fila)

            posicion_compas = posicion_inicio_por_seg_compas.get((seg.id, compas))

            for p in range(pulso_ini_emitir, pulso_fin_emitir + 1):
                idx_en_fila = offset_compas + (p - pulso_ini)
                bpm_pulso = bpm_por_pulso.get((seg.id, compas, p), bpm_inicio)
                factor_pulso = factor_por_pulso.get((seg.id, compas, p), 1.0)
                duracion_pulso = 60.0 / bpm_pulso * factor_pulso

                # Posición calculada GLOBAL de este pulso puntual (cruza
                # filas del itinerario, ver _anclas_globales) — sirve de eje
                # para ubicarlo contra las anclas reales.
                posicion_pulso = None
                if posicion_compas is not None:
                    posicion_pulso = posicion_compas + (posiciones_fila[idx_en_fila] - posiciones_fila[offset_compas])

                # "compases": duración real = diferencia entre el tiempo real
                # interpolado en la posición calculada de ESTE pulso y la del
                # siguiente. None si algún extremo cae fuera de lo cubierto
                # por marcas reales.
                duracion_compases_pulso = None
                if posicion_pulso is not None:
                    t_ini = _tiempo_real_en_posicion(anclas_compases_globales, posicion_pulso)
                    t_fin = _tiempo_real_en_posicion(anclas_compases_globales, posicion_pulso + duracion_pulso)
                    if t_ini is not None and t_fin is not None:
                        duracion_compases_pulso = t_fin - t_ini

                # Primer pulso EMITIDO de TODO el plan (primera_iteracion,
                # no sólo de este compás): si desde_pulso venía con fracción
                # (ej. "5,2.5"), a este pulso sólo le queda por correr la
                # parte final — se le resta la fracción ya "consumida" a su
                # duración en las dos fuentes, para que el reloj visual
                # quede de acuerdo con dónde se posiciona el audio (ver
                # tiempo_real_ancla). No aplica a los pulsos siguientes.
                if primera_iteracion and p == pulso_ini_emitir and fraccion_inicial > 0:
                    resto = 1.0 - fraccion_inicial
                    duracion_pulso *= resto
                    if duracion_compases_pulso is not None:
                        duracion_compases_pulso *= resto

                pulsos.append({
                    'segmento_id': seg.id,
                    'compas': compas,
                    'pasada': pasadas_por_compas.get((seg.id, compas)),
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
                    'indicacion_compas': info_c.get('indicacion_compas'),
                    'armadura': info_c.get('armadura'),
                    'bpm': round(bpm_pulso),
                    'variacion_tempo_display': variacion_por_pulso.get((seg.id, compas, p), ('', None))[0],
                    'bpm_llegada': variacion_por_pulso.get((seg.id, compas, p), ('', None))[1],
                    'descripcion': seg.descripcion,
                    'duracion': duracion_pulso,
                    # Duración real: la calculada, escalada SÓLO contra
                    # MarcaTiempoCompas puntuales — None fuera del tramo
                    # cubierto por al menos dos marcas reales (ver
                    # _tiempo_real_en_posicion).
                    'duracion_compases': duracion_compases_pulso,
                })

        primera_iteracion = False
        if es_ultima_iteracion:
            break
        pos = avanzar_compas(obra, seg, compas)

    return pulsos, completo, cambios, rampas, saltos


def compases_desenrollados(obra):
    """Una entrada por CADA ocurrencia de compás de la obra completa (a
    diferencia de segmentos_navegables, que trabaja a nivel de fila del
    itinerario) — cada repetición (2da vez, D.C., etc.) es su propia
    entrada, con su propia 'pasada'. Un compás partido en dos filas por
    continuidad (salto que cae a mitad de compás — misma pasada fusionada,
    ver _pasadas_por_compas) es UNA sola entrada, no dos: el segundo
    fragmento no genera una entrada nueva, aunque dispare
    es_primer_pulso_compas de nuevo en construir_plan. Pensada para
    sincronizar_compases (tap compás a compás, sincronización fina — ver
    MarcaTiempoCompas).

    Devuelve (entradas, completo): entradas es una lista de dicts, uno por
    ocurrencia, con segmento_id/compas/pasada/indicacion_compas/bpm/
    pulsos_por_compas (numérico, "4/4" -> 4.0 — ver _pulsos_por_compas)/
    pulso_inicial (primer pulso REALMENTE tocado de esta ocurrencia — 1
    salvo que la fila arranque a mitad de compás, ver Segmento.pulso_desde)/
    pulso_final (último pulso tocado antes de pasar al compás siguiente o
    cortar el rango — si un salto deja esta ocurrencia sin llegar al final
    del compás, ver Segmento.pulso_hasta, queda menor que pulsos_por_compas;
    en un compás partido en dos filas por continuidad, es el último pulso
    del SEGUNDO fragmento, no del primero — ver más abajo)/
    tiempo_inicio_calculado (acumulado desde el primer compás de la obra,
    en segundos — None de ahí en más si en algún punto faltó bpm o
    indicación)/tiempo_inicio (real, si ya está marcado)/explicita (None si
    no hay tiempo_inicio en absoluto; True/False según MarcaTiempoCompas.explicita
    si lo hay — ver ese modelo)/es_cierre; completo es False si algún compás
    quedó sin bpm/indicación resueltos (mismo criterio que construir_plan).

    Puede haber MÁS de una entrada de cierre (es_cierre en True) — no sólo
    la última: una PAUSA entre movimientos también vive como una fila de
    cierre, en su posición cronológica real, pero con su propio umbral en
    `compas` en vez de None — ver umbral_pausa/es_cierre más abajo (compas
    None es específicamente la fila de cierre FINAL, fin de la obra; un
    número ahí es una pausa, el mismo umbral que su EfectoTempo tipo
    'pausa'). Cada una guarda su tiempo_inicio (Segmento.tiempo_inicio, no
    una MarcaTiempoCompas) con marcar_tiempo_segmento, no
    marcar_tiempo_compas."""
    navegables = segmentos_navegables(obra)
    if not navegables:
        return [], True

    pulsos, completo, _cambios, _rampas, _saltos = construir_plan(obra, navegables[0].compas_desde, 1, None, None)
    pasadas = _pasadas_por_compas(obra)
    marcas = {(m.compas, m.pasada): m for m in obra.marcas_tiempo_compas.all()}
    valor_por_umbral = {p['compas_desde']: p['valor_segundos'] for p in indice_pausas(obra)}
    todos_los_segmentos = list(obra.segmentos.order_by('orden'))

    entradas = []
    entradas_por_pasada = {}  # (compas, pasada) -> dict ya agregado a `entradas`
    acumulado = 0.0
    idx_pulso = 0
    for seg in todos_los_segmentos:
        if seg.compas_hasta is None:
            entradas.append({
                'segmento_id': seg.id,
                'compas': None,
                'pasada': None,
                'indicacion_compas': None,
                'armadura': None,
                'bpm': None,
                'pulsos_por_compas': None,
                'pulso_inicial': None,
                'pulso_final': None,
                'tiempo_inicio_calculado': acumulado,
                'tiempo_inicio': seg.tiempo_inicio,
                'explicita': True if seg.tiempo_inicio is not None else None,
                'es_cierre': True,
                'umbral_pausa': seg.compas_desde,
            })
            if acumulado is not None and seg.compas_desde is not None:
                valor = valor_por_umbral.get(seg.compas_desde)
                if valor is not None:
                    acumulado += valor
            continue

        while idx_pulso < len(pulsos) and pulsos[idx_pulso]['segmento_id'] == seg.id:
            p = pulsos[idx_pulso]
            idx_pulso += 1
            pasada = pasadas.get((p['segmento_id'], p['compas']), 1)
            clave = (p['compas'], pasada)
            if p.get('es_primer_pulso_compas') and clave not in entradas_por_pasada:
                marca = marcas.get(clave)
                entrada = {
                    'segmento_id': p['segmento_id'],
                    'compas': p['compas'],
                    'pasada': pasada,
                    'indicacion_compas': p['indicacion_compas'],
                    'armadura': p['armadura'],
                    'bpm': p['bpm'],
                    'pulsos_por_compas': _pulsos_por_compas(p['indicacion_compas']),
                    'pulso_inicial': p.get('pulso', 1),
                    'pulso_final': p.get('pulso', 1),
                    'tiempo_inicio_calculado': acumulado,
                    'tiempo_inicio': marca.tiempo_inicio if marca else None,
                    'explicita': marca.explicita if marca else None,
                    'es_cierre': False,
                    'umbral_pausa': None,
                }
                entradas.append(entrada)
                entradas_por_pasada[clave] = entrada
            if clave in entradas_por_pasada:
                entradas_por_pasada[clave]['pulso_final'] = p.get('pulso', entradas_por_pasada[clave]['pulso_final'])
            if acumulado is not None:
                acumulado = (acumulado + p['duracion']) if p['duracion'] is not None else None

    return entradas, completo


def tiempo_real_ancla(obra, segmento_id, compas, pulso, pulso_fraccion=0.0):
    """Tiempo real (por compases, ver MarcaTiempoCompas/MarcaTiempoPulso) en
    el punto que arranca el pulso pedido de esta ocurrencia de compás
    puntual — pensado para posicionar el audio en navegador_obra.html (ver
    plan_obra). pulso_fraccion (0.0-1.0) ubica un punto intermedio DENTRO de
    ese pulso (p.ej. "5,2.5" = compás 5, pulso 2, pulso_fraccion=0.5); 0.0
    (default) es el arranque exacto del pulso. None si no hay ningún ancla
    real que cubra ese punto — no inventa un valor con el tiempo calculado.
    El cliente interpreta None como "no hay cómo posicionar el audio acá" y
    avisa en vez de arrancarlo desde 0 (ver navegador_obra.html/
    audioDebeSonar)."""
    segmento = Segmento.objects.filter(pk=segmento_id).first()
    if segmento is None:
        return None
    indice = _indice_notacion(obra)
    pasadas_por_compas = _pasadas_por_compas(obra)
    todos_los_segmentos = list(obra.segmentos.order_by('orden'))
    _, notacion_por_compas = _resolver_notacion_por_compas(todos_los_segmentos, indice, pasadas_por_compas)
    info_c = notacion_por_compas.get((segmento.id, compas), {})
    bpm_inicio = info_c.get('bpm')
    pulsos_compas = info_c.get('pulsos_compas')
    if not (bpm_inicio and pulsos_compas):
        return None
    pulso_ini_compas, _ = _rango_pulsos_del_compas(segmento, compas, pulsos_compas)

    navegables = segmentos_navegables(obra)
    marcas_por_compas_pasada = {
        (m.compas, m.pasada): m.tiempo_inicio for m in obra.marcas_tiempo_compas.all()
    }
    marcas_pulso_por_compas_pasada = _indice_marcas_pulso(obra)
    pausas = indice_pausas(obra)
    rampas = _resolver_rampas(obra, notacion_por_compas)
    calderones = _indice_calderones(obra)
    bpm_por_pulso, factor_por_pulso, _ = _resolver_bpm_por_pulso(
        obra, navegables, notacion_por_compas, pasadas_por_compas, indice, rampas, calderones,
    )
    anclas, posicion_inicio = _anclas_globales(
        todos_los_segmentos, notacion_por_compas, pasadas_por_compas, marcas_por_compas_pasada,
        bpm_por_pulso, factor_por_pulso, marcas_pulso_por_compas_pasada, pausas,
    )

    posicion_compas = posicion_inicio.get((segmento.id, compas))
    if posicion_compas is None:
        return None
    posiciones_fila = _posiciones_calculadas_fila(segmento, notacion_por_compas, bpm_por_pulso, factor_por_pulso)
    if posiciones_fila is None:
        return None
    pulsos_por_compas_fila = _pulsos_por_compas_de_fila(segmento, notacion_por_compas)
    offset_compas_local = _pulsos_antes_del_compas(segmento, compas, pulsos_por_compas_fila)
    idx_en_fila = offset_compas_local + (pulso - pulso_ini_compas)
    if idx_en_fila < 0 or idx_en_fila + 1 >= len(posiciones_fila):
        return None
    posicion = posicion_compas + (posiciones_fila[idx_en_fila] - posiciones_fila[offset_compas_local])
    t_ini = _tiempo_real_en_posicion(anclas, posicion)
    if t_ini is None or pulso_fraccion <= 0:
        return t_ini
    duracion_pulso_calc = posiciones_fila[idx_en_fila + 1] - posiciones_fila[idx_en_fila]
    t_fin = _tiempo_real_en_posicion(anclas, posicion + duracion_pulso_calc)
    return t_ini if t_fin is None else t_ini + pulso_fraccion * (t_fin - t_ini)


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
    # bulk_update no dispara post_save (a diferencia de save()/update_or_create/
    # delete(), que si lo hacen) — sin esto, correr todas las marcas no
    # actualizaba Obra.actualizado (ver signals.py).
    if marcas:
        Obra.objects.filter(pk=obra.pk).update(actualizado=timezone.now())
    return len(marcas)


def interpolar_marcas_compas(obra, objetivos):
    """objetivos: iterable de (compas, pasada) sin marca explícita, a rellenar
    por interpolación (sincronizar_compases.html). Busca, para cada uno, el
    compás explícito más cercano hacia atrás y hacia adelante en TODA la
    obra (no sólo en objetivos) — incluye la fila de cierre
    (Segmento.tiempo_inicio) como ancla válida del extremo derecho, ya que
    es una entrada más de compases_desenrollados. Si a alguno le falta un
    extremo, se lo salta (no se interpola) sin abortar el resto.

    Reparte el tiempo entre las dos anclas siguiendo la curva de bpm
    calculada del itinerario (tiempo_inicio_calculado de
    compases_desenrollados, que ya acumula por bpm/bpm_llegada cruzando
    filas del itinerario) en vez de una línea recta por cantidad de pulsos —
    así un acelerando/ritardando notado en el itinerario se refleja en el
    reparto, aunque el tramo entre esas dos marcas no esté marcado compás a
    compás. Guarda cada resultado como MarcaTiempoCompas(explicita=False),
    sobrescribiendo si ya había un valor no-explícito ahí (permite
    recalcular).

    Devuelve (resueltos, no_resueltos, invertidos, sin_calcular): resueltos
    es la cantidad interpolada con éxito; no_resueltos es la lista de
    (compas, pasada) a las que les faltó una ancla explícita de algún lado
    (hay posición calculada, pero ningún compás marcado a mano antes y/o
    después en toda la obra); invertidos es la lista de (compas, pasada)
    cuyas DOS anclas SÍ existen pero están en el orden real equivocado (la
    de adelante en la partitura tiene un tiempo real tapeado antes que la
    de atrás) — un tapeo contradictorio, no un hueco de datos: no se
    interpola ahí en vez de guardar un tiempo que iría hacia atrás (ver
    _bracket_anclas_invertido); sin_calcular es la lista de (compas, pasada)
    cuya posición ni siquiera se pudo calcular — falta bpm o indicación de
    compás en el itinerario en esa fila o antes (ver resolver_segmentos),
    así que no es un problema de anclas sino del itinerario/notación."""
    entradas, _ = compases_desenrollados(obra)
    idx_por_clave = {
        (e['compas'], e['pasada']): i for i, e in enumerate(entradas) if not e['es_cierre']
    }

    anclas = sorted(
        (e['tiempo_inicio_calculado'], e['tiempo_inicio'].total_seconds())
        for e in entradas
        if e['explicita'] and e['tiempo_inicio'] is not None and e['tiempo_inicio_calculado'] is not None
    )

    no_resueltos = []
    invertidos = []
    sin_calcular = []
    a_guardar = []
    for clave in objetivos:
        i = idx_por_clave.get(clave)
        posicion = entradas[i]['tiempo_inicio_calculado'] if i is not None else None
        if posicion is None:
            sin_calcular.append(clave)
            continue
        if _bracket_anclas_invertido(anclas, posicion):
            invertidos.append(clave)
            continue
        tiempo = _tiempo_real_en_posicion(anclas, posicion)
        if tiempo is None:
            no_resueltos.append(clave)
        else:
            a_guardar.append((clave, tiempo))

    for (compas, pasada), tiempo in a_guardar:
        MarcaTiempoCompas.objects.update_or_create(
            obra=obra, compas=compas, pasada=pasada,
            defaults={'tiempo_inicio': timedelta(seconds=tiempo), 'explicita': False},
        )

    return len(a_guardar), no_resueltos, invertidos, sin_calcular


def borrar_marcas_compas(obra, objetivos):
    """Borra (explícitas Y no-explícitas, sin distinción) las MarcaTiempoCompas
    de obra que coincidan con (compas, pasada) en objetivos — borrado en
    lote de una selección múltiple en sincronizar_compases.html. No toca la
    fila de cierre (Segmento.tiempo_inicio, mecanismo aparte) — mismo
    criterio que desplazar_marcas_compas, que tampoco la soporta. Devuelve
    cuántas se borraron."""
    objetivos = set(objetivos)
    if not objetivos:
        return 0
    ids = [m.id for m in obra.marcas_tiempo_compas.all() if (m.compas, m.pasada) in objetivos]
    MarcaTiempoCompas.objects.filter(id__in=ids).delete()
    return len(ids)


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
