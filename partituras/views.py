import json
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

import cv2
import numpy as np
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from .audio import normalizar_audio_referencia
from .forms import (
    EfectoTempoFormSet, MarcaNotacionFormSet, ObraEditForm, ObraForm, PartituraEditForm,
    PartituraForm, RepertorioForm, SegmentoFormSet,
)
from .models import (
    Anotacion, Barra, Ciclo, Compas, EfectoTempo, MarcaNotacion, MarcaTiempoCompas, MarcaTiempoPulso, Obra, Pagina,
    Partitura, PreferenciaObra, PreferenciaParte, Repertorio, Segmento, Sistema,
)
from .normalizacion import detectar_angulo_deskew, detectar_rotacion_90, normalizar_pagina
from .pdf import armar_pdf_desde_imagenes, contar_paginas, rasterizar_pagina
from .services import (
    armadura_transportada, avanzar_compas, borrar_marcas_compas, buscar_posicion, compases_desenrollados,
    construir_plan, desplazar_marcas_compas, geometria_partitura, guardar_compases_pagina,
    indice_pausas, interpolar_marcas_compas, invalidar_desde_ancla, invalidar_desde_margenes,
    invalidar_desde_orientacion, invalidar_desde_sistemas, numero_inicial_pagina,
    parsear_compas_pulso, recalcular_tiempos_calculados, renumerar_segmentos,
    resolver_efecto_tempo_en_compas, resolver_notacion_en_compas, retroceder_compas,
    segmentos_navegables, tiempo_real_ancla,
)
from .vision import (
    UMBRAL_CONTENIDO_SISTEMA_DEFAULT, UMBRAL_RELATIVO_BARRA_DOBLE, UMBRAL_SEPARACION_SISTEMAS_DEFAULT,
    buscar_barra_en_rectangulo, detectar_barras_candidatas, detectar_borde_contenido_sistema,
    detectar_borde_fin_sistema, detectar_margenes, detectar_sistemas, encontrar_ancla,
)

DPI = 300


# Orden fijo del pipeline de preparación — cada etapa se habilita recién
# cuando la anterior está confirmada en TODAS las páginas activas de la
# partitura (ver Partitura.margenes_completos y análogas). "orientacion" no
# tiene una propiedad de completitud propia porque ya existe
# `estado_normalizacion` con el mismo sentido.
_ETAPAS = ["ajuste_orientacion", "ajuste_margenes", "ajuste_sistemas", "ajuste_ancla", "ajuste_barras"]


def _siguiente_etapa(url_name):
    idx = _ETAPAS.index(url_name)
    return _ETAPAS[idx + 1] if idx + 1 < len(_ETAPAS) else None


def _siguiente_pagina(partitura, pk, url_name, numero_actual, campo_confirmado):
    """Redirige a la próxima página (excluyendo la actual e ignoradas) que
    todavía no tiene `campo_confirmado` en True, dentro de la misma etapa
    (`url_name`). Si no queda ninguna, la etapa está completa: encadena
    directo a la primera página de la etapa siguiente (o al detalle si ésta
    era la última) — el usuario nunca tiene que volver al menú a mitad de
    camino."""
    siguiente = partitura.paginas.filter(
        ignorada=False, **{campo_confirmado: False},
    ).exclude(numero=numero_actual).order_by("numero").first()
    if siguiente:
        return redirect(f"partituras:{url_name}", pk=pk, numero=siguiente.numero)
    proxima_etapa = _siguiente_etapa(url_name)
    if proxima_etapa:
        return redirect(f"partituras:{proxima_etapa}", pk=pk, numero=1)
    return redirect("partituras:detalle", pk=pk)


def _primera_pendiente(partitura, campo, numero_default=1):
    pagina = partitura.paginas.filter(ignorada=False, **{campo: False}).order_by("numero").first()
    return pagina.numero if pagina else numero_default


def _primera_pendiente_sistemas(partitura, numero_default=1):
    for pagina in partitura.paginas.filter(ignorada=False).order_by("numero"):
        if not pagina.sistemas_confirmados:
            return pagina.numero
    return numero_default


def _proximo_paso(partitura):
    """(url_name, numero) de la primera página pendiente en la primera
    etapa incompleta — o None si no hay nada pendiente todavía por arrancar
    (o si ya está todo confirmado). Es lo que hace que abrir una partitura
    te lleve directo a lo que falta, en vez de al menú."""
    if partitura.estado_normalizacion == "pendiente":
        return None  # ni siquiera se corrió "Enderezar PDF" — no hay nada a lo que saltar
    if partitura.estado_normalizacion != "confirmada":
        pagina = partitura.paginas.filter(confirmada=False).order_by("numero").first()
        return ("ajuste_orientacion", pagina.numero) if pagina else None
    if not partitura.margenes_completos:
        return ("ajuste_margenes", _primera_pendiente(partitura, "margen_confirmado"))
    if not partitura.sistemas_completos:
        return ("ajuste_sistemas", _primera_pendiente_sistemas(partitura))
    if not partitura.ancla_completa:
        return ("ajuste_ancla", _primera_pendiente(partitura, "ancla_confirmada"))
    if not partitura.barras_completas:
        return ("ajuste_barras", _primera_pendiente(partitura, "barras_confirmadas"))
    return None


@login_required
def partes_sueltas(request):
    """Partituras propias sin obra — huérfanas por diseño (las partes
    nuevas siempre se cargan desde la ficha de una obra, ver subir; una
    parte sólo queda suelta si se la separa o si se borró su obra).
    Página de limpieza: desde acá se puede editar o borrar cada una."""
    partituras = sorted(
        Partitura.objects.filter(owner=request.user, obra__isnull=True),
        key=lambda p: (p.titulo.lower(), p.nombre_parte.lower()),
    )
    return render(request, "partituras/partes_sueltas.html", {"partituras": partituras})


@login_required
@require_POST
def borrar_partitura(request, pk):
    """Borra una partitura y todo lo que cuelga de ella (páginas, sistemas,
    barras, compases — todo en cascada por FK); los archivos se limpian
    solos vía señal post_delete (ver signals.py), no hace falta acá.
    Vuelve a la ficha de la obra si estaba adjunta, o a partes sueltas si no.
    Del dueño o de un admin (mismo criterio que el resto del pipeline)."""
    partitura = get_object_or_404(Partitura, pk=pk)
    if not (partitura.owner_id == request.user.id or _es_admin(request.user)):
        return HttpResponseForbidden()
    titulo = str(partitura)
    obra_id = partitura.obra_id
    partitura.delete()
    messages.success(request, f'Se borró "{titulo}".')
    if obra_id:
        return redirect("partituras:obra_detalle", pk=obra_id)
    return redirect("partituras:partes_sueltas")


@login_required
@require_POST
def alternar_publicacion_partitura(request, pk):
    """Publicar/despublicar una parte — de su dueño, del dueño de la obra a
    la que está adjunta (si tiene), o de un admin (ver _puede_ver_partitura).
    Le da al dueño de la obra la potestad de aceptar o rechazar una parte
    que subió otro usuario: mientras esté despublicada, sólo la ven su
    propio dueño, el dueño de la obra y los admins (ver estado/_partes_disponibles)."""
    partitura = get_object_or_404(Partitura, pk=pk)
    es_dueño_parte = partitura.owner_id == request.user.id
    es_dueño_obra = bool(partitura.obra_id and partitura.obra.owner_id == request.user.id)
    if not (es_dueño_parte or es_dueño_obra or _es_admin(request.user)):
        return HttpResponseForbidden()
    partitura.publicada = not partitura.publicada
    partitura.save(update_fields=["publicada"])
    return redirect("partituras:estado", pk=pk)


@login_required
@require_POST
def transferir_ownership_partitura(request, pk):
    """Análogo a transferir_ownership_obra, para una parte — mismo criterio
    de permiso que alternar_publicacion_partitura (dueño de la parte, dueño
    de la obra a la que está adjunta, o admin)."""
    partitura = get_object_or_404(Partitura, pk=pk)
    es_dueño_parte = partitura.owner_id == request.user.id
    es_dueño_obra = bool(partitura.obra_id and partitura.obra.owner_id == request.user.id)
    if not (es_dueño_parte or es_dueño_obra or _es_admin(request.user)):
        return HttpResponseForbidden()
    nuevo_owner = _usuarios_transferibles(request.user).filter(pk=request.POST.get("nuevo_owner")).first()
    if not nuevo_owner:
        messages.error(request, _("Usuario inválido."))
        return redirect("partituras:estado", pk=pk)
    partitura.owner = nuevo_owner
    partitura.save(update_fields=["owner"])
    messages.success(request, _('Se transfirió "%(parte)s" a %(usuario)s.') % {
        "parte": partitura, "usuario": nuevo_owner.get_full_name() or nuevo_owner.username,
    })
    return redirect("partituras:estado", pk=pk)


@login_required
def editar_partitura(request, pk):
    """Corrige instrumento/parte de una partitura ya subida (y el título,
    sólo si es una parte suelta — si ya pertenece a una obra, el título es
    el de la obra y no se toca acá) — no el archivo (ver PartituraEditForm).
    Del dueño o de un admin (mismo criterio que el resto del pipeline)."""
    partitura = get_object_or_404(Partitura, pk=pk)
    if not (partitura.owner_id == request.user.id or _es_admin(request.user)):
        return HttpResponseForbidden()
    if request.method == "POST":
        form = PartituraEditForm(request.POST, instance=partitura)
        if partitura.obra_id:
            form.fields['titulo'].disabled = True
        if form.is_valid():
            form.save()
            messages.success(request, f'Se guardaron los cambios de "{partitura}".')
            if partitura.obra_id:
                return redirect("partituras:obra_detalle", pk=partitura.obra_id)
            return redirect("partituras:partes_sueltas")
    else:
        form = PartituraEditForm(instance=partitura)
        if partitura.obra_id:
            form.fields['titulo'].disabled = True
    return render(request, "partituras/editar.html", {"form": form, "partitura": partitura})


@login_required
def subir(request, pk):
    """Cargar una parte (PDF) — siempre asociada a una obra, no hay carga
    suelta (ver notas de diseño: "la biblioteca es una biblioteca de
    obras"). El título no se pide: se toma directo de la obra (una parte
    siempre lo comparte). No hace falta ser dueño de la obra — cualquier
    usuario puede sumarle su propia parte (queda con owner=el que la sube,
    ver Partitura.owner)."""
    obra = get_object_or_404(Obra, pk=pk)
    if request.method == "POST":
        form = PartituraForm(request.POST, request.FILES)
        if form.is_valid():
            partitura = form.save(commit=False)
            partitura.owner = request.user
            partitura.obra = obra
            partitura.titulo = obra.titulo
            partitura.save()
            return redirect("partituras:detalle", pk=partitura.pk)
    else:
        initial = {}
        if request.user.instrumento_principal_id:
            initial["instrumento"] = request.user.instrumento_principal_id
        form = PartituraForm(initial=initial)
    return render(request, "partituras/subir.html", {"form": form, "obra": obra})


def _contexto_estado(request, partitura):
    es_dueño = partitura.owner_id == request.user.id
    es_dueño_obra = bool(partitura.obra_id and partitura.obra.owner_id == request.user.id)
    es_admin = _es_admin(request.user)
    return {
        "partitura": partitura,
        "es_dueño": es_dueño,
        "es_dueño_obra": es_dueño_obra,
        "es_admin": es_admin,
        "preparada": partitura.paginas.filter(compases_confirmados=True).exists(),
        "usuarios_transferibles": _usuarios_transferibles(request.user) if (es_dueño or es_dueño_obra or es_admin) else None,
        "pagina_margenes": _primera_pendiente(partitura, "margen_confirmado"),
        "pagina_sistemas": _primera_pendiente_sistemas(partitura),
        "pagina_ancla": _primera_pendiente(partitura, "ancla_confirmada"),
        "pagina_barras": _primera_pendiente(partitura, "barras_confirmadas"),
        "obras_propias": Obra.objects.filter(owner=request.user),
        "ciclos": Ciclo.objects.select_related("repertorio").all(),
    }


@login_required
def detalle(request, pk):
    """Punto de entrada "inteligente": si hay algo pendiente, te lleva
    directo ahí — nunca hace falta pasar por el menú a propósito. Si no hay
    nada pendiente (o todavía no arrancó nada), muestra el panel de estado.
    Del dueño o de un admin (mismo criterio que el resto del pipeline) — para
    cualquier otro, ver `estado`, que sí es público para ver (sin el salto
    automático ni el panel de edición del pipeline)."""
    partitura = get_object_or_404(Partitura, pk=pk)
    if not (partitura.owner_id == request.user.id or _es_admin(request.user)):
        return HttpResponseForbidden()
    paso = _proximo_paso(partitura)
    if paso:
        url_name, numero = paso
        return redirect(f"partituras:{url_name}", pk=pk, numero=numero)
    return render(request, "partituras/detalle.html", _contexto_estado(request, partitura))


@login_required
def estado(request, pk):
    """El mismo panel que `detalle`, pero sin el salto automático — para
    volver a ver el estado general a propósito (el botón "Salir" de cada
    etapa apunta acá, no a `detalle`, para no rebotar de vuelta a la misma
    pantalla que se acaba de dejar). No hace falta ser dueño de la parte
    para entrar acá (es el link que se muestra desde la ficha de la obra a
    cualquiera) — pero el panel de edición del pipeline (enderezar, ajustar
    márgenes/sistemas/ancla/barras, separar/adjuntar a obra) queda oculto
    para quien no es dueño, ver `es_dueño` en el contexto y detalle.html.
    Sí hace falta poder VER la parte (ver Partitura.publicada/_puede_ver_partitura)
    — una despublicada por el dueño de la obra queda oculta para el resto."""
    partitura = get_object_or_404(Partitura, pk=pk)
    if not _puede_ver_partitura(request.user, partitura):
        raise Http404()
    return render(request, "partituras/detalle.html", _contexto_estado(request, partitura))


# ── Obra (agrupa varias Partitura de la misma pieza, una por parte) ────────

def _es_admin(user):
    """Mismo criterio ya usado en el resto del proyecto (usuarios/sc_versiones,
    ver base.html) — se reusa acá tal cual, no es un mecanismo nuevo."""
    return user.es_admin or user.is_staff


def _repertorio_visible_para(user, repertorio, grupos_usuario_ids=None):
    """grupos_visibles vacío = público. Admin siempre. No contempla "dueño
    de una obra dentro" — eso ya lo resuelve _obra_visible_para aparte; este
    helper es sólo para decidir qué repertorios ofrecer como filtro/categoría
    (ver _ciclos_visibles_qs) y para la porción "permitido_por_repertorio"
    de _obra_visible_para."""
    if _es_admin(user):
        return True
    if grupos_usuario_ids is None:
        grupos_usuario_ids = set(user.grupos.values_list("pk", flat=True))
    grupos_repertorio_ids = set(repertorio.grupos_visibles.values_list("pk", flat=True))
    return not grupos_repertorio_ids or bool(grupos_usuario_ids & grupos_repertorio_ids)


def _obra_visible_para(user, obra, grupos_usuario_ids=None):
    """Punto único de verdad de "¿puede ver esta obra?" — dueño y admin
    siempre, si no tiene que estar publicada Y pasar el filtro de grupo (ver
    Obra.restriccion / Repertorio.grupos_visibles). 'restringida' sólo puede
    ACOTAR lo que el repertorio ya permitía, nunca ampliarlo: por eso se
    exige `permitido_por_repertorio` incluso cuando hay una concesión
    puntual por grupo/usuario en la propia obra. `grupos_usuario_ids`
    opcional para no repetir la consulta al recorrer un listado (ver
    _obras_visibles_qs)."""
    if _es_admin(user) or obra.owner_id == user.id:
        return True
    if not obra.publicada:
        return False
    if obra.restriccion == Obra.RESTRICCION_PUBLICA:
        return True

    if grupos_usuario_ids is None:
        grupos_usuario_ids = set(user.grupos.values_list("pk", flat=True))
    repertorio = obra.ciclo.repertorio if obra.ciclo_id else None
    permitido_por_repertorio = (
        repertorio is None or _repertorio_visible_para(user, repertorio, grupos_usuario_ids=grupos_usuario_ids)
    )

    if obra.restriccion == Obra.RESTRICCION_RESTRINGIDA:
        grupos_obra_ids = set(obra.grupos_visibles.values_list("pk", flat=True))
        concedido = bool(grupos_usuario_ids & grupos_obra_ids) or obra.usuarios_visibles.filter(pk=user.pk).exists()
        return concedido and permitido_por_repertorio

    return permitido_por_repertorio


