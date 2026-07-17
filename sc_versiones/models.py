from django.db import models
from django.conf import settings


class PerfilVersiones(models.Model):
    usuario = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='perfil_versiones',
    )
    ultima_version_vista = models.CharField(max_length=20, blank=True, default='')
    ultimo_acceso = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'sc_versiones_perfil'
        verbose_name = 'Perfil de versiones'
        verbose_name_plural = 'Perfiles de versiones'

    def __str__(self):
        return f"{self.usuario} — v{self.ultima_version_vista}"


TIPOS_COMENTARIO = [
    ('error', 'Error'),
    ('idea', 'Idea'),
    ('duda', 'Duda'),
    ('comentario', 'Comentario'),
]


class Comentario(models.Model):
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='comentarios_versiones',
    )
    version = models.CharField(max_length=20)
    tipo = models.CharField(max_length=20, choices=TIPOS_COMENTARIO, blank=True)
    texto = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)
    fecha = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'sc_versiones_comentario'
        ordering = ['-fecha']
        verbose_name = 'Comentario'
        verbose_name_plural = 'Comentarios'

    def __str__(self):
        tipo_str = self.get_tipo_display() or 'Sin tipo'
        return f"{self.usuario} — v{self.version} — {tipo_str}"
