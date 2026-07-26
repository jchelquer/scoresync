from django.db import migrations


def backfill(apps, schema_editor):
    Obra = apps.get_model('partituras', 'Obra')
    MarcaNotacion = apps.get_model('partituras', 'MarcaNotacion')

    for obra in Obra.objects.all():
        segmentos = list(obra.segmentos.order_by('orden'))
        vistos = {}  # (tipo, compas) -> valor ya creado, para avisar de conflictos
        for seg in segmentos:
            if seg.compas_desde is None:
                continue  # fila de cierre, no tiene indicacion_compas/bpm propios
            for tipo, valor in (('compas', seg.indicacion_compas), ('tempo', seg.bpm)):
                if not valor:
                    continue
                valor_texto = str(valor)
                clave = (tipo, seg.compas_desde)
                if clave in vistos:
                    if vistos[clave] != valor_texto:
                        print(
                            f"[backfill MarcaNotacion] AVISO: obra {obra.pk} ({obra.titulo!r}) "
                            f"tipo={tipo} compas={seg.compas_desde} — valor {vistos[clave]!r} ya migrado "
                            f"(otra fila), se ignora el valor distinto {valor_texto!r} de segmento id={seg.pk}. "
                            f"Si era intencional, agregalo a mano como override puntual (compas, pasada) "
                            f"en la pantalla de Notación."
                        )
                    continue
                MarcaNotacion.objects.get_or_create(
                    obra=obra, tipo=tipo, compas=seg.compas_desde, pasada=None,
                    defaults={'valor': valor_texto},
                )
                vistos[clave] = valor_texto


def eliminar_backfill(apps, schema_editor):
    MarcaNotacion = apps.get_model('partituras', 'MarcaNotacion')
    MarcaNotacion.objects.filter(tipo__in=('compas', 'tempo'), pasada__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('partituras', '0027_marcanotacion'),
    ]

    operations = [
        migrations.RunPython(backfill, eliminar_backfill),
    ]