def _obras_visibles_qs(user):
    """Queryset de las obras que `user` puede ver — equivalente en masa a
    _obra_visible_para, para la biblioteca. Arranca de un candidato amplio
    en la base (publicada o propia) y filtra en Python el resto de la regla:
    la biblioteca es chica, no vale la pena una query ORM con varios joins
    M2M encadenados sólo por eficiencia prematura."""
    if _es_admin(user):
        return Obra.objects.all()
    candidatas = Obra.objects.select_related("owner", "ciclo__repertorio").filter(
        Q(publicada=True) | Q(owner=user)
    )
    grupos_usuario_ids = set(user.grupos.values_list("pk", flat=True))
    ids_visibles = [
        o.pk for o in candidatas
        if _obra_visible_para(user, o, grupos_usuario_ids=grupos_usuario_ids)
    ]
    return Obra.objects.filter(pk__in=ids_visibles)


def _ciclos_visibles_qs(user):
    """Ciclos (y por lo tanto Repertorios, ver Ciclo.__str__) para ofrecer
    como opciones del filtro de la biblioteca — sólo los que `user` puede
    ver según su grupo (ver _repertorio_visible_para). No tiene que ver con
    qué obras existen dentro, sólo con si el repertorio en sí está
    restringido — así el desplegable no revela nombres de repertorios que
    el usuario no debería ni saber que existen."""
    if _es_admin(user):
        return Ciclo.objects.select_related("repertorio").all()
    grupos_usuario_ids = set(user.grupos.values_list("pk", flat=True))
    candidatos = Ciclo.objects.select_related("repertorio")
    ids_visibles = [
        c.pk for c in candidatos
        if _repertorio_visible_para(user, c.repertorio, grupos_usuario_ids=grupos_usuario_ids)
    ]
    return Ciclo.objects.filter(pk__in=ids_visibles).select_related("repertorio")


def _usuarios_transferibles(user):
    """Candidatos para recibir una obra/parte transferida (ver
    transferir_ownership_obra/transferir_ownership_partitura). Usuario es una
    tabla COMPARTIDA entre cuatro apps del mismo Postgres (afinacion/ensayos/
    tempo/scoresync — ver usuarios/models.py), sin ningún campo que diga
    "usa ScoreSync" — un desplegable sin filtro le mostraría a cualquier
    dueño el directorio entero del ecosistema. Un admin sí lo ve sin filtro
    (ya puede reasignar owner sin restricción desde el admin de Django; esto
    sólo lo formaliza acá); cualquier otro sólo ve usuarios que comparten
    AL MENOS UN grupo con él (mismo criterio de grupo que la visibilidad de
    obras/repertorios, ver _obra_visible_para) — si no está en ningún grupo,
    no ve a nadie. Se usa tanto para pintar el desplegable como para VALIDAR
    el POST del lado del servidor — nunca se confía en el id que mandó el
    cliente sin filtrarlo de nuevo por este mismo queryset."""
    User = get_user_model()
    if _es_admin(user):
        return User.objects.prefetch_related('grupos').order_by('first_name', 'last_name', 'username')
    grupos_ids = user.grupos.values_list('pk', flat=True)
    return User.objects.filter(grupos__in=grupos_ids).distinct().prefetch_related('grupos').order_by(
        'first_name', 'last_name', 'username',
    )


def _puede_ver_partitura(user, partitura, obra=None):
    """Análogo a la visibilidad de Obra (ver Partitura.publicada): además
    del dueño de la parte y los admins, el dueño de la OBRA a la que está
    adjunta también la ve siempre — es quien decide si acepta o rechaza una
    parte que subió otro usuario. `obra` opcional: pasarla si ya se tiene a
    mano (evita una consulta extra) — si no, se usa partitura.obra.
    Si la parte está adjunta a una obra, la obra tiene que ser visible
    primero (ver _obra_visible_para) — si no, la restricción por grupo de
    la obra se podría esquivar entrando directo por la URL de una parte."""
    obra = obra if obra is not None else partitura.obra
    if obra is not None and not _obra_visible_para(user, obra):
        return False
    if partitura.publicada:
        return True
    if partitura.owner_id == user.id:
        return True
    if obra and obra.owner_id == user.id:
        return True
    return _es_admin(user)


def _puede_editar_anotacion(user, anotacion):
    """Dueño de la obra para las de nivel 'obra', dueño de la parte para
    las de 'parte', el propio usuario para las privadas — ningún permiso
    nuevo, se apoya en los dueños que ya existen (ver Anotacion.nivel)."""
    if _es_admin(user):
        return True
    nivel = anotacion.nivel
    if nivel == 'obra':
        return anotacion.obra.owner_id == user.id
    if nivel == 'parte':
        return anotacion.partitura.owner_id == user.id
    return anotacion.usuario_id == user.id


def _niveles_permitidos_anotacion(user, obra, partitura):
    """Qué niveles puede CREAR este usuario ahora mismo (obra requiere ser
    su dueño; parte requiere ser dueño de la partitura elegida; privada
    siempre que haya una partitura elegida — no depende de ser su dueño,
    ver notas de diseño de Anotacion)."""
    niveles = []
    if obra.owner_id == user.id or _es_admin(user):
        niveles.append('obra')
    if partitura and (partitura.owner_id == user.id or _es_admin(user)):
        niveles.append('parte')
    if partitura:
        niveles.append('privada')
    return niveles


def _serializar_anotacion(anotacion, user):
    return {
        'id': anotacion.pk,
        'compas': anotacion.compas,
        'texto': anotacion.texto,
        'posicion': anotacion.posicion,
        'tipo': anotacion.tipo,
        'nivel': anotacion.nivel,
        'offset_x': anotacion.offset_x,
        'offset_y': anotacion.offset_y,
        'puede_editar': _puede_editar_anotacion(user, anotacion),
    }


def _obra_incompleta_motivos(obra):
    """Motivos concretos (lista, vacía si la obra ya se puede publicar) por
    los que todavía le falta algo: itinerario armado + al menos una parte
    con los compases confirmados en alguna página (mismo criterio que
    _partes_disponibles usa para decidir si una parte se puede seguir en
    la ejecución) + temporización "por compases" completa (ningún compás
    del rango sin cobertura real — mismo chequeo que bloquea "Ejecutar por
    compases" en navegador_obra.html/pedirPlan). Se evalúan los tres
    chequeos SIEMPRE (no se corta en el primero que falla) para poder
    mostrar en obra_detalle.html/alternar_publicacion_obra exactamente qué
    falta, en vez de un texto fijo con los tres requisitos completos."""
    motivos = []
    navegables = segmentos_navegables(obra)
    if not navegables:
        motivos.append(_('armar el itinerario de ejecución (hay algún segmento sin "compás desde/hasta", o ninguno cargado)'))
    if not Pagina.objects.filter(partitura__obra=obra, compases_confirmados=True).exists():
        motivos.append(_("confirmar los compases de al menos una parte"))
    if navegables:
        pulsos, completo, _cambios, _rampas, _saltos = construir_plan(obra, navegables[0].compas_desde, 1, None, None)
        if not completo or not pulsos:
            motivos.append(_('completar tempo/compás en "Notación" para algún tramo del itinerario'))
        elif any(p['duracion_compases'] is None for p in pulsos):
            motivos.append(_('anclar tiempos reales en "Sincronizar compases" para algún tramo del itinerario'))
    return motivos


def _obra_completa(obra):
    """True si la obra ya se puede publicar — ver _obra_incompleta_motivos."""
    return not _obra_incompleta_motivos(obra)


ORDENES_BIBLIOTECA = {
    "titulo": ("titulo",),
    "compositor": ("compositor", "titulo"),
    "repertorio": ("ciclo__repertorio__nombre", "ciclo__nombre", "titulo", "compositor"),
}


@login_required
def obras(request):
    """La biblioteca: todas las obras PUBLICADAS (de cualquier usuario, no
    sólo las propias — ver notas de diseño, cualquiera puede navegar/sumar
    su parte a cualquier obra), más las propias despublicadas (para que el
    dueño las siga viendo y pueda republicarlas) — también punto de entrada
    para crear una obra sin depender de tener ya una partitura cargada. Un
    admin ve TODO, publicado o no.

    Filtros y orden vienen todos de la querystring (GET simple, sin form
    de sesión) — "q" es una búsqueda libre entre título/compositor/
    arreglista/repertorio/ciclo (cubre lo que antes eran filtros de campo
    separados para compositor/arreglista/título); ciclo y publicada
    acotan por campo aparte. Repertorio y Ciclo se gestionan sólo desde
    el admin (ver Repertorio/Ciclo en models.py) — acá sólo se filtra por
    los que ya existen (no hace falta un filtro de Repertorio aparte: el
    de Ciclo ya lo compone, ver Ciclo.__str__)."""
    lista = _obras_visibles_qs(request.user).select_related("owner", "ciclo__repertorio")

    q = request.GET.get("q", "").strip()
    if q:
        lista = lista.filter(
            Q(titulo__icontains=q) | Q(compositor__icontains=q) | Q(arreglista__icontains=q)
            | Q(ciclo__nombre__icontains=q) | Q(ciclo__repertorio__nombre__icontains=q)
        )
    ciclo_id = request.GET.get("ciclo", "").strip()
    if ciclo_id:
        lista = lista.filter(ciclo_id=ciclo_id)
    publicada = request.GET.get("publicada", "").strip()
    if publicada == "si":
        lista = lista.filter(publicada=True)
    elif publicada == "no":
        lista = lista.filter(publicada=False)

    orden = request.GET.get("orden", "titulo")
    lista = lista.order_by(*ORDENES_BIBLIOTECA.get(orden, ORDENES_BIBLIOTECA["titulo"]))

    return render(request, "partituras/obras.html", {
        "obras": lista,
        "es_admin": _es_admin(request.user),
        "ciclos": _ciclos_visibles_qs(request.user),
        "filtros": request.GET,
        "orden": orden,
        "form_crear_obra": ObraForm(usuarios_candidatos=_usuarios_transferibles(request.user)) if _es_admin(request.user) else None,
    })


@login_required
def obra_detalle(request, pk):
    """Ficha de una obra: sus datos y las partes (partituras) que tiene
    adjuntas, más un formulario para adjuntar otra partitura propia todavía
    sin obra. También el alta/reemplazo del audio de referencia (para poder
    sincronizar itinerario/compases con el tiempo real) junto con su versión
    y enlace de origen (Obra.version/enlace — texto libre, sin validación
    fuerte de URL porque se guardan a mano, no vía ModelForm).

    No hace falta ser dueño de la OBRA para entrar — cualquiera logueado
    puede ver la ficha, elegir qué parte seguir y navegar/ejecutar, SIEMPRE
    QUE la obra esté publicada (si no, sólo el dueño y los admins entran —
    ver Obra.publicada). Cargar una parte nueva o adjuntar una propia suelta
    también está abierto (cada parte tiene su propio dueño, independiente
    del de la obra — ver Partitura.owner). Lo que sí es exclusivo del dueño
    de la obra: borrar la obra, el audio de referencia y sincronizar tiempos
    (ver plantilla y las vistas de sincronización, que sí exigen ser dueño).
    Publicar/despublicar es del dueño O de un admin (ver alternar_publicacion_obra)."""
    obra = get_object_or_404(Obra, pk=pk)
    es_dueño = obra.owner_id == request.user.id
    es_admin = _es_admin(request.user)
    if not _obra_visible_para(request.user, obra):
        raise Http404()
    if request.method == "POST" and "audio_form" in request.POST:
        if not es_dueño:
            return HttpResponseForbidden()
        campos = []
        mensaje_conversion_audio = None
        if request.FILES.get("audio"):
            if obra.audio:
                obra.audio.delete(save=False)
            archivo_subido = request.FILES["audio"]
            # Sólo en la subida (acá) — no toca audios ya guardados, ver
            # audio.normalizar_audio_referencia. Si no hace falta convertir
            # (o algo falla y no hay ffmpeg, etc.) devuelve (None, None) y
            # se sigue con el archivo tal cual llegó, como antes.
            nuevo_audio, mensaje_conversion_audio = normalizar_audio_referencia(archivo_subido)
            obra.audio = nuevo_audio if nuevo_audio is not None else archivo_subido
            campos.append("audio")
        obra.version = request.POST.get("version", "").strip()
        obra.enlace = request.POST.get("enlace", "").strip()
        campos += ["version", "enlace"]
        obra.save(update_fields=campos)
        if mensaje_conversion_audio:
            messages.info(request, mensaje_conversion_audio)
        messages.success(request, 'Se actualizó el audio de referencia.')
        return redirect("partituras:obra_detalle", pk=pk)
    partituras = sorted(
        (p for p in obra.partituras.select_related('owner').all() if _puede_ver_partitura(request.user, p, obra=obra)),
        key=lambda p: p.nombre_parte.lower(),
    )
    for p in partituras:
        p.preparada = p.paginas.filter(compases_confirmados=True).exists()
    obra_incompleta_motivos = _obra_incompleta_motivos(obra)
    return render(request, "partituras/obra_detalle.html", {
        "obra": obra,
        "es_dueño": es_dueño,
        "es_admin": es_admin,
        "usuarios_transferibles": _usuarios_transferibles(request.user) if (es_dueño or es_admin) else None,
        "obra_completa": not obra_incompleta_motivos,
        "obra_incompleta_motivos": obra_incompleta_motivos,
        "partituras": partituras,
        "partituras_sin_obra": Partitura.objects.filter(owner=request.user, obra__isnull=True),
    })


@login_required
def editar_obra(request, pk):
    """Corrige título/compositor/arreglista/ciclo y visibilidad
    (restriccion/grupos_visibles/usuarios_visibles, ver ObraEditForm) de una
    obra ya creada — del dueño o de un admin (mismo criterio que publicar/
    despublicar, ver alternar_publicacion_obra). Repertorio/Ciclo en sí (qué
    existe) se gestiona sólo desde el admin — acá sólo se elige entre los
    ya creados."""
    obra = get_object_or_404(Obra, pk=pk)
    if not (obra.owner_id == request.user.id or _es_admin(request.user)):
        return HttpResponseForbidden()
    usuarios_candidatos = _usuarios_transferibles(request.user)
    if request.method == "POST":
        form = ObraEditForm(request.POST, instance=obra, usuarios_candidatos=usuarios_candidatos)
        if form.is_valid():
            form.save()
            messages.success(request, f'Se guardaron los cambios de "{obra}".')
            return redirect("partituras:obra_detalle", pk=obra.pk)
    else:
        form = ObraEditForm(instance=obra, usuarios_candidatos=usuarios_candidatos)
    return render(request, "partituras/editar_obra.html", {"form": form, "obra": obra})


@login_required
def repertorios_visibilidad(request):
    """Listado admin-only de Repertorios con sus grupos_visibles — punto de
    entrada en la app misma para gestionarlos (crear/renombrar/visibilidad)
    sin depender de /admin/ (ver RepertorioForm/crear_repertorio/
    editar_visibilidad_repertorio). Un Repertorio no tiene dueño, así que a
    diferencia de Obra esto es exclusivamente de admin — si en algún
    momento se agrega ese concepto, acá es donde habría que sumarlo."""
    if not _es_admin(request.user):
        return HttpResponseForbidden()
    return render(request, "partituras/repertorios_visibilidad.html", {
        "repertorios": Repertorio.objects.prefetch_related("grupos_visibles"),
    })


