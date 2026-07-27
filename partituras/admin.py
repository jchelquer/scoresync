from django.contrib import admin
from .models import (
    Barra, Ciclo, Compas, MarcaNotacion, MarcaTiempoCompas, Obra, Pagina, PreferenciaObra,
    PreferenciaParte, Repertorio, Segmento, Sistema, Partitura,
)


@admin.register(Repertorio)
class RepertorioAdmin(admin.ModelAdmin):
    list_display = ('nombre',)
    search_fields = ('nombre',)


@admin.register(Ciclo)
class CicloAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'repertorio')
    list_filter = ('repertorio',)
    search_fields = ('nombre', 'repertorio__nombre')


@admin.register(Obra)
class ObraAdmin(admin.ModelAdmin):
    list_display = ('titulo', 'compositor', 'arreglista', 'ciclo', 'owner', 'publicada', 'creado')
    list_filter = ('publicada', 'ciclo__repertorio', 'ciclo')
    search_fields = ('titulo', 'compositor', 'arreglista', 'owner__username')


@admin.register(Segmento)
class SegmentoAdmin(admin.ModelAdmin):
    list_display = ('obra', 'orden', 'compas_desde', 'compas_hasta', 'bpm_llegada', 'descripcion')
    list_filter = ('variacion_tempo',)
    search_fields = ('obra__titulo', 'descripcion')


@admin.register(MarcaNotacion)
class MarcaNotacionAdmin(admin.ModelAdmin):
    list_display = ('obra', 'tipo', 'compas', 'pasada', 'valor')
    list_filter = ('tipo',)
    search_fields = ('obra__titulo',)


@admin.register(Barra)
class BarraAdmin(admin.ModelAdmin):
    list_display = ('sistema', 'x', 'estado', 'origen')
    list_filter = ('estado', 'origen')


@admin.register(Partitura)
class PartituraAdmin(admin.ModelAdmin):
    list_display = ('titulo', 'instrumento', 'parte', 'owner', 'estado_normalizacion', 'estado_analisis', 'publicada', 'creado')
    list_filter = ('estado_normalizacion', 'estado_analisis', 'publicada', 'instrumento')
    search_fields = ('titulo', 'owner__username')


@admin.register(MarcaTiempoCompas)
class MarcaTiempoCompasAdmin(admin.ModelAdmin):
    list_display = ('obra', 'compas', 'pasada', 'tiempo_inicio', 'explicita')
    list_filter = ('explicita',)
    search_fields = ('obra__titulo',)


@admin.register(Pagina)
class PaginaAdmin(admin.ModelAdmin):
    list_display = ('partitura', 'numero', 'rotacion_detectada', 'angulo_deskew_detectado', 'confirmada', 'umbral_contenido_sistema')
    list_filter = ('confirmada',)


@admin.register(Sistema)
class SistemaAdmin(admin.ModelAdmin):
    list_display = ('pagina', 'orden', 'origen', 'confirmado', 'contenido_x0', 'contenido_x1')


@admin.register(Compas)
class CompasAdmin(admin.ModelAdmin):
    list_display = ('sistema', 'numero', 'x', 'y', 'width', 'height', 'origen', 'confirmado')
    list_filter = ('origen', 'confirmado')


@admin.register(PreferenciaObra)
class PreferenciaObraAdmin(admin.ModelAdmin):
    list_display = ('usuario', 'obra', 'desde_compas', 'hasta_compas', 'loop', 'velocidad', 'compases_al_aire', 'parte_seguida', 'actualizado')
    search_fields = ('usuario__username', 'obra__titulo')


@admin.register(PreferenciaParte)
class PreferenciaParteAdmin(admin.ModelAdmin):
    list_display = ('usuario', 'partitura', 'nivel_zoom', 'actualizado')
    search_fields = ('usuario__username', 'partitura__titulo')
