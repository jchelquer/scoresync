from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils.translation import gettext_lazy as _


class Usuario(AbstractUser):
    """
    Espejo de la tabla usuarios_usuario del proyecto ensayos (misma BD Postgres
    compartida entre afinacion, ensayos, tempo y scoresync).
    managed = False: Django lee la tabla existente sin crearla ni modificarla.
    No se incluye instrumento_principal ni grupo (FKs a apps externas).
    """

    ROL_PROFESOR = 'profesor'
    ROL_ALUMNO = 'alumno'
    ROL_ADMIN = 'admin'

    ROLES = [
        (ROL_PROFESOR, 'Profesor'),
        (ROL_ALUMNO, 'Alumno'),
        (ROL_ADMIN, 'Administrador'),
    ]

    rol = models.CharField(max_length=10, choices=ROLES, default=ROL_ALUMNO)
    suscripcion = models.CharField(max_length=10, null=True, blank=True)
    celular = models.CharField(max_length=30, blank=True)
    telefono = models.CharField(max_length=30, blank=True)
    direccion = models.CharField(max_length=255, blank=True)
    localidad = models.CharField(max_length=100, blank=True)
    genero = models.CharField(max_length=1, blank=True)
    fecha_nacimiento = models.DateField(null=True, blank=True)

    IDIOMA_ES = 'es-ar'
    IDIOMA_EN = 'en'
    IDIOMAS = [
        (IDIOMA_ES, 'Español (Argentina)'),
        (IDIOMA_EN, 'English'),
    ]

    idioma = models.CharField(max_length=10, choices=IDIOMAS, default=IDIOMA_ES)

    class Meta:
        managed = False
        db_table = 'usuarios_usuario'
        verbose_name = 'Usuario'
        verbose_name_plural = 'Usuarios'

    @property
    def es_admin(self):
        return self.rol == self.ROL_ADMIN

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_rol_display()})"


class SolicitudAcceso(models.Model):
    """Espejo de usuarios_solicitudacceso, tabla gestionada por el proyecto ensayos."""

    PENDIENTE = 'pendiente'
    APROBADA = 'aprobada'
    RECHAZADA = 'rechazada'
    ESTADOS = [
        (PENDIENTE, _('Pendiente')),
        (APROBADA, _('Aprobada')),
        (RECHAZADA, _('Rechazada')),
    ]

    nombre = models.CharField(max_length=100, verbose_name=_('Nombre'))
    apellido = models.CharField(max_length=100, verbose_name=_('Apellido'))
    email = models.EmailField(verbose_name=_('Email'))
    celular = models.CharField(max_length=30, blank=True, verbose_name=_('Celular'))
    instrumento = models.CharField(max_length=100, blank=True, verbose_name=_('Instrumento principal'))
    mensaje = models.TextField(blank=True, verbose_name=_('Mensaje'))
    programa = models.CharField(max_length=50, default='scoresync')
    estado = models.CharField(max_length=10, choices=ESTADOS, default=PENDIENTE, verbose_name=_('Estado'))
    fecha_solicitud = models.DateTimeField(auto_now_add=True, verbose_name=_('Fecha de solicitud'))
    notas_admin = models.TextField(blank=True, verbose_name=_('Notas internas'))

    class Meta:
        managed = False
        db_table = 'usuarios_solicitudacceso'
        ordering = ['-fecha_solicitud']
        verbose_name = _('Solicitud de acceso')
        verbose_name_plural = _('Solicitudes de acceso')

    def __str__(self):
        return f"{self.apellido}, {self.nombre} ({self.get_estado_display()})"