@login_required
def crear_repertorio(request):
    """Alta de un Repertorio nuevo desde la app — ver repertorios_visibilidad.
    Reusa el mismo template que editar_visibilidad_repertorio (repertorio=None
    en el contexto distingue "nuevo" de "editar")."""
    if not _es_admin(request.user):
        return HttpResponseForbidden()
    if request.method == "POST":
        form = RepertorioForm(request.POST)
        if form.is_valid():
            repertorio = form.save()
            messages.success(request, f'Se creó el repertorio "{repertorio}".')
            return redirect("partituras:repertorios_visibilidad")
    else:
        form = RepertorioForm()
    return render(request, "partituras/editar_visibilidad_repertorio.html", {
        "form": form, "repertorio": None,
    })


@login_required
def editar_visibilidad_repertorio(request, pk):
    """Editar nombre/grupos_visibles de este Repertorio — ver repertorios_visibilidad."""
    if not _es_admin(request.user):
        return HttpResponseForbidden()
    repertorio = get_object_or_404(Repertorio, pk=pk)
    if request.method == "POST":
        form = RepertorioForm(request.POST, instance=repertorio)
        if form.is_valid():
            form.save()
            messages.success(request, f'Se guardaron los cambios de "{repertorio}".')
            return redirect("partituras:repertorios_visibilidad")
    else:
        form = RepertorioForm(instance=repertorio)
    return render(request, "partituras/editar_visibilidad_repertorio.html", {
        "form": form, "repertorio": repertorio,
    })


@login_required
@require_POST
def alternar_publicacion_obra(request, pk):
    """Publicar/despublicar una obra — del dueño o de un admin (ver
    _es_admin). Una obra despublicada no aparece en la biblioteca para
    nadie más, y tampoco se puede entrar a su ficha/practicar por URL
    directa (ver obra_detalle/navegador_obra) — sigue existiendo, sólo
    queda invisible para el resto. Despublicar siempre se permite; publicar
    NO, si la obra todavía no está completa (ver _obra_completa) — sería
    confuso para quien la encuentre en la biblioteca sin poder practicarla."""
    obra = get_object_or_404(Obra, pk=pk)
    if not (obra.owner_id == request.user.id or _es_admin(request.user)):
        return HttpResponseForbidden()
    if not obra.publicada:
        motivos = _obra_incompleta_motivos(obra)
        if motivos:
            messages.warning(
                request,
                _("Todavía no se puede publicar: ") + "; ".join(motivos) + ".",
            )
            return redirect("partituras:obra_detalle", pk=pk)
    obra.publicada = not obra.publicada
    obra.save(update_fields=["publicada"])
    return redirect("partituras:obra_detalle", pk=pk)


@login_required
@require_POST
def transferir_ownership_obra(request, pk):
    """Entregar la obra a otro usuario — del dueño actual o de un admin
    (mismo criterio que editar_obra/alternar_publicacion_obra). El nuevo
    dueño tiene que salir del mismo queryset que se le mostró a quien pidió
    la transferencia (ver _usuarios_transferibles) — nunca se confía en el
    id que mandó el POST sin filtrarlo nuevo por ESE mismo conjunto, así un
    dueño no-admin no puede forzar a mano un id fuera de su desplegable."""
    obra = get_object_or_404(Obra, pk=pk)
    if not (obra.owner_id == request.user.id or _es_admin(request.user)):
        return HttpResponseForbidden()
    nuevo_owner = _usuarios_transferibles(request.user).filter(pk=request.POST.get("nuevo_owner")).first()
    if not nuevo_owner:
        messages.error(request, _("Usuario inválido."))
        return redirect("partituras:obra_detalle", pk=pk)
    obra.owner = nuevo_owner
    obra.save(update_fields=["owner"])
    messages.success(request, _('Se transfirió "%(obra)s" a %(usuario)s.') % {
        "obra": obra, "usuario": nuevo_owner.get_full_name() or nuevo_owner.username,
    })
    return redirect("partituras:obra_detalle", pk=pk)


@login_required
@require_POST
def borrar_obra(request, pk):
    """Borra la obra Y todas sus partes DE VERDAD (no sólo las desvincula
    como separar/gestionar_obra — Partitura.obra es SET_NULL ahí a
    propósito, así que hay que borrar cada partitura a mano acá, si no
    obra.delete() sólo las desvincularía). Los archivos (de cada partitura
    y el audio de la obra) se limpian solos vía señal post_delete (ver
    signals.py). La confirmación de este botón (ver obra_detalle.html) ya
    le avisa al usuario cuántas partes se van a perder antes de llegar acá.
    Del dueño o de un admin (mismo criterio que el resto del pipeline)."""
    obra = get_object_or_404(Obra, pk=pk)
    if not (obra.owner_id == request.user.id or _es_admin(request.user)):
        return HttpResponseForbidden()
    titulo = str(obra)
    for partitura in obra.partituras.all():
        partitura.delete()
    obra.delete()
    messages.success(request, f'Se borró "{titulo}" y sus partes.')
    return redirect("partituras:obras")


@login_required
@require_POST
def marcar_tiempo_segmento(request, pk):
    """Guarda (o borra) el tiempo_inicio REAL de una fila puntual — hoy sólo
    se usa para la fila de cierre (marca el fin real de la obra), tocando su
    tiempo en la lista de sincronizar_compases.html (fetch() en cada marca/
    deshacer, no hay pantalla ni redirect asociado). Del dueño o de un admin
    (mismo criterio que el resto del pipeline)."""
    obra = get_object_or_404(Obra, pk=pk)
    if not (obra.owner_id == request.user.id or _es_admin(request.user)):
        return HttpResponseForbidden()
    segmento = get_object_or_404(Segmento, pk=request.POST.get("segmento_id"), obra=obra)

    segundos_raw = request.POST.get("segundos")
    if segundos_raw in (None, ""):
        segmento.tiempo_inicio = None
    else:
        try:
            segundos = float(segundos_raw)
        except ValueError:
            return JsonResponse({"ok": False, "error": "segundos inválido"}, status=400)
        segmento.tiempo_inicio = timedelta(seconds=max(segundos, 0))
    segmento.save(update_fields=["tiempo_inicio"])

    return JsonResponse({
        "ok": True,
        "segmento_id": segmento.id,
        "tiempo_inicio": str(segmento.tiempo_inicio) if segmento.tiempo_inicio is not None else None,
    })


@login_required
def sincronizar_compases(request, pk):
    """Pantalla de sincronización FINA: tap compás a compás (cada ocurrencia,
    repeticiones incluidas — ver MarcaTiempoCompas), más la fila de cierre
    (Segmento.tiempo_inicio) para marcar dónde termina la obra de verdad.
    Del dueño o de un admin (mismo criterio que el resto del pipeline)."""
    obra = get_object_or_404(Obra, pk=pk)
    if not (obra.owner_id == request.user.id or _es_admin(request.user)):
        return HttpResponseForbidden()
    if not obra.audio:
        messages.warning(request, 'Esta obra todavía no tiene un audio de referencia cargado.')
        return redirect("partituras:obra_detalle", pk=pk)

    entradas, _completo = compases_desenrollados(obra)
    if not entradas:
        messages.warning(request, 'Esta obra todavía no tiene compases navegables en el itinerario.')
        return redirect("partituras:obra_detalle", pk=pk)

    # El primer compás de la obra siempre necesita una ancla real para que
    # la interpolación tenga de dónde arrancar — si nadie lo tapeó todavía,
    # se asume que el audio arranca justo ahí (tiempo 0), igual que la fila
    # de cierre se completa sola con la duración del audio del lado del
    # cliente (ver sincronizar_compases.html, sólo ahí se conoce audio.duration).
    primera = entradas[0]
    if not primera["es_cierre"] and primera["tiempo_inicio"] is None:
        MarcaTiempoCompas.objects.get_or_create(
            obra=obra, compas=primera["compas"], pasada=primera["pasada"],
            defaults={"tiempo_inicio": timedelta(0), "explicita": True},
        )
        primera["tiempo_inicio"] = timedelta(0)
        primera["explicita"] = True

    # El ajuste de pulsos (arrastre en sincronizar_compases.html) sólo tiene
    # sentido donde el cálculo automático puede desviarse de lo que se
    # escucha: un calderón (punto exacto) o un compás dentro de un
    # accelerando/ritardando (rango) — en el resto, el reparto lineal ya es
    # correcto y el botón sólo ensucia la lista (pedido del usuario,
    # 2026-07-27).
    compases_con_efecto_tempo = set()
    for efecto in obra.efectos_tempo.all():
        if efecto.tipo == "calderon":
            compases_con_efecto_tempo.add(efecto.compas_desde)
        elif efecto.compas_hasta is not None:
            compases_con_efecto_tempo.update(range(efecto.compas_desde, efecto.compas_hasta + 1))

    for entrada in entradas:
        tiempo_inicio = entrada["tiempo_inicio"]
        entrada["tiempo_inicio_segundos"] = tiempo_inicio.total_seconds() if tiempo_inicio is not None else None
        entrada["ajustable_pulsos"] = (
            not entrada["es_cierre"]
            and bool(entrada.get("pulsos_por_compas"))
            and entrada["compas"] in compases_con_efecto_tempo
        )

    pref = PreferenciaObra.objects.filter(usuario=request.user, obra=obra).first()
    partes_disponibles = _partes_disponibles(obra, request.user)
    partitura_seguida = _partitura_seguida(obra, request, pref) if partes_disponibles else None

    # Mismo criterio que navegador_obra: si vino una parte elegida A
    # PROPÓSITO por querystring, se recuerda para la próxima visita.
    parte_id_qs = _leer_entero(request.GET.get("parte"), None)
    if parte_id_qs and partitura_seguida and partitura_seguida.id == parte_id_qs:
        PreferenciaObra.objects.update_or_create(
            usuario=request.user, obra=obra,
            defaults={"parte_seguida": partitura_seguida},
        )

    return render(request, "partituras/sincronizar_compases.html", {
        "obra": obra,
        "entradas": entradas,
        "tiene_score": partitura_seguida is not None,
        "partitura_seguida": partitura_seguida,
        "partes_disponibles": partes_disponibles,
    })


@login_required
@require_POST
def marcar_tiempo_compas(request, pk):
    """Guarda (o borra) el tiempo real de UNA ocurrencia de compás puntual
    (compas+pasada) — se llama por fetch() desde sincronizar_compases.html
    en cada marca/deshacer/edición manual, no hay pantalla ni redirect
    asociado (mismo criterio que marcar_tiempo_segmento)."""
    obra = get_object_or_404(Obra, pk=pk)
    if not (obra.owner_id == request.user.id or _es_admin(request.user)):
        return HttpResponseForbidden()
    try:
        compas = int(request.POST.get("compas"))
        pasada = int(request.POST.get("pasada"))
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "compás/pasada inválido"}, status=400)

    segundos_raw = request.POST.get("segundos")
    if segundos_raw in (None, ""):
        MarcaTiempoCompas.objects.filter(obra=obra, compas=compas, pasada=pasada).delete()
        return JsonResponse({"ok": True, "compas": compas, "pasada": pasada, "tiempo_inicio": None})

    try:
        segundos = float(segundos_raw)
    except ValueError:
        return JsonResponse({"ok": False, "error": "segundos inválido"}, status=400)
    # Un tap/edición manual siempre es una marca explícita, aunque el
    # compás ya tuviera un valor interpolado (explicita=False) puesto por
    # el botón Interpolar — la mano del usuario siempre pisa/promueve eso.
    marca, _creada = MarcaTiempoCompas.objects.update_or_create(
        obra=obra, compas=compas, pasada=pasada,
        defaults={"tiempo_inicio": timedelta(seconds=max(segundos, 0)), "explicita": True},
    )
    return JsonResponse({
        "ok": True, "compas": compas, "pasada": pasada,
        "tiempo_inicio": str(marca.tiempo_inicio),
    })


@login_required
@require_POST
def marcar_tiempo_pulso(request, pk):
    """Guarda (o borra) el tiempo real de UN pulso puntual dentro de una
    ocurrencia de compás (compas+pasada+pulso) — ancla más fina que
    MarcaTiempoCompas, misma fuente "por compases" (ver
    services._anclas_globales). Se llama por fetch() desde el arrastre de
    pulsos en sincronizar_compases.html, mismo criterio que
    marcar_tiempo_compas: sin pantalla ni redirect asociado."""
    obra = get_object_or_404(Obra, pk=pk)
    if not (obra.owner_id == request.user.id or _es_admin(request.user)):
        return HttpResponseForbidden()
    try:
        compas = int(request.POST.get("compas"))
        pasada = int(request.POST.get("pasada"))
        pulso = int(request.POST.get("pulso"))
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "compás/pasada/pulso inválido"}, status=400)

    segundos_raw = request.POST.get("segundos")
    if segundos_raw in (None, ""):
        MarcaTiempoPulso.objects.filter(obra=obra, compas=compas, pasada=pasada, pulso=pulso).delete()
        return JsonResponse({"ok": True, "compas": compas, "pasada": pasada, "pulso": pulso, "tiempo_inicio": None})

    try:
        segundos = float(segundos_raw)
    except ValueError:
        return JsonResponse({"ok": False, "error": "segundos inválido"}, status=400)
    marca, _creada = MarcaTiempoPulso.objects.update_or_create(
        obra=obra, compas=compas, pasada=pasada, pulso=pulso,
        defaults={"tiempo_inicio": timedelta(seconds=max(segundos, 0)), "explicita": True},
    )
    return JsonResponse({
        "ok": True, "compas": compas, "pasada": pasada, "pulso": pulso,
        "tiempo_inicio": str(marca.tiempo_inicio),
    })


@login_required
def pulsos_compas_actual(request, pk):
    """Tiempo real ACTUAL (explícito o interpolado) de cada pulso de una
    ocurrencia de compás puntual — usado para posicionar los puntos
    arrastrables al abrir el panel de "ajustar pulsos" en
    sincronizar_compases.html, antes de que el usuario corrija nada.
    GET segmento/compas/pasada."""
    obra = get_object_or_404(Obra, pk=pk)
    if not (obra.owner_id == request.user.id or _es_admin(request.user)):
        return HttpResponseForbidden()
    try:
        segmento_id = int(request.GET.get("segmento"))
        compas = int(request.GET.get("compas"))
        pasada = int(request.GET.get("pasada"))
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "segmento/compás/pasada inválido"}, status=400)

    segmento = get_object_or_404(Segmento, pk=segmento_id, obra=obra)
    info = resolver_notacion_en_compas(obra, segmento, compas)
    pulsos_compas = info.get("pulsos_compas")
    if not pulsos_compas:
        return JsonResponse({"ok": False, "error": "no se pudo resolver la notación de este compás"}, status=400)

    explicitos = set(
        MarcaTiempoPulso.objects.filter(obra=obra, compas=compas, pasada=pasada).values_list("pulso", flat=True)
    )
    pulsos = []
    for pulso in range(1, int(pulsos_compas) + 1):
        tiempo = tiempo_real_ancla(obra, segmento_id, compas, pulso, "compases")
        pulsos.append({"pulso": pulso, "tiempo_inicio": tiempo, "explicita": pulso in explicitos})
    return JsonResponse({"ok": True, "compas": compas, "pasada": pasada, "pulsos": pulsos})


def _parsear_compas_pasada_lista(texto):
    """"5:1,5:2,6:1" -> [(5,1),(5,2),(6,1)] — formato compartido en que el
    cliente manda una selección múltiple de sincronizar_compases.html
    (desplazar/interpolar/borrar tiempos). Levanta ValueError si algún par
    no tiene el formato esperado."""
    if not texto:
        return []
    objetivos = []
    for par in texto.split(","):
        c, p = par.split(":")
        objetivos.append((int(c), int(p)))
    return objetivos


