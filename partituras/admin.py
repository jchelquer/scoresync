from django.contrib import admin
from .models import Barra, Compas, Obra, Pagina, Segmento, Sistema, Partitura


@admin.register(Obra)
class ObraAdmin(admin.ModelAdmin):
    list_display = ('titulo', 'compositor', 'arreglista', 'owner', 'creado')
    search_fields = ('titulo', 'compositor', 'arreglista', 'owner__username')


@admin.register(Segmento)
class SegmentoAdmin(admin.ModelAdmin):
    list_display = ('obra', 'orden', 'compas_desde', 'compas_hasta', 'indicacion_compas', 'bpm', 'bpm_llegada', 'descripcion')
    list_filter = ('variacion_tempo',)
    search_fields = ('obra__titulo', 'descripcion')


@admin.register(Barra)
class BarraAdmin(admin.ModelAdmin):
    list_display = ('sistema', 'x', 'estado', 'origen')
    list_filter = ('estado', 'origen')


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
