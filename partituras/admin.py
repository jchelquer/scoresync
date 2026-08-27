from django.contrib import admin, messages
from django.shortcuts import redirect, render
from django.urls import path
from .media_cleanup import borrar_huerfanos, encontrar_huerfanos
from .models import (
    Barra, Ciclo, Compas, EfectoTempo, MarcaNotacion, MarcaTiempoCompas, MarcaTiempoPulso, Obra, Pagina,
    PreferenciaObra, PreferenciaParte, Repertorio, Segmento, Sistema, Partitura,
)


class RepertorioGrupoVisibleInline(admin.TabularInline):
    """Repertorio.grupos_visibles tiene through explícito (db_constraint=False
    hacia GrupoUsuario, ver models.py) — Django admin no permite filter_horizontal
    en ese caso, el inline sobre la tabla intermedia es el patrón estándar."""
    model = Repertorio.grupos_visibles.through
    extra = 1
    verbose_name = "Grupo visible"
    verbose_name_plural = "Grupos visibles (vacío = público)"


@admin.register(Repertorio)
class RepertorioAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'get_grupos_visibles')
    search_fields = ('nombre',)
    inlines = [RepertorioGrupoVisibleInline]

    @admin.display(description="Grupos visibles")
    def get_grupos_visibles(self, obj):
        return ", ".join(obj.grupos_visibles.values_list("nombre", flat=True)) or "(público)"


@admin.register(Ciclo)
class CicloAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'repertorio')
    list_filter = ('repertorio',)
    search_fields = ('nombre', 'repertorio__nombre')


class ObraGrupoVisibleInline(admin.TabularInline):
    """Obra.grupos_visibles tiene through explícito, mismo motivo que
    RepertorioGrupoVisibleInline — sólo importa con restriccion='restringida'."""
    model = Obra.grupos_visibles.through
    extra = 1
    verbose_name = "Grupo visible"
    verbose_name_plural = "Grupos visibles (sólo con restricción = Restringida)"


@admin.register(Obra)
class ObraAdmin(admin.ModelAdmin):
    list_display = ('titulo', 'compositor', 'arreglista', 'ciclo', 'owner', 'publicada', 'restriccion', 'creado')
    list_filter = ('publicada', 'restriccion', 'ciclo__repertorio', 'ciclo')
    search_fields = ('titulo', 'compositor', 'arreglista', 'owner__username')
    filter_horizontal = ('usuarios_visibles',)
    inlines = [ObraGrupoVisibleInline]
    change_list_template = "admin/partituras/obra/change_list.html"

    def get_urls(self):
        urls = [
            path(
                "limpiar-huerfanos/",
                self.admin_site.admin_view(self.limpiar_huerfanos_view),
                name="partituras_obra_limpiar_huerfanos",
            ),
        ]
        return urls + super().get_urls()

    def limpiar_huerfanos_view(self, request):
        """GET sólo escanea y muestra; el borrado real recién ocurre en el
        POST del form de confirmación de la misma pantalla (ver
        media_cleanup.py -- comparte lógica con el management command
        limpiar_huerfanos)."""
        huerfanos = encontrar_huerfanos()
        if request.method == "POST":
            total = len(huerfanos["audios"]) + len(huerfanos["pdfs"]) + len(huerfanos["cache_paginas"])
            borrar_huerfanos(huerfanos)
            messages.success(request, f"{total} elemento(s) huérfano(s) borrado(s).")
            return redirect("admin:partituras_obra_changelist")
        context = {
            **self.admin_site.each_context(request),
            "huerfanos": huerfanos,
            "kb_total": huerfanos["kb_audios"] + huerfanos["kb_pdfs"] + huerfanos["kb_cache_paginas"],
            "title": "Limpiar archivos huérfanos",
            "opts": self.model._meta,
        }
        return render(request, "admin/partituras/obra/limpiar_huerfanos.html", context)


@admin.register(Segmento)
class SegmentoAdmin(admin.ModelAdmin):
    list_display = ('obra', 'orden', 'compas_desde', 'compas_hasta', 'descripcion')
    search_fields = ('obra__titulo', 'descripcion')


@admin.register(MarcaNotacion)
class MarcaNotacionAdmin(admin.ModelAdmin):
    list_display = ('obra', 'tipo', 'compas', 'pasada', 'valor')
    list_filter = ('tipo',)
    search_fields = ('obra__titulo',)


@admin.register(EfectoTempo)
class EfectoTempoAdmin(admin.ModelAdmin):
    list_display = ('obra', 'tipo', 'desde_texto', 'hasta_texto', 'valor')
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


@admin.register(MarcaTiempoPulso)
class MarcaTiempoPulsoAdmin(admin.ModelAdmin):
    list_display = ('obra', 'compas', 'pasada', 'pulso', 'tiempo_inicio', 'explicita')
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
    ordering = (
        'sistema__pagina__partitura__obra__titulo',
        'sistema__pagina__partitura',
        'sistema__pagina__numero',
        'sistema__orden',
        'x',
    )


@admin.register(PreferenciaObra)
class PreferenciaObraAdmin(admin.ModelAdmin):
    list_display = ('usuario', 'obra', 'desde_compas', 'hasta_compas', 'loop', 'velocidad', 'compases_al_aire', 'parte_seguida', 'actualizado')
    search_fields = ('usuario__username', 'obra__titulo')


@admin.register(PreferenciaParte)
class PreferenciaParteAdmin(admin.ModelAdmin):
    list_display = ('usuario', 'partitura', 'nivel_zoom', 'actualizado')
    search_fields = ('usuario__username', 'partitura__titulo')