@login_required
@require_POST
def desplazar_tiempos_compases(request, pk):
    """Corre una fracción de segundos las MarcaTiempoCompas de la obra — ver
    desplazar_marcas_compas. "compases" (POST, opcional) es una lista
    "compas:pasada,compas:pasada,..." — la selección múltiple hecha en
    sincronizar_compases.html; sin ese parámetro, se corren TODAS."""
    obra = get_object_or_404(Obra, pk=pk)
    if not (obra.owner_id == request.user.id or _es_admin(request.user)):
        return HttpResponseForbidden()
    try:
        delta = float(request.POST.get("delta_segundos"))
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "delta inválido"}, status=400)

    objetivos = None
    compases_raw = request.POST.get("compases")
    if compases_raw:
        try:
            objetivos = _parsear_compas_pasada_lista(compases_raw)
        except ValueError:
            return JsonResponse({"ok": False, "error": "compases inválido"}, status=400)

    n = desplazar_marcas_compas(obra, delta, objetivos=objetivos)
    return JsonResponse({"ok": True, "n": n})


@login_required
@require_POST
def interpolar_tiempos_compases(request, pk):
    """Rellena por interpolación (ver interpolar_marcas_compas) los compases
    sin marca explícita de la selección — "compases" (POST) es la misma
    lista "compas:pasada,..." que usa desplazar_tiempos_compases. Devuelve
    los tiempos resueltos (para que el cliente actualice esas filas sin
    recargar la página), los que no se pudieron resolver por falta de
    ancla, los que tenían las dos anclas pero en el orden real equivocado
    (tapeo contradictorio, ver invertidos en interpolar_marcas_compas), y
    los que ni siquiera tienen posición calculada (falta bpm/indicación de
    compás en el itinerario — no es un problema de anclas, ver
    sin_calcular en interpolar_marcas_compas)."""
    obra = get_object_or_404(Obra, pk=pk)
    if not (obra.owner_id == request.user.id or _es_admin(request.user)):
        return HttpResponseForbidden()
    try:
        objetivos = _parsear_compas_pasada_lista(request.POST.get("compases", ""))
    except ValueError:
        return JsonResponse({"ok": False, "error": "compases inválido"}, status=400)

    _resueltos, no_resueltos, invertidos, sin_calcular = interpolar_marcas_compas(obra, objetivos)
    excluidos = set(no_resueltos) | set(sin_calcular)
    marcas = {
        (m.compas, m.pasada): m.tiempo_inicio.total_seconds()
        for m in obra.marcas_tiempo_compas.filter(
            compas__in=[c for c, _p in objetivos]
        ) if (m.compas, m.pasada) in objetivos and (m.compas, m.pasada) not in excluidos
    }
    return JsonResponse({
        "ok": True,
        "resueltos": [{"compas": c, "pasada": p, "tiempo_inicio": marcas[(c, p)]} for c, p in objetivos if (c, p) in marcas],
        "no_resueltos": [{"compas": c, "pasada": p} for c, p in no_resueltos],
        "invertidos": [{"compas": c, "pasada": p} for c, p in invertidos],
        "sin_calcular": [{"compas": c, "pasada": p} for c, p in sin_calcular],
    })


@login_required
@require_POST
def borrar_tiempos_compases(request, pk):
    """Borra en lote (explícitas y no-explícitas) las MarcaTiempoCompas de la
    selección — ver borrar_marcas_compas. "compases" (POST) es la misma
    lista "compas:pasada,..." que usan los otros endpoints de selección."""
    obra = get_object_or_404(Obra, pk=pk)
    if not (obra.owner_id == request.user.id or _es_admin(request.user)):
        return HttpResponseForbidden()
    try:
        objetivos = _parsear_compas_pasada_lista(request.POST.get("compases", ""))
    except ValueError:
        return JsonResponse({"ok": False, "error": "compases inválido"}, status=400)

    n = borrar_marcas_compas(obra, objetivos)
    return JsonResponse({"ok": True, "n": n})


@login_required
def itinerario_obra(request, pk):
    """Tabla editable del itinerario de ejecución de la obra — insertar,
    editar o borrar filas de una, sin pantalla gráfica: cada fila es un
    tramo de compases que se toca de corrido (ver Segmento). Usa un
    formset de Django en vez de JS a medida — es justo lo que hace falta
    para "llenar una tabla", nada más."""
    obra = get_object_or_404(Obra, pk=pk)
    if not (obra.owner_id == request.user.id or _es_admin(request.user)):
        return HttpResponseForbidden()
    queryset = Segmento.objects.filter(obra=obra).order_by("orden")

    if request.method == "POST":
        formset = SegmentoFormSet(request.POST, queryset=queryset, prefix="segmentos")
        if formset.is_valid():
            # Todo el guardado va envuelto en una única transacción: son
            # varios saves en pasos (offset temporal, renumerar, recalcular
            # tiempos) y sin atomic() cada uno se commitea solo — si alguno
            # de la mitad para adelante fallaba (p.ej. la colisión que
            # describe el comentario de abajo, en un caso límite no
            # cubierto), lo ya guardado quedaba pegado en la base con
            # valores de orden temporales, rompiendo cualquier guardado
            # futuro contra esa fila (pasó una vez, ver el commit que
            # agregó este comentario).
            with transaction.atomic():
                # (obra, orden) es unique_together, así que guardar cada fila
                # con el "orden" tal cual lo tipeó el usuario puede chocar
                # contra el de otra fila que todavía no se actualizó (p.ej.
                # insertar una fila nueva en el medio). Se guarda primero en
                # un rango que no puede existir todavía — evita cualquier
                # colisión sin importar el orden de guardado — y recién
                # después renumerar_segmentos() asigna los valores finales
                # limpios.
                # (deleted_objects sólo queda poblado después de llamar a
                # save(), así que hay que leerlo recién acá, no antes.)
                instancias_tocadas = formset.save(commit=False)
                for eliminada in formset.deleted_objects:
                    eliminada.delete()

                # El orden EFECTIVO deseado combina las filas tocadas (con el
                # "orden" que el usuario acaba de tipear, ya aplicado en
                # memoria por el formset pero todavía sin persistir) y las NO
                # tocadas (con el que ya tenían guardado) — ordenar sólo las
                # tocadas entre sí (como hacía antes) ignoraba por completo
                # dónde debían quedar relativas a las que no cambiaron: pedir
                # "orden=5" para que una fila pase a ser la primera no hacía
                # nada, porque esa fila se mandaba a un rango temporal aparte
                # sin comparar contra el 10/20/30... de las filas intactas.
                #
                # Se ordena PURO por orden — antes había un criterio extra
                # (s.compas_desde is None primero en la tupla) que forzaba
                # cualquier fila de cierre siempre al final, sin importar su
                # propio orden tipeado. Tenía sentido cuando sólo podía haber
                # UNA fila de cierre (la del fin de la obra); ahora que
                # también puede haber una interna, en medio del itinerario
                # (pausa entre movimientos), ese forzado la empujaba mal
                # hasta el final de todas formas — bug real, encontrado
                # 2026-07-30 porque el usuario armó una pausa de verdad y la
                # fila de cierre interna terminaba después del movimiento
                # siguiente en vez de antes.
                ids_tocados = {i.pk for i in instancias_tocadas if i.pk}
                no_tocadas = list(Segmento.objects.filter(obra=obra).exclude(pk__in=ids_tocados))
                orden_deseado = sorted(
                    list(instancias_tocadas) + no_tocadas,
                    key=lambda s: s.orden,
                )

                # OJO: este offset temporal tiene que ser DISTINTO del que usa
                # renumerar_segmentos() puertas adentro (10_000_000) — si
                # fueran el mismo, la primera fila que renumerar_segmentos()
                # procesa (la de menor orden actual) intenta escribir
                # exactamente el valor que una instancia recién guardada acá
                # ya ocupa (todavía sin reprocesar, porque una fila tocada
                # siempre queda última en el orden por tener un "orden"
                # gigante) → IntegrityError. Pasó de verdad, reproducido y
                # confirmado antes de este fix — con rangos que nunca se
                # pisan (acá muy por encima de lo que renumerar_segmentos
                # llega a usar, aun con miles de filas) no puede volver a
                # pasar. Ahora se guardan TODAS las filas (tocadas y no) en
                # este rango, ya en el orden deseado calculado arriba, para
                # que renumerar_segmentos() (que sólo sabe releer el orden
                # actual de la base) reciba esa posición correcta en vez de
                # tener que adivinarla.
                OFFSET_TEMPORAL_VISTA = 50_000_000
                for i, seg in enumerate(orden_deseado):
                    seg.obra = obra
                    seg.orden = OFFSET_TEMPORAL_VISTA + i
                    seg.save()

                # Vuelve a numerar de a 10 en el orden actual — así una fila
                # insertada "entre medio" (con un orden como 15) recupera
                # hueco completo alrededor para la próxima inserción, en vez
                # de ir agotándose de a poco.
                renumerar_segmentos(obra)

                # Si la ÚLTIMA fila (por orden) todavía no es una de cierre
                # (compas_desde vacío, sólo marca dónde termina el último
                # compás real — ver docstring de Segmento), se agrega sola:
                # no hay forma intuitiva de armarla a mano desde la tabla
                # (hay que saber dejar Desde/Hasta en blanco y no pisar
                # ningún orden existente), y sin ella nunca se ve el tiempo
                # estimado de fin de la obra. OJO: se chequea la ÚLTIMA fila
                # puntualmente, no "si existe alguna" — puede haber otra
                # fila de cierre MÁS ADENTRO del itinerario (una pausa entre
                # movimientos, ver EfectoTempo tipo 'pausa'), que no cuenta
                # como la de fin de obra.
                ultimo_segmento = Segmento.objects.filter(obra=obra).order_by("-orden").first()
                if ultimo_segmento is not None and ultimo_segmento.compas_desde is not None:
                    Segmento.objects.create(obra=obra, orden=ultimo_segmento.orden + 10)

                # Recalcula tiempo_inicio_calculado de toda la obra (no sólo
                # las filas tocadas: cambiar un bpm más arriba corre el
                # cálculo de todo lo que sigue) y de paso avisa — sin
                # bloquear el guardado — si algún pulso quedó fuera del
                # rango de su indicación de compás.
                resueltos = recalcular_tiempos_calculados(obra)

            for info in resueltos:
                seg = info["segmento"]
                pulsos_compas = info["pulsos_por_compas"]
                if not pulsos_compas or seg.compas_hasta is None:
                    continue
                pulso_desde = seg.pulso_desde if seg.pulso_desde is not None else 1
                pulso_hasta = seg.pulso_hasta if seg.pulso_hasta is not None else pulsos_compas
                if not (1 <= pulso_desde <= pulsos_compas):
                    messages.warning(
                        request,
                        f"Fila {seg.orden}: el pulso desde ({pulso_desde:g}) está fuera de rango "
                        f"para {info['indicacion_compas']} (1 a {pulsos_compas:g}).",
                    )
                if not (1 <= pulso_hasta <= pulsos_compas):
                    messages.warning(
                        request,
                        f"Fila {seg.orden}: el pulso hasta ({pulso_hasta:g}) está fuera de rango "
                        f"para {info['indicacion_compas']} (1 a {pulsos_compas:g}).",
                    )

            messages.success(request, _("Se guardó el itinerario."))
            return redirect("partituras:itinerario_obra", pk=pk)
    else:
        formset = SegmentoFormSet(queryset=queryset, prefix="segmentos")

    return render(request, "partituras/itinerario_obra.html", {
        "obra": obra,
        "formset": formset,
        "umbrales_con_pausa": {p["compas_desde"] for p in indice_pausas(obra)},
    })


@login_required
def notacion_obra(request, pk):
    """Tabla editable de las marcas de notación de la obra (indicación de
    compás, armadura, tempo base — ver MarcaNotacion) y de sus efectos de
    tempo (accelerando/ritardando/calderón — ver EfectoTempo): dos formsets
    en la misma pantalla, ambos hechos de la PARTITURA por posición
    (compás[,pulso]), no del itinerario. Mismo patrón que itinerario_obra:
    formsets de Django para "llenar una tabla", sin orden propio que
    renumerar."""
    obra = get_object_or_404(Obra, pk=pk)
    if not (obra.owner_id == request.user.id or _es_admin(request.user)):
        return HttpResponseForbidden()
    queryset_notacion = MarcaNotacion.objects.filter(obra=obra).order_by("tipo", "compas", "pasada")
    queryset_efectos = EfectoTempo.objects.filter(obra=obra).order_by("compas_desde", "pulso_desde")

    if request.method == "POST":
        formset = MarcaNotacionFormSet(request.POST, queryset=queryset_notacion, prefix="notacion")
        formset_efectos = EfectoTempoFormSet(request.POST, queryset=queryset_efectos, prefix="efectos")
        if formset.is_valid() and formset_efectos.is_valid():
            with transaction.atomic():
                instancias = formset.save(commit=False)
                for instancia in instancias:
                    instancia.obra = obra
                    instancia.save()
                for eliminada in formset.deleted_objects:
                    eliminada.delete()

                instancias_efectos = formset_efectos.save(commit=False)
                for instancia in instancias_efectos:
                    instancia.obra = obra
                    instancia.save()
                for eliminada in formset_efectos.deleted_objects:
                    eliminada.delete()

                recalcular_tiempos_calculados(obra)
            messages.success(request, _("Se guardó la notación."))
            return redirect("partituras:notacion_obra", pk=pk)
    else:
        formset = MarcaNotacionFormSet(queryset=queryset_notacion, prefix="notacion")
        formset_efectos = EfectoTempoFormSet(queryset=queryset_efectos, prefix="efectos")

    return render(request, "partituras/notacion_obra.html", {
        "obra": obra,
        "formset": formset,
        "formset_efectos": formset_efectos,
        "umbrales_con_fila_itinerario": set(
            obra.segmentos.filter(compas_desde__isnull=False, compas_hasta__isnull=True)
            .values_list("compas_desde", flat=True)
        ),
    })


def _leer_entero(valor, default):
    try:
        return int(valor) if valor not in (None, "") else default
    except ValueError:
        return default


def _partes_disponibles(obra, user):
    """Partes de esta obra que se pueden seguir en la ejecución — sólo las
    que ya tienen compases confirmados en alguna página (mostrar una parte
    a medio procesar sería peor que no mostrar nada) Y que `user` puede ver
    (publicada, o es su dueña, o es dueño de la obra, o admin — ver
    _puede_ver_partitura; así una parte despublicada por el dueño de la
    obra deja de ofrecerse para seguir, pero su propio dueño la sigue
    viendo mientras espera que se acepte). Alfabético por nombre_parte (no
    por el campo 'parte' en crudo — está vacío en varias partes, y ordenar
    por ahí las agrupa todas al principio en vez de por el instrumento que
    se termina mostrando)."""
    partituras = sorted(obra.partituras.select_related('owner').all(), key=lambda p: p.nombre_parte.lower())
    return [
        p for p in partituras
        if p.paginas.filter(compases_confirmados=True).exists() and _puede_ver_partitura(user, p, obra=obra)
    ]


