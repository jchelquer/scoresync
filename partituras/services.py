"""Lógica de dominio que no depende de HTTP — guardado de compases a partir
de lo que el cliente ya calculó (geometría + numeración), y el reajuste de
numeración entre páginas."""

from django.db.models import F

from .models import Compas


def numero_inicial_pagina(pagina):
    """Número que le correspondería al primer compás de esta página si no
    hay ninguno propio todavía — continúa desde el último compás de la
    página anterior (en toda la partitura), o arranca en 1 si no hay nada
    previo. Se le pasa al cliente para que sepa desde dónde numerar si
    arranca a construir compases de cero."""
    anterior = Compas.objects.filter(
        sistema__pagina__partitura=pagina.partitura,
        sistema__pagina__numero__lt=pagina.numero,
    ).order_by('-sistema__pagina__numero', '-sistema__orden', '-x').first()
    return (anterior.numero + 1) if anterior else 1


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

    desfasaje = (ultimo.numero + 1) - primero_siguiente.numero
    if desfasaje != 0:
        siguientes.update(numero=F('numero') + desfasaje)
