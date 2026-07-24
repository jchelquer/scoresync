"""Limpieza de archivos en storage al borrar filas — a nivel de MODELO (no
de vista) para que corra pase lo que pase borre por donde borre (admin,
shell, cascada de FK, o las vistas propias), a diferencia de tenerlo
duplicado en cada vista de borrado."""

from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import Obra, Partitura


@receiver(post_delete, sender=Partitura)
def borrar_archivos_partitura(sender, instance, **kwargs):
    if instance.archivo_original:
        instance.archivo_original.delete(save=False)


@receiver(post_delete, sender=Obra)
def borrar_audio_obra(sender, instance, **kwargs):
    if instance.audio:
        instance.audio.delete(save=False)
