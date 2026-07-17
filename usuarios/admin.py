from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import SolicitudAcceso, Usuario


@admin.register(SolicitudAcceso)
class SolicitudAccesoAdmin(admin.ModelAdmin):
    list_display = ('apellido', 'nombre', 'email', 'instrumento', 'programa', 'estado', 'fecha_solicitud')
    list_filter = ('estado', 'programa')
    search_fields = ('nombre', 'apellido', 'email')
    list_editable = ('estado',)
    readonly_fields = ('fecha_solicitud',)
    fieldsets = (
        (None, {'fields': ('nombre', 'apellido', 'email', 'celular', 'instrumento', 'mensaje', 'programa')}),
        ('Gestión', {'fields': ('estado', 'notas_admin', 'fecha_solicitud')}),
    )
    actions = ['desestimar_solicitudes']

    @admin.action(description='Desestimar seleccionadas')
    def desestimar_solicitudes(self, request, queryset):
        actualizadas = queryset.filter(estado=SolicitudAcceso.PENDIENTE).update(estado=SolicitudAcceso.RECHAZADA)
        self.message_user(request, f"{actualizadas} solicitud(es) desestimada(s).")


@admin.register(Usuario)
class UsuarioAdmin(UserAdmin):
    list_display = ("username", "first_name", "last_name", "email", "rol", "is_active")
    list_filter = ("rol", "is_active")
    fieldsets = UserAdmin.fieldsets + (
        ("ScoreSync", {"fields": ("rol",)}),
    )