def _partitura_seguida(obra, request, pref=None):
    """La parte de esta obra que se usa para mostrar el score durante la
    ejecución. Prioridad: (1) la elegida explícitamente por querystring
    (?parte=<id>), si es válida — así el selector del navegador puede
    cambiarla; (2) la última elegida explícitamente en una visita anterior
    (PreferenciaObra.parte_seguida), si sigue disponible; (3) la propia del
    usuario logueado (Partitura.owner), el default más útil: "mi parte" sin
    tener que elegir nada; (4) la primera disponible, si ninguna de las
    anteriores aplica."""
    candidatas = _partes_disponibles(obra, request.user)
    if not candidatas:
        return None
    partitura_id = _leer_entero(request.GET.get("parte"), None)
    if partitura_id:
        elegida = next((p for p in candidatas if p.id == partitura_id), None)
        if elegida:
            return elegida
    if pref and pref.parte_seguida_id:
        guardada = next((p for p in candidatas if p.id == pref.parte_seguida_id), None)
        if guardada:
            return guardada
    propia = next((p for p in candidatas if p.owner_id == request.user.id), None)
    if propia:
        return propia
    return candidatas[0]


@login_required
def navegador_obra(request, pk):
    """Navegador manual del itinerario de ejecución: muestra en qué compás
    está parado (entero — todavía no por pulso, decisión explícita para esta
    primera versión) con la info resuelta de la fila que lo contiene (tempo,
    indicación de compás, descripción), y deja moverse de a un compás con
    Anterior/Siguiente. Sin reproducción automática ni referencia visual al
    score — es la fase 1 del "player" (ver notas de diseño del proyecto):
    no hay tiempo_inicio real todavía, así que no hay nada que auto-avanzar.

    Todo el estado (posición actual, rango desde-hasta, loop) viaja en la
    querystring — no hay nada que guardar en sesión ni en la base: esta
    pantalla es de sólo lectura, no modifica el itinerario.

    No hace falta ser dueño de la obra — cualquier usuario logueado puede
    navegar/ejecutar cualquier obra publicada (ver obra_detalle); lo único
    que se guarda es PreferenciaObra del propio usuario, no algo de la obra."""
    obra = get_object_or_404(Obra, pk=pk)
    if not _obra_visible_para(request.user, obra):
        raise Http404()
    navegables = segmentos_navegables(obra)
    if not navegables:
        return render(request, "partituras/navegador_obra.html", {
            "obra": obra, "sin_contenido": True,
        })

    # Preferencias guardadas de este usuario para esta obra (rango, loop,
    # velocidad, compases al aire, última parte elegida) — se usan como
    # segundo nivel de default, por debajo de la querystring: un link
    # explícito (Anterior/Siguiente, uno compartido) siempre gana; si la
    # clave ni siquiera viene en la URL, se completa con lo guardado en vez
    # de arrancar de cero. Se autoguardan solas desde el JS del navegador
    # (ver guardar_preferencias_obra), no hay botón de "guardar" acá.
    pref = PreferenciaObra.objects.filter(usuario=request.user, obra=obra).first()

    # desde_compas/hasta_compas aceptan la misma notación "compás,pulso" que
    # desde_texto/hasta_texto en el itinerario (ver parsear_compas_pulso) —
    # el texto crudo se conserva para reponerlo en el input y para armar las
    # URLs de anterior/siguiente; el compás ya parseado (entero) sigue
    # siendo lo que usan buscar_posicion/avanzar_compas/retroceder_compas,
    # que trabajan a nivel de compás, no de pulso.
    if "desde_compas" in request.GET:
        desde_compas_raw = request.GET.get("desde_compas") or ""
    elif pref and pref.desde_compas:
        desde_compas_raw = pref.desde_compas
    else:
        desde_compas_raw = ""
    try:
        desde_compas, desde_pulso = parsear_compas_pulso(desde_compas_raw, 1)
    except ValueError:
        desde_compas, desde_pulso = None, None
    if desde_compas is None:
        desde_compas = navegables[0].compas_desde
        desde_pulso = None
        desde_compas_raw = str(desde_compas)
    if "desde_pasada" in request.GET:
        desde_pasada = _leer_entero(request.GET.get("desde_pasada"), 1)
    else:
        desde_pasada = pref.desde_pasada if pref else 1

    if "hasta_compas" in request.GET:
        hasta_compas_raw = request.GET.get("hasta_compas") or ""
    elif pref and pref.hasta_compas:
        hasta_compas_raw = pref.hasta_compas
    else:
        hasta_compas_raw = ""
    try:
        hasta_compas, hasta_pulso = parsear_compas_pulso(hasta_compas_raw, None)
    except ValueError:
        hasta_compas, hasta_pulso = None, None
    if "hasta_pasada" in request.GET:
        hasta_pasada = _leer_entero(request.GET.get("hasta_pasada"), 1)
    else:
        hasta_pasada = pref.hasta_pasada if pref else 1
    if "loop" in request.GET:
        loop = request.GET.get("loop") == "on"
    else:
        loop = pref.loop if pref else False

    pos_desde = buscar_posicion(obra, desde_compas, desde_pasada) or (navegables[0], navegables[0].compas_desde)
    if hasta_compas_raw:
        pos_hasta = buscar_posicion(obra, hasta_compas or 0, hasta_pasada) \
            or (navegables[-1], navegables[-1].compas_hasta)
    else:
        pos_hasta = (navegables[-1], navegables[-1].compas_hasta)

    # Posición actual: la que venga en la URL (si es válida), si no la de
    # arranque del rango — así entrar sin querystring, o cambiar el rango a
    # mano, siempre lleva a un punto consistente.
    seg_id = _leer_entero(request.GET.get("segmento"), None)
    compas_actual = _leer_entero(request.GET.get("compas"), None)
    segmento_actual = next((s for s in navegables if s.id == seg_id), None) if seg_id else None
    if not segmento_actual or compas_actual is None or not (
        segmento_actual.compas_desde <= compas_actual <= segmento_actual.compas_hasta
    ):
        segmento_actual, compas_actual = pos_desde

    en_fin_de_rango = (segmento_actual.orden, compas_actual) >= (pos_hasta[0].orden, pos_hasta[1])
    siguiente = pos_desde if (en_fin_de_rango and loop) else (
        None if en_fin_de_rango else avanzar_compas(obra, segmento_actual, compas_actual)
    )
    anterior = retroceder_compas(obra, segmento_actual, compas_actual)

    partes_disponibles = _partes_disponibles(obra, request.user)
    partitura_seguida = _partitura_seguida(obra, request, pref) if partes_disponibles else None

    # Si vino una parte elegida A PROPÓSITO por querystring, se recuerda
    # para la próxima visita — no en cada request (sería reescribir la
    # misma fila en cada Anterior/Siguiente sin necesidad), sólo cuando
    # realmente hay una elección explícita en esta URL.
    parte_id_qs = _leer_entero(request.GET.get("parte"), None)
    if parte_id_qs and partitura_seguida and partitura_seguida.id == parte_id_qs:
        PreferenciaObra.objects.update_or_create(
            usuario=request.user, obra=obra,
            defaults={"parte_seguida": partitura_seguida},
        )

    pref_parte = (
        PreferenciaParte.objects.filter(usuario=request.user, partitura=partitura_seguida).first()
        if partitura_seguida else None
    )

    base_params = {
        "desde_compas": desde_compas_raw, "desde_pasada": desde_pasada,
        "hasta_compas": hasta_compas_raw, "hasta_pasada": hasta_pasada,
    }
    if loop:
        base_params["loop"] = "on"
    if partitura_seguida:
        base_params["parte"] = partitura_seguida.pk

    def url_para(posicion):
        if posicion is None:
            return None
        seg, compas = posicion
        params = dict(base_params, segmento=seg.id, compas=compas)
        return f"?{urlencode(params)}"

    info_actual = resolver_notacion_en_compas(obra, segmento_actual, compas_actual)
    variacion_tempo_display, bpm_llegada = resolver_efecto_tempo_en_compas(obra, segmento_actual, compas_actual)

    return render(request, "partituras/navegador_obra.html", {
        "obra": obra,
        "segmento": segmento_actual,
        "compas_actual": compas_actual,
        "indicacion_compas": info_actual.get("indicacion_compas"),
        "armadura": info_actual.get("armadura"),
        "bpm": info_actual.get("bpm"),
        "variacion_tempo_display": variacion_tempo_display,
        "bpm_llegada": bpm_llegada,
        "url_siguiente": url_para(siguiente),
        "url_anterior": url_para(anterior),
        "en_fin_de_rango": en_fin_de_rango and not loop,
        "desde_compas": desde_compas_raw, "desde_pasada": desde_pasada,
        "hasta_compas": hasta_compas_raw, "hasta_pasada": hasta_pasada,
        "loop": loop,
        "tiene_score": partitura_seguida is not None,
        "partitura_seguida": partitura_seguida,
        "partes_disponibles": partes_disponibles,
        "velocidad_guardada": pref.velocidad if pref else 100,
        "compases_al_aire_guardado": pref.compases_al_aire if pref else 1,
        "compases_al_aire_en_loop_guardado": pref.compases_al_aire_en_loop if pref else False,
        "nivel_zoom_guardado": pref_parte.nivel_zoom if pref_parte else 1,
        "ejecutar_con_audio_guardado": pref.ejecutar_con_audio if pref else bool(obra.audio),
        "modo_guardado": pref.modo_score if pref else "",
    })


@login_required
@require_POST
def guardar_preferencias_obra(request, pk):
    """Autoguardado (sin botón, sin redirect) de las preferencias de
    ejecución del usuario para esta obra — rango, loop, velocidad,
    compases al aire (PreferenciaObra) y, si viene zoom+parte, también el
    zoom preferido para esa parte puntual (PreferenciaParte). Lo llama el
    JS del navegador con un pequeño debounce cada vez que el usuario
    cambia algo — es un POST "silencioso" desde fetch(), no hay pantalla
    ni mensaje asociado. No hace falta ser dueño de la obra: esto guarda
    la preferencia del usuario que la llama, no algo de la obra en sí."""
    obra = get_object_or_404(Obra, pk=pk)
    defaults = {
        "desde_compas": (request.POST.get("desde_compas") or "")[:20],
        "desde_pasada": _leer_entero(request.POST.get("desde_pasada"), 1),
        "hasta_compas": (request.POST.get("hasta_compas") or "")[:20],
        "hasta_pasada": _leer_entero(request.POST.get("hasta_pasada"), 1),
        "loop": request.POST.get("loop") == "on",
        "velocidad": max(20, min(150, _leer_entero(request.POST.get("velocidad"), 100))),
        "compases_al_aire": max(0, min(4, _leer_entero(request.POST.get("compases_al_aire"), 1))),
        "compases_al_aire_en_loop": request.POST.get("compases_al_aire_en_loop") == "on",
        "ejecutar_con_audio": request.POST.get("ejecutar_con_audio") == "on",
        "modo_score": (
            request.POST.get("modo_score")
            if request.POST.get("modo_score") in ("compas", "partitura") else ""
        ),
    }
    PreferenciaObra.objects.update_or_create(usuario=request.user, obra=obra, defaults=defaults)

    parte_id = _leer_entero(request.POST.get("parte"), None)
    zoom_raw = request.POST.get("zoom")
    if parte_id and zoom_raw:
        try:
            nivel_zoom = float(zoom_raw)
        except ValueError:
            nivel_zoom = None
        if nivel_zoom is not None:
            partitura = Partitura.objects.filter(pk=parte_id, obra=obra).first()
            if partitura:
                PreferenciaParte.objects.update_or_create(
                    usuario=request.user, partitura=partitura,
                    defaults={"nivel_zoom": max(0.4, min(3, nivel_zoom))},
                )

    return JsonResponse({"ok": True})


@login_required
def plan_obra(request, pk):
    """Plan de ejecución (lista de PULSOS, no de compases — ver
    construir_plan) del rango desde-hasta pedido, en un solo JSON — la
    ejecución en tiempo real lo pide una sola vez al arrancar y de ahí en
    más programa todo con un reloj propio en JS, sin volver a pedirle un
    pulso a la vez al servidor (ver navegador_obra.html: eso dejaría que la
    variabilidad de red se fuera acumulando como desfasaje de tempo). No
    hace falta ser dueño de la obra — ver navegador_obra."""
    obra = get_object_or_404(Obra, pk=pk)
    navegables = segmentos_navegables(obra)
    if not navegables:
        return JsonResponse({"pulsos": [], "completo": True})

    try:
        desde_compas, desde_pulso = parsear_compas_pulso(request.GET.get("desde_compas") or "", 1)
    except ValueError:
        desde_compas, desde_pulso = None, None
    if desde_compas is None:
        desde_compas = navegables[0].compas_desde
        desde_pulso = None
    desde_pasada = _leer_entero(request.GET.get("desde_pasada"), 1)

    hasta_compas_raw = request.GET.get("hasta_compas") or ""
    try:
        hasta_compas, hasta_pulso = parsear_compas_pulso(hasta_compas_raw, None) if hasta_compas_raw else (None, None)
    except ValueError:
        hasta_compas, hasta_pulso = None, None
    hasta_pasada = _leer_entero(request.GET.get("hasta_pasada"), 1)

    pulsos, completo, cambios, rampas, saltos = construir_plan(
        obra, desde_compas, desde_pasada, hasta_compas, hasta_pasada,
        desde_pulso=desde_pulso, hasta_pulso=hasta_pulso,
    )
    # Los cambios de armadura vienen de construir_plan en CONCIERTO (lo que
    # está cargado en Notación) — para el aviso visual del navegador tienen
    # que verse como los lee el instrumentista de la parte que se está
    # siguiendo, no como concierto. Misma parte que score_geometria_obra
    # (_partitura_seguida sin pref: alcanza para este JSON liviano).
    partitura_seguida = _partitura_seguida(obra, request)
    transposicion = (
        partitura_seguida.instrumento.transposicion_semitonos
        if partitura_seguida and partitura_seguida.instrumento_id else None
    )
    for cambio in cambios:
        if cambio["tipo"] == "armadura":
            cambio["valor"] = armadura_transportada(cambio["valor"], transposicion)
    # Ancla real para "Ejecutar con audio": el tiempo real del primer pulso
    # del plan (ver tiempo_real_ancla). Si el pulso venía con fracción (ej.
    # "5,2.5"), pulso_fraccion ubica el punto exacto dentro del pulso — de
    # ahí en más, el cliente suma duracion_compases (ya viene en cada pulso,
    # y el primero ya sale acortado a lo que resta) para saber a qué segundo
    # del audio corresponde cualquier otro pulso del plan.
    primer_pulso_tiempo_real = None
    if pulsos:
        pulso_fraccion = (desde_pulso - int(desde_pulso)) if desde_pulso is not None else 0.0
        primer_pulso_tiempo_real = tiempo_real_ancla(
            obra, pulsos[0]["segmento_id"], pulsos[0]["compas"], pulsos[0].get("pulso", 1),
            pulso_fraccion,
        )
    return JsonResponse({
        "pulsos": pulsos,
        "completo": completo,
        "cambios": cambios,
        "rampas": rampas,
        "saltos": saltos,
        "primer_pulso_tiempo_real": primer_pulso_tiempo_real,
    })


@login_required
def score_geometria_obra(request, pk):
    """Geometría (sistemas/compases por página) de la parte que se sigue
    para mostrar el score durante la ejecución — un solo JSON, pedido una
    vez al arrancar (igual criterio que plan_obra): el cursor sobre el
    score se dibuja después con esto ya en memoria, sin volver a pedirle
    la posición de cada compás al servidor. No hace falta ser dueño de la
    obra — ver navegador_obra."""
    obra = get_object_or_404(Obra, pk=pk)
    partitura = _partitura_seguida(obra, request)
    if not partitura:
        return JsonResponse({"partitura": None, "paginas": []})

    paginas = geometria_partitura(partitura)
    for p in paginas:
        p["imagen_url"] = reverse("partituras:pagina_imagen_normalizada", args=[partitura.pk, p["numero"]])

    return JsonResponse({
        "partitura": {"id": partitura.pk, "titulo": partitura.titulo, "parte": partitura.nombre_parte},
        "paginas": paginas,
    })


