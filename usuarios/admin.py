from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import SolicitudAcceso, Usuario, GrupoUsuario


@admin.register(GrupoUsuario)
class GrupoUsuarioAdmin(admin.ModelAdmin):
    """Sólo lectura: los grupos se crean y se asignan a usuarios desde
    ensayos, no desde acá (ver GrupoUsuario en usuarios/models.py)."""
    list_display = ('nombre', 'descripcion')
    search_fields = ('nombre',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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
    list_display = ("username", "first_name", "last_name", "email", "rol", "get_grupos", "is_active")
    list_filter = ("rol", "is_active", "grupos")
    fieldsets = UserAdmin.fieldsets + (
        ("ScoreSync", {"fields": ("rol", "get_grupos")}),
    )
    readonly_fields = UserAdmin.readonly_fields + ("get_grupos",)

    @admin.display(description="Grupos")
    def get_grupos(self, obj):
        return ", ".join(obj.grupos.values_list("nombre", flat=True))
