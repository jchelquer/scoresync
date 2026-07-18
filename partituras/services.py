"""Lógica de dominio que no depende de HTTP — guardado de compases a partir
de lo que el cliente ya calculó (geometría + numeración), el reajuste de
numeración entre páginas, y la invalidación en cascada cuando se rehace una
etapa anterior del pipeline (orientación → márgenes → sistemas → ancla →
barras/compases)."""

from django.db.models import F

from .models import Barra, Compas


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
