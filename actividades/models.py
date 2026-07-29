from django.db import models


class Instrumento(models.Model):
    """
    Espejo recortado de actividades_instrumento, tabla real y gestionada por
    el proyecto ensayos (misma BD Postgres compartida).
    managed = False: Django lee la tabla existente sin crearla ni modificarla.
    Sólo se incluyen los campos que scoresync necesita — no los de rango
    MIDI, que pertenecen al dominio de afinación/ensayos. transposicion_semitonos
    sí se trae (2026-07-28): la usa MarcaNotacion tipo='armadura' para calcular
    la armadura ESCRITA de cada parte a partir de la de concierto (ver
    services.armadura_transportada) — misma convención que ensayos/afinación:
    concierto = escrito + transposicion_semitonos.
    """

    nombre = models.CharField(max_length=100)
    padre = models.ForeignKey(
        'self', null=True, blank=True,
        on_delete=models.SET_NULL,
        db_constraint=False,
        related_name='hijos',
    )
    transposicion_semitonos = models.SmallIntegerField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = 'actividades_instrumento'
        verbose_name = 'Instrumento'
        verbose_name_plural = 'Instrumentos'
        ordering = ['nombre']

    def __str__(self):
        return self.nombre
