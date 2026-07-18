from django.contrib import admin
from .models import Instrumento


@admin.register(Instrumento)
class InstrumentoAdmin(admin.ModelAdmin):
    """Solo lectura: la tabla es propiedad del proyecto ensayos."""
    list_display = ('nombre', 'padre')
    search_fields = ('nombre',)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False