@login_required
@require_POST
def exportar_pdf_partitura(request, pk):
    """Arma un PDF de la parte que se está siguiendo (?parte=, mismo
    criterio que score_geometria_obra) a partir de imágenes YA dibujadas
    del lado del cliente — una por página, con lo que esté visible en ese
    momento (números/avisos/rampas/saltos/anotaciones, ver exportarPdf en
    navegador_obra.html). Esta vista no dibuja nada, sólo arma el archivo
    final (ver armar_pdf_desde_imagenes) — evita reimplementar en Python
    el mismo dibujo que ya vive (y se sigue ajustando) del lado del JS.
    "paginas" (POST, archivos) y "numeros" (POST, uno por archivo, mismo
    orden) — se reordena por número acá, no se confía en el orden de
    subida."""
    obra = get_object_or_404(Obra, pk=pk)
    partitura = _partitura_seguida(obra, request)
    if not partitura or not _puede_ver_partitura(request.user, partitura, obra=obra):
        return HttpResponseForbidden()

    archivos = request.FILES.getlist("paginas")
    numeros_raw = request.POST.getlist("numeros")
    if not archivos or len(archivos) != len(numeros_raw):
        return JsonResponse({"ok": False, "error": "faltan páginas o no coinciden con los números"}, status=400)
    try:
        numeros = [int(n) for n in numeros_raw]
    except ValueError:
        return JsonResponse({"ok": False, "error": "número de página inválido"}, status=400)

    pares = sorted(zip(numeros, archivos), key=lambda par: par[0])
    pdf_bytes = armar_pdf_desde_imagenes([archivo.read() for _numero, archivo in pares])

    partes_nombre = [obra.titulo, partitura.nombre_parte or partitura.titulo]
    nombre = "-".join(p for p in partes_nombre if p) + ".pdf"
    nombre = nombre.replace("/", "-")
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{nombre}"'
    return response


@login_required
def anotaciones_obra(request, pk):
    """Anotaciones (carteles de texto anclados a un compás, ver Anotacion)
    visibles para este usuario en esta obra, para la parte que se está
    siguiendo (?parte=, mismo criterio que score_geometria_obra/plan_obra) —
    de obra siempre, de esa parte puntual, y privadas propias de esa parte.
    Sin parte elegida sólo hay de obra (las otras dos dependen de una
    partitura concreta). Devuelve también qué niveles puede CREAR este
    usuario ahora, para que el cliente arme el selector sólo con esas
    opciones."""
    obra = get_object_or_404(Obra, pk=pk)
    partitura = _partitura_seguida(obra, request)

    if partitura:
        visibles = Anotacion.objects.filter(obra=obra).filter(
            Q(partitura__isnull=True)
            | Q(partitura=partitura, usuario__isnull=True)
            | Q(partitura=partitura, usuario=request.user)
        )
    else:
        visibles = Anotacion.objects.filter(obra=obra, partitura__isnull=True)

    return JsonResponse({
        "anotaciones": [_serializar_anotacion(a, request.user) for a in visibles],
        "niveles_permitidos": _niveles_permitidos_anotacion(request.user, obra, partitura),
    })


def _parsear_offset(request):
    """offset_x/offset_y (POST) — el "candado" a un punto exacto dentro de
    la caja del compás (ver Anotacion.offset_x). Ambos o ninguno: si falta
    alguno, o no son números, se toma como "sin offset" (ancla al compás,
    default). Acotado a [0,1] — un click nunca debería mandar algo fuera
    de rango, pero no cuesta nada blindarlo."""
    ox_raw = request.POST.get("offset_x")
    oy_raw = request.POST.get("offset_y")
    if ox_raw is None or oy_raw is None:
        return None, None
    try:
        ox, oy = float(ox_raw), float(oy_raw)
    except ValueError:
        return None, None
    return max(0.0, min(1.0, ox)), max(0.0, min(1.0, oy))


@login_required
@require_POST
def guardar_anotacion(request, pk):
    """Crea (sin "id") o edita (con "id") una anotación — "compas"/"texto"/
    "posicion" siempre; "nivel" ("obra"/"parte"/"privada") y "parte" (id de
    Partitura) sólo hacen falta al CREAR, una anotación no cambia de nivel
    después (ver notas de diseño de Anotacion — reasignar de quién es una
    anotación ya existente no tiene un caso de uso real). El mismo
    endpoint sirve para "mover" (arrastrar): el cliente reenvía el texto
    sin cambios junto con el compás/posición (o el offset, si es de punto
    exacto) nuevos. "offset_x"/"offset_y" (opcionales): el "candado" a un
    punto exacto — se fijan sólo al crear (el modo no cambia después) y,
    si ya estaba en modo punto, se pueden actualizar al arrastrarla
    (nunca al revés: una de compás no pasa a tener offset por esta vía,
    ver más abajo)."""
    obra = get_object_or_404(Obra, pk=pk)
    try:
        compas = int(request.POST.get("compas", ""))
    except ValueError:
        return JsonResponse({"ok": False, "error": "compás inválido"}, status=400)
    texto = request.POST.get("texto", "").strip()
    if not texto:
        return JsonResponse({"ok": False, "error": "el texto no puede estar vacío"}, status=400)
    posicion = request.POST.get("posicion") or "arriba"
    if posicion not in ("arriba", "abajo"):
        return JsonResponse({"ok": False, "error": "posición inválida"}, status=400)
    offset_x, offset_y = _parsear_offset(request)

    anotacion_id = request.POST.get("id")
    if anotacion_id:
        anotacion = get_object_or_404(Anotacion, pk=anotacion_id, obra=obra)
        if not _puede_editar_anotacion(request.user, anotacion):
            return HttpResponseForbidden()
        anotacion.compas = compas
        anotacion.texto = texto
        campos = ["compas", "texto", "actualizado"]
        if anotacion.offset_x is not None and offset_x is not None:
            anotacion.offset_x = offset_x
            anotacion.offset_y = offset_y
            campos += ["offset_x", "offset_y"]
        else:
            anotacion.posicion = posicion
            campos.append("posicion")
        anotacion.save(update_fields=campos)
        return JsonResponse({"ok": True, "anotacion": _serializar_anotacion(anotacion, request.user)})

    nivel = request.POST.get("nivel")
    partitura_id = request.POST.get("parte")
    partitura = get_object_or_404(Partitura, pk=partitura_id) if partitura_id else None
    if nivel == "obra":
        if not (obra.owner_id == request.user.id or _es_admin(request.user)):
            return HttpResponseForbidden()
        anotacion = Anotacion.objects.create(
            obra=obra, compas=compas, texto=texto, posicion=posicion, offset_x=offset_x, offset_y=offset_y,
        )
    elif nivel == "parte":
        if not partitura or not (partitura.owner_id == request.user.id or _es_admin(request.user)):
            return HttpResponseForbidden()
        anotacion = Anotacion.objects.create(
            obra=obra, partitura=partitura, compas=compas, texto=texto, posicion=posicion,
            offset_x=offset_x, offset_y=offset_y,
        )
    elif nivel == "privada":
        if not partitura:
            return JsonResponse({"ok": False, "error": "una anotación privada necesita una parte elegida"}, status=400)
        anotacion = Anotacion.objects.create(
            obra=obra, partitura=partitura, usuario=request.user, compas=compas, texto=texto, posicion=posicion,
            offset_x=offset_x, offset_y=offset_y,
        )
    else:
        return JsonResponse({"ok": False, "error": "nivel inválido"}, status=400)

    return JsonResponse({"ok": True, "anotacion": _serializar_anotacion(anotacion, request.user)})


@login_required
@require_POST
def borrar_anotacion(request, pk):
    obra = get_object_or_404(Obra, pk=pk)
    anotacion = get_object_or_404(Anotacion, pk=request.POST.get("id"), obra=obra)
    if not _puede_editar_anotacion(request.user, anotacion):
        return HttpResponseForbidden()
    anotacion.delete()
    return JsonResponse({"ok": True})


@login_required
def crear_obra(request):
    """Crea una obra nueva — por ahora sólo admins (ver _es_admin); el botón
    ya queda oculto para el resto en los templates, esto es lo que lo hace
    cumplir de verdad. Si se llamó desde la ficha de una partitura
    (partitura_pk en el POST) la adjunta ahí mismo en el mismo paso y vuelve
    a esa partitura; si no, es una creación independiente y va a la ficha de
    la obra recién creada."""
    if not _es_admin(request.user):
        return HttpResponseForbidden()
    if request.method != "POST":
        return redirect("partituras:obras")
    partitura = Partitura.objects.filter(pk=request.POST.get("partitura_pk"), owner=request.user).first()
    form = ObraForm(request.POST, usuarios_candidatos=_usuarios_transferibles(request.user))
    if not form.is_valid():
        return redirect("partituras:estado", pk=partitura.pk) if partitura else redirect("partituras:obras")
    obra = form.save(commit=False)
    obra.owner = request.user
    obra.save()
    form.save_m2m()
    if partitura:
        partitura.obra = obra
        partitura.save(update_fields=["obra"])
        # `estado`, no `detalle` — igual que el botón "Salir" de cada etapa:
        # si se fuera a `detalle` (el router inteligente) y la partitura
        # tiene trabajo pendiente, rebotaría a esa etapa sin mostrar la
        # confirmación de que la obra se creó/adjuntó.
        return redirect("partituras:estado", pk=partitura.pk)
    return redirect("partituras:obra_detalle", pk=obra.pk)


@login_required
def adjuntar_a_obra(request, pk):
    """Adjunta una partitura propia (todavía sin obra) a esta obra, desde
    la propia ficha de la obra — el otro sentido de gestionar_obra. No hace
    falta ser dueño de la OBRA (cualquiera puede sumar su propia parte a
    cualquier obra, ver obra_detalle) — la partitura sí tiene que ser
    propia, eso no cambia."""
    obra = get_object_or_404(Obra, pk=pk)
    if request.method == "POST":
        partitura = Partitura.objects.filter(
            pk=request.POST.get("partitura_id"), owner=request.user, obra__isnull=True,
        ).first()
        if partitura:
            partitura.obra = obra
            partitura.save(update_fields=["obra"])
    return redirect("partituras:obra_detalle", pk=pk)


@login_required
def gestionar_obra(request, pk):
    """Adjunta o separa esta partitura de una obra — de cualquier obra, no
    sólo las propias (no hace falta aprobación del dueño para sumar una
    parte propia a una obra ajena, ver obra_detalle/adjuntar_a_obra)."""
    partitura = get_object_or_404(Partitura, pk=pk)
    if not (partitura.owner_id == request.user.id or _es_admin(request.user)):
        return HttpResponseForbidden()
    if request.method != "POST":
        return redirect("partituras:estado", pk=pk)
    accion = request.POST.get("accion")
    if accion == "adjuntar":
        obra = Obra.objects.filter(pk=request.POST.get("obra_id")).first()
        if obra:
            partitura.obra = obra
            partitura.save(update_fields=["obra"])
    elif accion == "separar":
        partitura.obra = None
        partitura.save(update_fields=["obra"])
    # `next`, si vino de la ficha de una obra (para volver ahí en vez de a la
    # ficha de la partitura) — sólo se acepta una ruta local, no una URL externa.
    siguiente = request.POST.get("next")
    if siguiente and siguiente.startswith("/"):
        return redirect(siguiente)
    return redirect("partituras:estado", pk=pk)


# ── Normalización: rotación + desalineado fino ────────────────────────────

@login_required
def iniciar_normalizacion(request, pk):
    """Crea (o recrea, para páginas no confirmadas) un Pagina por cada
    página del PDF y arranca el ajuste de orientación. POST "accion=omitir"
    (botón "Omitir detección automática", ver detalle.html) salta
    rasterizar_pagina/detectar_rotacion_90/detectar_angulo_deskew por
    completo — quedan en 0/0 (rotación/ángulo), exactamente el mismo
    resultado que produce la detección en una página ya derecha (ver
    normalizacion.py), así que no deja nada en un estado "raro" para las
    pantallas siguientes. Existe porque la detección es O(páginas) DENTRO
    del propio request síncrono — con muchas páginas puede tardar mucho o
    directamente dar timeout, y no es grave si el usuario prefiere ajustar
    todo a mano desde cero."""
    partitura = get_object_or_404(Partitura, pk=pk)
    if not (partitura.owner_id == request.user.id or _es_admin(request.user)):
        return HttpResponseForbidden()
    if request.method != "POST":
        return redirect("partituras:detalle", pk=pk)

    omitir_deteccion = request.POST.get("accion") == "omitir"

    total = contar_paginas(partitura.archivo_original.path)
    ya_confirmadas = set(
        partitura.paginas.filter(confirmada=True).values_list("numero", flat=True)
    )
    for numero in range(1, total + 1):
        if numero in ya_confirmadas:
            continue  # no pisar una página que el usuario ya revisó y confirmó
        if omitir_deteccion:
            rotacion, angulo = 0, 0.0
        else:
            img = rasterizar_pagina(partitura.archivo_original.path, numero, dpi=DPI)
            rotacion = detectar_rotacion_90(img)
            rotada = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE) if rotacion == 90 else img
            angulo = detectar_angulo_deskew(rotada)

        Pagina.objects.update_or_create(
            partitura=partitura, numero=numero,
            defaults={
                "rotacion_detectada": rotacion,
                "angulo_deskew_detectado": angulo,
                "rotacion_aplicada": rotacion,
                "angulo_deskew_aplicado": angulo,
                "confirmada": False,
            },
        )
        # Recrear/rehacer la página puede cambiar rotacion_aplicada/
        # angulo_deskew_aplicado respecto de un intento anterior — sin esto,
        # una imagen cacheada de esa vez quedaba sirviéndose de nuevo con el
        # valor viejo.
        _invalidar_cache_imagen_normalizada(partitura.pk, numero)

    partitura.estado_normalizacion = "propuesta"
    partitura.save(update_fields=["estado_normalizacion"])
    return redirect("partituras:ajuste_orientacion", pk=pk, numero=1)


def _ruta_cache_imagen_normalizada(partitura_id, numero):
    """Dónde vive en disco el PNG cacheado de una página (ver
    pagina_imagen_normalizada) — derivado, no un FileField: se regenera solo,
    no hace falta modelo/migración ni que aparezca en el admin."""
    return Path(settings.MEDIA_ROOT) / "cache_paginas" / str(partitura_id) / f"{numero}.png"


def _invalidar_cache_imagen_normalizada(partitura_id, numero):
    """Borra el PNG cacheado de esta página — llamar siempre que
    rotacion_aplicada/angulo_deskew_aplicado cambien (ver ajuste_orientacion),
    para que la próxima visita regenere la imagen con el valor nuevo en vez
    de seguir sirviendo la vieja."""
    try:
        _ruta_cache_imagen_normalizada(partitura_id, numero).unlink()
    except FileNotFoundError:
        pass


@login_required
def pagina_imagen_normalizada(request, pk, numero):
    """PNG de la página con la rotación/desalineado PROPUESTOS (o ya
    confirmados) aplicados. No exige ser dueño de la partitura: además de
    usarse en la edición propia, score_geometria_obra arma URLs acá para
    mostrar el score durante la ejecución, y ahí puede ser una parte de
    otro usuario (ver navegador_obra).

    Cacheado en disco (rasterizar a 300 DPI + deskew + encode PNG es caro,
    y esta vista se pide de nuevo cada vez que se muestra una página del
    score, aunque su rotación/deskew no haya cambiado) — real, no
    hipotético: un pico de reproceso repetido de esto mientras se navegaba
    rápido entre obras fue lo que tumbó el worker de gunicorn por falta de
    memoria en la VPS (2026-07-27). Sin cambios en los headers de caché del
    lado del navegador (sigue en `no-store`) para no arriesgar servir una
    imagen vieja justo en ajuste_orientacion, donde el usuario necesita ver
    el resultado de rotar/enderezar al toque — el caché es sólo del lado
    del servidor, invisible para el cliente."""
    partitura = get_object_or_404(Partitura, pk=pk)
    pagina = get_object_or_404(Pagina, partitura=partitura, numero=numero)
    ruta_cache = _ruta_cache_imagen_normalizada(pk, numero)
    if ruta_cache.exists():
        png_bytes = ruta_cache.read_bytes()
    else:
        img = rasterizar_pagina(partitura.archivo_original.path, numero, dpi=DPI)
        corregida = normalizar_pagina(img, pagina.rotacion_aplicada, pagina.angulo_deskew_aplicado)
        ok, buf = cv2.imencode(".png", corregida)
        if not ok:
            return HttpResponseBadRequest("No se pudo generar la imagen")
        png_bytes = buf.tobytes()
        ruta_cache.parent.mkdir(parents=True, exist_ok=True)
        ruta_cache.write_bytes(png_bytes)
    response = HttpResponse(png_bytes, content_type="image/png")
    response["Cache-Control"] = "no-store"
    return response


