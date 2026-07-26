"""Limpieza de archivos en storage al borrar filas, y actualización en
cascada de Obra.actualizado — todo a nivel de MODELO (no de vista) para que
corra pase lo que pase borre/edite por donde sea (admin, shell, cascada de
FK, o las vistas propias), a diferencia de tenerlo duplicado en cada vista."""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.utils import timezone

from .models import MarcaNotacion, MarcaTiempoCompas, Obra, Partitura, Segmento


@receiver(post_delete, sender=Partitura)
def borrar_archivos_partitura(sender, instance, **kwargs):
    if instance.archivo_original:
        instance.archivo_original.delete(save=False)


@receiver(post_delete, sender=Obra)
def borrar_audio_obra(sender, instance, **kwargs):
    if instance.audio:
        instance.audio.delete(save=False)


def _tocar_obra(obra_id):
    """Actualiza Obra.actualizado sin pasar por Model.save() (evita disparar
    cualquier otra lógica de guardado de Obra) — ver Obra.actualizado."""
    Obra.objects.filter(pk=obra_id).update(actualizado=timezone.now())


# Segmento (itinerario) y MarcaTiempoCompas (marcas de tiempo por compás) son
# los dos modelos que este proyecto considera "cambios reales de la obra"
# para Obra.actualizado (a diferencia de PreferenciaObra/PreferenciaParte,
# que son por-usuario). Cubre alta/edición/borrado individual (.save()/
# .delete() disparan estas señales) — la única excepción conocida es
# bulk_update, que Django no dispara por diseño (ver
# services.desplazar_marcas_compas, que toca Obra.actualizado a mano).
@receiver([post_save, post_delete], sender=Segmento)
def obra_actualizada_por_segmento(sender, instance, **kwargs):
    _tocar_obra(instance.obra_id)


@receiver([post_save, post_delete], sender=MarcaTiempoCompas)
def obra_actualizada_por_marca_compas(sender, instance, **kwargs):
    _tocar_obra(instance.obra_id)


@receiver([post_save, post_delete], sender=MarcaNotacion)
def obra_actualizada_por_notacion(sender, instance, **kwargs):
    _tocar_obra(instance.obra_id)
