from django.contrib import admin
from .models import Partitura, Pagina, Sistema, Compas


@admin.register(Partitura)
class PartituraAdmin(admin.ModelAdmin):
    list_display = ('titulo', 'compositor', 'instrumento', 'parte', 'owner', 'estado_normalizacion', 'estado_analisis', 'creado')
    list_filter = ('estado_normalizacion', 'estado_analisis', 'instrumento')
    search_fields = ('titulo', 'compositor', 'owner__username')


@admin.register(Pagina)
class PaginaAdmin(admin.ModelAdmin):
    list_display = ('partitura', 'numero', 'rotacion_detectada', 'angulo_deskew_detectado', 'confirmada')
    list_filter = ('confirmada',)


@admin.register(Sistema)
class SistemaAdmin(admin.ModelAdmin):
    list_display = ('pagina', 'orden', 'origen')


@admin.register(Compas)
class CompasAdmin(admin.ModelAdmin):
    list_display = ('sistema', 'numero', 'x', 'y', 'width', 'height', 'origen', 'confirmado')
    list_filter = ('origen', 'confirmado')