@login_required
def ajuste_orientacion(request, pk, numero):
    partitura = get_object_or_404(Partitura, pk=pk)
    if not (partitura.owner_id == request.user.id or _es_admin(request.user)):
        return HttpResponseForbidden()
    if partitura.estado_normalizacion == "pendiente":
        return redirect("partituras:detalle", pk=pk)  # todavía no se corrió "Enderezar PDF"
    pagina = get_object_or_404(Pagina, partitura=partitura, numero=numero)
    total = partitura.paginas.count()

    if request.method == "POST":
        accion = request.POST.get("accion")
        if accion == "rotar_izq":
            pagina.rotacion_aplicada = (pagina.rotacion_aplicada - 90) % 360
            pagina.save(update_fields=["rotacion_aplicada"])
            _invalidar_cache_imagen_normalizada(pk, numero)
        elif accion == "rotar_der":
            pagina.rotacion_aplicada = (pagina.rotacion_aplicada + 90) % 360
            pagina.save(update_fields=["rotacion_aplicada"])
            _invalidar_cache_imagen_normalizada(pk, numero)
        elif accion == "ajustar_angulo":
            try:
                pagina.angulo_deskew_aplicado = float(request.POST.get("angulo", 0))
            except ValueError:
                pass
            pagina.save(update_fields=["angulo_deskew_aplicado"])
            _invalidar_cache_imagen_normalizada(pk, numero)
        elif accion == "confirmar":
            if pagina.confirmada:
                # Ya estaba confirmada: esto es un rehacer, no la primera
                # vez — todo lo de abajo (márgenes, sistemas, ancla, barras)
                # está calculado sobre la imagen vieja y ya no vale.
                invalidar_desde_orientacion(pagina)
            pagina.confirmada = True
            pagina.save(update_fields=["confirmada"])
            siguiente = partitura.paginas.filter(confirmada=False).order_by("numero").first()
            if siguiente:
                return redirect("partituras:ajuste_orientacion", pk=pk, numero=siguiente.numero)
            partitura.estado_normalizacion = "confirmada"
            partitura.save(update_fields=["estado_normalizacion"])
            return redirect("partituras:ajuste_margenes", pk=pk, numero=1)
        return redirect("partituras:ajuste_orientacion", pk=pk, numero=numero)

    return render(request, "partituras/ajuste_orientacion.html", {
        "partitura": partitura,
        "pagina": pagina,
        "total": total,
    })


# ── Márgenes (recuadro de contenido real) ──────────────────────────────────

def _detectar_margenes_pagina(pagina):
    """Corre detectar_margenes y aplica el resultado a esta página. Usado
    tanto al entrar por primera vez a esta etapa (auto-detección) como por
    "volver a detectar de cero" desde la propia pantalla."""
    img = rasterizar_pagina(pagina.partitura.archivo_original.path, pagina.numero, dpi=DPI)
    normalizada = normalizar_pagina(img, pagina.rotacion_aplicada, pagina.angulo_deskew_aplicado)
    m = detectar_margenes(normalizada)
    pagina.margen_x0_detectado = pagina.margen_x0_aplicado = m['x0']
    pagina.margen_y0_detectado = pagina.margen_y0_aplicado = m['y0']
    pagina.margen_x1_detectado = pagina.margen_x1_aplicado = m['x1']
    pagina.margen_y1_detectado = pagina.margen_y1_aplicado = m['y1']
    pagina.margen_confirmado = False
    pagina.save(update_fields=[
        "margen_x0_detectado", "margen_y0_detectado", "margen_x1_detectado", "margen_y1_detectado",
        "margen_x0_aplicado", "margen_y0_aplicado", "margen_x1_aplicado", "margen_y1_aplicado",
        "margen_confirmado",
    ])


@login_required
def ajuste_margenes(request, pk, numero):
    partitura = get_object_or_404(Partitura, pk=pk)
    if not (partitura.owner_id == request.user.id or _es_admin(request.user)):
        return HttpResponseForbidden()
    if partitura.estado_normalizacion != "confirmada":
        return redirect("partituras:detalle", pk=pk)  # falta terminar orientación
    pagina = get_object_or_404(Pagina, partitura=partitura, numero=numero)
    total = partitura.paginas.count()

    if request.method == "POST":
        accion = request.POST.get("accion")

        if accion == "redetectar":
            _detectar_margenes_pagina(pagina)
            return redirect("partituras:ajuste_margenes", pk=pk, numero=numero)

        if accion == "ignorar":
            pagina.ignorada = True
            pagina.margen_confirmado = False
            pagina.save(update_fields=["ignorada", "margen_confirmado"])
            return _siguiente_pagina(partitura, pk, "ajuste_margenes", numero, "margen_confirmado")

        try:
            x0 = float(request.POST["x0"]); y0 = float(request.POST["y0"])
            x1 = float(request.POST["x1"]); y1 = float(request.POST["y1"])
        except (KeyError, ValueError):
            return HttpResponseBadRequest("Rectángulo inválido")

        pagina.margen_x0_aplicado, pagina.margen_y0_aplicado = x0, y0
        pagina.margen_x1_aplicado, pagina.margen_y1_aplicado = x1, y1

        if accion == "confirmar":
            if pagina.margen_confirmado:
                # Rehacer: lo que hubiera de sistemas/ancla/barras para acá
                # se detectó sobre el margen viejo, ya no corresponde.
                invalidar_desde_margenes(pagina)
            pagina.margen_confirmado = True
            pagina.save(update_fields=[
                "margen_x0_aplicado", "margen_y0_aplicado", "margen_x1_aplicado", "margen_y1_aplicado",
                "margen_confirmado",
            ])
            return _siguiente_pagina(partitura, pk, "ajuste_margenes", numero, "margen_confirmado")

        pagina.save(update_fields=["margen_x0_aplicado", "margen_y0_aplicado", "margen_x1_aplicado", "margen_y1_aplicado"])
        return redirect("partituras:ajuste_margenes", pk=pk, numero=numero)

    if not pagina.ignorada and not pagina.tiene_margen_detectado:
        _detectar_margenes_pagina(pagina)

    return render(request, "partituras/ajuste_margenes.html", {
        "partitura": partitura,
        "pagina": pagina,
        "total": total,
    })


# ── Detección de sistemas ──────────────────────────────────────────────────

def _detectar_sistemas_pagina(partitura, pagina):
    """Corre detectar_sistemas y reemplaza los Sistema existentes de la
    página. Usado tanto por la detección masiva como por "volver a detectar
    de cero" desde la propia pantalla de ajuste."""
    # Detecta sobre la imagen recortada a márgenes, no la página completa
    # sin recortar — un artefacto de escaneo (p.ej. una franja oscura de
    # encuadernación) puede contaminar el perfil de densidad por fila y
    # arruinar la segmentación en sistemas (confirmado: en un caso real
    # esto hacía que detectar_sistemas no encontrara NINGÚN sistema).
    normalizada, recortada, (offset_x, offset_y) = _pagina_normalizada_recortada(partitura, pagina)
    h, w = normalizada.shape[:2]
    umbral_separacion = pagina.umbral_separacion_sistemas
    if umbral_separacion is None:
        umbral_separacion = UMBRAL_SEPARACION_SISTEMAS_DEFAULT
    sistemas = detectar_sistemas(recortada, umbral_frac=umbral_separacion)

    pagina.sistemas.all().delete()
    Sistema.objects.bulk_create([
        Sistema(
            pagina=pagina, orden=i,
            y=(s["y0"] + offset_y) / h, height=(s["y1"] - s["y0"]) / h,
            origen="auto", confirmado=False,
        )
        for i, s in enumerate(sistemas)
    ])


@login_required
def ajuste_sistemas(request, pk, numero):
    partitura = get_object_or_404(Partitura, pk=pk)
    if not (partitura.owner_id == request.user.id or _es_admin(request.user)):
        return HttpResponseForbidden()
    if not partitura.margenes_completos:
        return redirect("partituras:detalle", pk=pk)  # falta terminar márgenes
    pagina = get_object_or_404(Pagina, partitura=partitura, numero=numero)
    total = partitura.paginas.count()

    if request.method == "POST":
        accion = request.POST.get("accion", "confirmar")
        ya_estaba_confirmada = pagina.sistemas_confirmados  # antes de tocar nada

        if accion == "redetectar":
            umbral_pct_raw = request.POST.get("umbral_separacion_pct")
            if umbral_pct_raw:
                try:
                    pagina.umbral_separacion_sistemas = max(0.0005, min(0.5, float(umbral_pct_raw) / 100))
                    pagina.save(update_fields=["umbral_separacion_sistemas"])
                except ValueError:
                    pass
            # Ignora lo que haya (confirmado o no) y vuelve a correr
            # detectar_sistemas de cero.
            _detectar_sistemas_pagina(partitura, pagina)
            if ya_estaba_confirmada:
                invalidar_desde_sistemas(pagina)
            return redirect("partituras:ajuste_sistemas", pk=pk, numero=numero)

        try:
            datos = json.loads(request.POST.get("sistemas", "[]"))
        except (json.JSONDecodeError, ValueError):
            return HttpResponseBadRequest("JSON inválido")

        ids_enviados = [d["id"] for d in datos if d.get("id")]
        pagina.sistemas.exclude(id__in=ids_enviados).delete()

        for orden, d in enumerate(sorted(datos, key=lambda d: d["y"])):
            if d.get("id"):
                Sistema.objects.filter(id=d["id"], pagina=pagina).update(
                    y=d["y"], height=d["height"], orden=orden, confirmado=True,
                )
            else:
                Sistema.objects.create(
                    pagina=pagina, orden=orden, y=d["y"], height=d["height"],
                    origen="manual", confirmado=True,
                )

        if ya_estaba_confirmada:
            # Rehacer: el ancla y las barras/compases de esta página se
            # ubicaron relativos a los sistemas viejos, ya no valen.
            invalidar_desde_sistemas(pagina)

        pendiente = partitura.paginas.filter(
            ignorada=False, sistemas__confirmado=False,
        ).order_by("numero").first()
        if pendiente:
            return redirect("partituras:ajuste_sistemas", pk=pk, numero=pendiente.numero)

        partitura.estado_analisis = "confirmado"
        partitura.save(update_fields=["estado_analisis"])
        return redirect("partituras:ajuste_ancla", pk=pk, numero=1)

    if not pagina.ignorada and not pagina.tiene_sistemas:
        _detectar_sistemas_pagina(partitura, pagina)

    sistemas = list(pagina.sistemas.order_by("orden").values("id", "y", "height"))
    umbral_separacion_actual = pagina.umbral_separacion_sistemas
    if umbral_separacion_actual is None:
        umbral_separacion_actual = UMBRAL_SEPARACION_SISTEMAS_DEFAULT
    return render(request, "partituras/ajuste_sistemas.html", {
        "partitura": partitura,
        "pagina": pagina,
        "total": total,
        "sistemas_json": json.dumps(sistemas),
        "umbral_separacion_pct": round(umbral_separacion_actual * 100, 2),
    })


# ── Ancla (barra de compás de referencia) ──────────────────────────────────

_PADDING_ANCLA_X = 30
_PADDING_ANCLA_Y = 15


def _pagina_normalizada_recortada(partitura, pagina):
    """Imagen normalizada (rotación+desalineado) y recortada a márgenes reales — la
    detección de sistemas/barras/ancla necesita esto para no confundirse con
    artefactos de escaneo (ver nota en vision.MARGEN_X_FRAC). Usa los márgenes
    ya confirmados por el usuario si existen; si todavía no se confirmaron
    para esta página, los detecta al vuelo (comportamiento previo a la
    pantalla de ajuste de márgenes)."""
    img = rasterizar_pagina(partitura.archivo_original.path, pagina.numero, dpi=DPI)
    normalizada = normalizar_pagina(img, pagina.rotacion_aplicada, pagina.angulo_deskew_aplicado)
    h, w = normalizada.shape[:2]
    if pagina.margen_confirmado:
        m = {
            'x0': pagina.margen_x0_aplicado, 'y0': pagina.margen_y0_aplicado,
            'x1': pagina.margen_x1_aplicado, 'y1': pagina.margen_y1_aplicado,
        }
    else:
        m = detectar_margenes(normalizada)
    x0, y0 = int(m['x0'] * w), int(m['y0'] * h)
    x1, y1 = int(m['x1'] * w), int(m['y1'] * h)
    return normalizada, normalizada[y0:y1, x0:x1], (x0, y0)


def _guardar_ancla(pagina, w, h, x0, y0, x1, y1, linea):
    """Guarda el rectángulo (con relleno) y, si se encontró, la línea exacta detectada."""
    pagina.ancla_x0 = (x0 - _PADDING_ANCLA_X) / w
    pagina.ancla_x1 = (x1 + _PADDING_ANCLA_X) / w
    pagina.ancla_y0 = (y0 - _PADDING_ANCLA_Y) / h
    pagina.ancla_y1 = (y1 + _PADDING_ANCLA_Y) / h
    if linea:
        pagina.ancla_linea_x = linea['x'] / w
        pagina.ancla_linea_y0 = linea['y0'] / h
        pagina.ancla_linea_y1 = linea['y1'] / h
    else:
        pagina.ancla_linea_x = pagina.ancla_linea_y0 = pagina.ancla_linea_y1 = None


def _detectar_ancla_pagina(partitura, pagina):
    """Corre encontrar_ancla y aplica el resultado a esta página (si
    encontró algo — si no, no toca los campos, y la plantilla ya sabe
    mostrar un rectángulo por defecto razonable para que el usuario lo
    ubique a mano). Usado tanto al entrar por primera vez a esta etapa como
    por "volver a detectar de cero"."""
    normalizada, recortada, (offset_x, offset_y) = _pagina_normalizada_recortada(partitura, pagina)
    h, w = normalizada.shape[:2]
    # Los Sistema de esta página ya están confirmados a esta altura (ver
    # ajuste_ancla, exige partitura.sistemas_completos) — no tiene sentido
    # que encontrar_ancla vuelva a correr detectar_sistemas de cero e
    # ignore la corrección del usuario. Sistema.y/height son relativos a la
    # página normalizada COMPLETA (ver _detectar_sistemas_pagina); acá hace
    # falta convertirlos a píxeles de la imagen recortada que usa encontrar_ancla.
    sistemas_confirmados = [
        {"y0": int(s.y * h - offset_y), "y1": int((s.y + s.height) * h - offset_y)}
        for s in pagina.sistemas.order_by("orden")
    ]
    ancla = encontrar_ancla(recortada, sistemas=sistemas_confirmados)
    if ancla:
        x = ancla['x'] + offset_x
        y0 = ancla['y0'] + offset_y
        y1 = ancla['y1'] + offset_y
        _guardar_ancla(pagina, w, h, x, y0, x, y1, {'x': x, 'y0': y0, 'y1': y1})
    pagina.ancla_confirmada = False
    pagina.save(update_fields=[
        "ancla_x0", "ancla_x1", "ancla_y0", "ancla_y1",
        "ancla_linea_x", "ancla_linea_y0", "ancla_linea_y1", "ancla_confirmada",
    ])


