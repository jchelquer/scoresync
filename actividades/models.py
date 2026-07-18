from django.db import models


class Instrumento(models.Model):
    """
    Espejo recortado de actividades_instrumento, tabla real y gestionada por
    el proyecto ensayos (misma BD Postgres compartida).
    managed = False: Django lee la tabla existente sin crearla ni modificarla.
    Solo se incluyen los campos que scoresync necesita para mostrar un
    desplegable de instrumentos — no los campos de rango MIDI/transposición,
    que pertenecen al dominio de afinación/ensayos.
    """

    nombre = models.CharField(max_length=100)
    padre = models.ForeignKey(
        'self', null=True, blank=True,
        on_delete=models.SET_NULL,
        db_constraint=False,
        related_name='hijos',
    )

    class Meta:
        managed = False
        db_table = 'actividades_instrumento'
        verbose_name = 'Instrumento'
        verbose_name_plural = 'Instrumentos'
        ordering = ['nombre']

    def __str__(self):
        return self.nombre
