from django.db import migrations


def backfill(apps, schema_editor):
    Segmento = apps.get_model('partituras', 'Segmento')
    EfectoTempo = apps.get_model('partituras', 'EfectoTempo')

    for seg in Segmento.objects.exclude(bpm_llegada__isnull=True):
        EfectoTempo.objects.create(
            obra=seg.obra,
            tipo=seg.variacion_tempo or 'accelerando',
            desde_texto=seg.desde_texto,
            compas_desde=seg.compas_desde,
            pulso_desde=seg.pulso_desde,
            hasta_texto=seg.hasta_texto,
            compas_hasta=seg.compas_hasta,
            pulso_hasta=seg.pulso_hasta,
            valor=str(seg.bpm_llegada),
        )


def eliminar_backfill(apps, schema_editor):
    EfectoTempo = apps.get_model('partituras', 'EfectoTempo')
    EfectoTempo.objects.filter(tipo__in=('accelerando', 'ritardando')).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('partituras', '0034_efectotempo'),
    ]

    operations = [
        migrations.RunPython(backfill, eliminar_backfill),
    ]