@login_required
def ajuste_ancla(request, pk, numero):
    partitura = get_object_or_404(Partitura, pk=pk)
    if not (partitura.owner_id == request.user.id or _es_admin(request.user)):
        return HttpResponseForbidden()
    if not partitura.sistemas_completos:
        return redirect("partituras:detalle", pk=pk)  # falta terminar sistemas
    pagina = get_object_or_404(Pagina, partitura=partitura, numero=numero)
    total = partitura.paginas.count()

    if request.method == "POST":
        accion = request.POST.get("accion")
        ya_estaba_confirmada = pagina.ancla_confirmada  # antes de tocar nada

        if accion == "ignorar":
            pagina.ignorada = True
            pagina.ancla_confirmada = False
            pagina.save(update_fields=["ignorada", "ancla_confirmada"])
            return _siguiente_pagina(partitura, pk, "ajuste_ancla", numero, "ancla_confirmada")

        if accion == "redetectar":
            # Ignora el rectángulo actual y confirmado/no-confirmado: vuelve a
            # correr encontrar_ancla de cero, como la primera vez.
            _detectar_ancla_pagina(partitura, pagina)
            if ya_estaba_confirmada:
                invalidar_desde_ancla(pagina)
            return redirect("partituras:ajuste_ancla", pk=pk, numero=numero)

        try:
            rx0 = float(request.POST["x0"]); ry0 = float(request.POST["y0"])
            rx1 = float(request.POST["x1"]); ry1 = float(request.POST["y1"])
        except (KeyError, ValueError):
            return HttpResponseBadRequest("Rectángulo inválido")

        if accion == "buscar":
            normalizada, _, _ = _pagina_normalizada_recortada(partitura, pagina)
            h, w = normalizada.shape[:2]
            y_centro = (ry0 * h + ry1 * h) / 2
            # Sistema.y/height son relativos a esta misma página normalizada
            # completa (ver _detectar_sistemas_pagina) — usar los ya
            # confirmados en vez de re-detectar de cero, mismo criterio que
            # _detectar_ancla_pagina (ver V1.6.6).
            sistema_px = next(
                (
                    {"y0": s.y * h, "y1": (s.y + s.height) * h}
                    for s in pagina.sistemas.order_by("orden")
                    if s.y * h <= y_centro <= (s.y + s.height) * h
                ),
                None,
            )
            refinado = buscar_barra_en_rectangulo(
                normalizada, rx0 * w, ry0 * h, rx1 * w, ry1 * h, sistema_px=sistema_px,
            )
            if refinado:
                _guardar_ancla(pagina, w, h, refinado['x'], refinado['y0'], refinado['x'], refinado['y1'], refinado)
            else:
                pagina.ancla_x0, pagina.ancla_y0, pagina.ancla_x1, pagina.ancla_y1 = rx0, ry0, rx1, ry1
                pagina.ancla_linea_x = pagina.ancla_linea_y0 = pagina.ancla_linea_y1 = None
            if ya_estaba_confirmada:
                # Mismo motivo que en "confirmar"/"redetectar": las barras de
                # esta página se detectaron con la referencia de escala del
                # ancla vieja — bug real (2026-07-31): "Buscar" desconfirmaba
                # el ancla pero se olvidaba de invalidar lo que cuelga de
                # ella, dejando barras_confirmadas/compases_confirmados en
                # True (con Compas/Barra viejos) contra un ancla ya distinta.
                invalidar_desde_ancla(pagina)
            pagina.ancla_confirmada = False  # volver a buscar implica que lo confirmado anterior ya no vale tal cual
            pagina.save(update_fields=[
                "ancla_x0", "ancla_y0", "ancla_x1", "ancla_y1",
                "ancla_linea_x", "ancla_linea_y0", "ancla_linea_y1", "ancla_confirmada",
            ])
            return redirect("partituras:ajuste_ancla", pk=pk, numero=numero)

        if accion == "confirmar":
            # Guarda exactamente lo que el usuario tiene en pantalla — sin
            # volver a buscar. Si quiere una línea refinada primero, usa
            # "Buscar"; confirmar no debería mover nada por su cuenta.
            if ya_estaba_confirmada:
                # Rehacer: las barras de esta página se detectaron con la
                # referencia de escala del ancla vieja, ya no valen.
                invalidar_desde_ancla(pagina)
            pagina.ancla_x0, pagina.ancla_y0, pagina.ancla_x1, pagina.ancla_y1 = rx0, ry0, rx1, ry1
            lx = request.POST.get("linea_x", "")
            ly0 = request.POST.get("linea_y0", "")
            ly1 = request.POST.get("linea_y1", "")
            if lx and ly0 and ly1:
                pagina.ancla_linea_x, pagina.ancla_linea_y0, pagina.ancla_linea_y1 = float(lx), float(ly0), float(ly1)
            else:
                pagina.ancla_linea_x = pagina.ancla_linea_y0 = pagina.ancla_linea_y1 = None
            pagina.ancla_confirmada = True
            pagina.save(update_fields=[
                "ancla_x0", "ancla_y0", "ancla_x1", "ancla_y1",
                "ancla_linea_x", "ancla_linea_y0", "ancla_linea_y1", "ancla_confirmada",
            ])
            return _siguiente_pagina(partitura, pk, "ajuste_ancla", numero, "ancla_confirmada")

        return redirect("partituras:ajuste_ancla", pk=pk, numero=numero)

    if not pagina.ignorada and not pagina.tiene_ancla_detectada:
        _detectar_ancla_pagina(partitura, pagina)

    return render(request, "partituras/ajuste_ancla.html", {
        "partitura": partitura,
        "pagina": pagina,
        "total": total,
    })


# ── Barras de compás (aceptadas y dudosas) ──────────────────────────────────

def _detectar_barras_pagina(partitura, pagina):
    """Corre detectar_barras_candidatas por sistema (usando el alto de la
    ancla confirmada como referencia) y reemplaza las Barra existentes de
    cada sistema de la página por las recién detectadas."""
    normalizada, recortada, (offset_x, offset_y) = _pagina_normalizada_recortada(partitura, pagina)
    h, w = normalizada.shape[:2]
    rh, rw = recortada.shape[:2]
    alto_referencia = (pagina.ancla_linea_y1 - pagina.ancla_linea_y0) * h
    umbral_contenido = pagina.umbral_contenido_sistema
    if umbral_contenido is None:
        umbral_contenido = UMBRAL_CONTENIDO_SISTEMA_DEFAULT

    for sistema in pagina.sistemas.order_by("orden"):
        # sistema.y/height son relativos a la página normalizada COMPLETA
        # (así se guardaron en iniciar_deteccion_sistemas) — hay que restar
        # el offset del recorte de márgenes para ubicarlos en el sistema de
        # coordenadas de `recortada`, que es lo que espera detectar_barras_candidatas.
        sy0 = max(0, int(sistema.y * h) - offset_y)
        sy1 = min(rh, int((sistema.y + sistema.height) * h) - offset_y)
        sistema.barras.all().delete()
        if sy1 <= sy0:
            continue
        candidatas = detectar_barras_candidatas(recortada, {'y0': sy0, 'y1': sy1}, alto_referencia=alto_referencia)

        # Borde real de FIN de contenido de este sistema — el límite real
        # del último compás. Si la última barra candidata cae ahí mismo (a
        # una distancia comparable a la que separa los dos trazos de una
        # barra doble — ver UMBRAL_RELATIVO_BARRA_DOBLE/_fusionar_barras_
        # dobles en vision.py — no hace falta que sea doble, el criterio es
        # sólo de distancia), esa barra no marca la división con un compás
        # siguiente real (no hay ninguno en este sistema) — es redundante
        # con contenido_x1 y se excluye de las Barra guardadas, para no
        # dejar un dato editable que no representa nada real. contenido_x1
        # pasa a ser SIEMPRE el límite del último compás (antes se guardaba
        # None cuando coincidía con una barra real, dejando que esa barra
        # hiciera de límite — eso es lo que se cambia acá).
        fin_col = detectar_borde_fin_sistema(recortada, {'y0': sy0, 'y1': sy1}, alto_referencia=alto_referencia, umbral_frac=umbral_contenido)
        xs_candidatas = [c['x'] for c in candidatas]
        separaciones = [b - a for a, b in zip(xs_candidatas, xs_candidatas[1:])]
        tipica = float(np.median(separaciones)) if len(separaciones) >= 1 else None
        tolerancia_px = tipica * UMBRAL_RELATIVO_BARRA_DOBLE if tipica else max(3, (alto_referencia or (sy1 - sy0)) * 0.05)
        ultima_aceptada_col = max((c['x'] for c in candidatas if c['aceptada']), default=None)
        ya_es_barra = (
            fin_col is not None and ultima_aceptada_col is not None
            and abs(fin_col - ultima_aceptada_col) < tolerancia_px
        )
        if ya_es_barra:
            candidatas = [c for c in candidatas if not (c['aceptada'] and c['x'] == ultima_aceptada_col)]

        Barra.objects.bulk_create([
            Barra(
                sistema=sistema,
                x=(c['x'] + offset_x) / w,
                estado='aceptada' if c['aceptada'] else 'dudosa',
                origen='auto',
            )
            for c in candidatas
        ])

        # Borde real de contenido de este sistema (clave/armadura) — usado
        # por el JS de ajuste_barras.html como arranque del primer compás,
        # en vez del placeholder x=0 (no hay ninguna barra a la izquierda
        # del primer compás contra la cual medir). Mismo offset_x/w que la
        # conversión de Barra.x de arriba, mismo sistema de coordenadas.
        borde_col = detectar_borde_contenido_sistema(recortada, {'y0': sy0, 'y1': sy1}, alto_referencia=alto_referencia, umbral_frac=umbral_contenido)
        sistema.contenido_x0 = (borde_col + offset_x) / w if borde_col is not None else None
        sistema.contenido_x1 = (fin_col + offset_x) / w if fin_col is not None else None

        sistema.save(update_fields=['contenido_x0', 'contenido_x1'])


@login_required
def ajuste_barras(request, pk, numero):
    """Pantalla fusionada: ajustar barras (aceptadas/dudosas, agregar/borrar)
    Y numerar los compases que resultan de ellas, en un solo lugar — separarlas
    obligaba a ir y volver cada vez que numerar hacía notar un error de barra."""
    partitura = get_object_or_404(Partitura, pk=pk)
    if not (partitura.owner_id == request.user.id or _es_admin(request.user)):
        return HttpResponseForbidden()
    if not partitura.ancla_completa:
        return redirect("partituras:detalle", pk=pk)  # falta terminar el ancla
    pagina = get_object_or_404(Pagina, partitura=partitura, numero=numero)
    total = partitura.paginas.count()

    if request.method == "POST":
        accion = request.POST.get("accion")

        if accion == "ignorar":
            pagina.ignorada = True
            pagina.barras_confirmadas = False
            pagina.compases_confirmados = False
            pagina.save(update_fields=["ignorada", "barras_confirmadas", "compases_confirmados"])
            Compas.objects.filter(sistema__pagina=pagina).delete()
            return _siguiente_pagina(partitura, pk, "ajuste_barras", numero, "barras_confirmadas")

        if accion == "redetectar":
            if pagina.ancla_confirmada and pagina.sistemas_confirmados:
                umbral_pct_raw = request.POST.get("umbral_contenido_pct")
                if umbral_pct_raw:
                    try:
                        pagina.umbral_contenido_sistema = max(0.0005, min(0.5, float(umbral_pct_raw) / 100))
                        pagina.save(update_fields=["umbral_contenido_sistema"])
                    except ValueError:
                        pass
                _detectar_barras_pagina(partitura, pagina)
                pagina.barras_confirmadas = False
                pagina.save(update_fields=["barras_confirmadas"])
                # Las barras acaban de cambiar — los compases que hubiera
                # (y su posible confirmación) ya no corresponden a nada real,
                # se borran en vez de dejarlos colgados y desactualizados.
                Compas.objects.filter(sistema__pagina=pagina).delete()
                if pagina.compases_confirmados:
                    pagina.compases_confirmados = False
                    pagina.save(update_fields=["compases_confirmados"])
            return redirect("partituras:ajuste_barras", pk=pk, numero=numero)

        try:
            datos = json.loads(request.POST.get("barras", "[]"))
        except (json.JSONDecodeError, ValueError):
            return HttpResponseBadRequest("JSON inválido")

        ids_enviados = [d["id"] for d in datos if d.get("id")]
        Barra.objects.filter(sistema__pagina=pagina).exclude(id__in=ids_enviados).delete()

        for d in datos:
            if d.get("id"):
                Barra.objects.filter(id=d["id"], sistema__pagina=pagina).update(
                    x=d["x"], estado=d["estado"],
                )
            else:
                sistema = pagina.sistemas.filter(id=d.get("sistema_id")).first()
                if sistema:
                    Barra.objects.create(sistema=sistema, x=d["x"], estado=d["estado"], origen="manual")

        try:
            datos_bordes = json.loads(request.POST.get("bordes_sistema", "[]"))
        except (json.JSONDecodeError, ValueError):
            return HttpResponseBadRequest("JSON de bordes de sistema inválido")
        for d in datos_bordes:
            Sistema.objects.filter(id=d.get("id"), pagina=pagina).update(
                contenido_x0=d.get("contenido_x0"), contenido_x1=d.get("contenido_x1"),
            )

        if accion == "confirmar":
            try:
                datos_compases = json.loads(request.POST.get("compases", "[]"))
            except (json.JSONDecodeError, ValueError):
                return HttpResponseBadRequest("JSON de compases inválido")
            pagina.barras_confirmadas = True
            pagina.save(update_fields=["barras_confirmadas"])
            guardar_compases_pagina(pagina, datos_compases)
            pagina.compases_confirmados = True
            pagina.save(update_fields=["compases_confirmados"])
            return _siguiente_pagina(partitura, pk, "ajuste_barras", numero, "barras_confirmadas")

        return redirect("partituras:ajuste_barras", pk=pk, numero=numero)

    if not pagina.ignorada and not pagina.tiene_barras_detectadas:
        _detectar_barras_pagina(partitura, pagina)

    sistemas = list(pagina.sistemas.order_by("orden").values("id", "y", "height", "contenido_x0", "contenido_x1"))
    barras = list(
        Barra.objects.filter(sistema__pagina=pagina)
        .order_by("sistema__orden", "x")
        .values("id", "sistema_id", "x", "estado", "origen")
    )
    compases = list(
        Compas.objects.filter(sistema__pagina=pagina)
        .order_by("sistema__orden", "x")
        .values("id", "sistema_id", "x", "y", "width", "height", "numero", "repeticiones")
    )
    umbral_actual = pagina.umbral_contenido_sistema
    if umbral_actual is None:
        umbral_actual = UMBRAL_CONTENIDO_SISTEMA_DEFAULT
    numero_inicial = numero_inicial_pagina(pagina)
    # Umbral de la próxima pausa entre movimientos (ver EfectoTempo tipo
    # 'pausa'), si hay una a partir de acá — frontera inamovible que
    # difundirNumeros() no puede cruzar al renumerar (ver esa función y
    # guardar_compases_pagina, mismo criterio del lado del servidor).
    umbral_pausa = None
    if partitura.obra_id is not None:
        umbral_pausa = next(
            (p["compas_desde"] for p in indice_pausas(partitura.obra) if p["compas_desde"] >= numero_inicial),
            None,
        )
    return render(request, "partituras/ajuste_barras.html", {
        "partitura": partitura,
        "pagina": pagina,
        "total": total,
        "sistemas_json": json.dumps(sistemas),
        "barras_json": json.dumps(barras),
        "compases_json": json.dumps(compases),
        "numero_inicial": numero_inicial,
        "umbral_pausa": umbral_pausa,
        "umbral_contenido_pct": round(umbral_actual * 100, 1),
    })
