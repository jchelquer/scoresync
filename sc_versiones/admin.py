from django.contrib import admin
from .models import PerfilVersiones, Comentario


@admin.register(PerfilVersiones)
class PerfilVersionesAdmin(admin.ModelAdmin):
    list_display = ('usuario', 'ultima_version_vista', 'ultimo_acceso')
    list_filter = ('ultima_version_vista',)
    search_fields = ('usuario__username', 'usuario__email')
    readonly_fields = ('ultimo_acceso',)


@admin.register(Comentario)
class ComentarioAdmin(admin.ModelAdmin):
    list_display = ('fecha', 'usuario', 'version', 'tipo', 'texto_corto')
    list_filter = ('version', 'tipo')
    search_fields = ('usuario__username', 'texto')
    readonly_fields = ('fecha', 'metadata', 'version', 'usuario')

    def texto_corto(self, obj):
        return obj.texto[:80] + '…' if len(obj.texto) > 80 else obj.texto
    texto_corto.short_description = 'Comentario'
