import json

import cv2
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render

from .forms import PartituraForm
from .models import Barra, Compas, Pagina, Partitura, Sistema
from .normalizacion import detectar_angulo_deskew, detectar_rotacion_90, normalizar_pagina
from .pdf import contar_paginas, generar_pdf_normalizado, rasterizar_pagina
from .services import (
    guardar_compases_pagina, invalidar_desde_ancla, invalidar_desde_margenes,
    invalidar_desde_orientacion, invalidar_desde_sistemas, numero_inicial_pagina,
)
from .vision import (
    buscar_barra_en_rectangulo, detectar_barras_candidatas, detectar_margenes,
    detectar_sistemas, encontrar_ancla,
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
def biblioteca(request):
    partituras = Partitura.objects.filter(owner=request.user)
    return render(request, "partituras/biblioteca.html", {"partituras": partituras})


@login_required
def subir(request):
    if request.method == "POST":
        form = PartituraForm(request.POST, request.FILES)
        if form.is_valid():
            partitura = form.save(commit=False)
            partitura.owner = request.user
            partitura.save()
            return redirect("partituras:detalle", pk=partitura.pk)
    else:
        initial = {}
        if request.user.instrumento_principal_id:
            initial["instrumento"] = request.user.instrumento_principal_id
        form = PartituraForm(initial=initial)
    return render(request, "partituras/subir.html", {"form": form})


def _contexto_estado(partitura):
    return {
        "partitura": partitura,
        "pagina_margenes": _primera_pendiente(partitura, "margen_confirmado"),
        "pagina_sistemas": _primera_pendiente_sistemas(partitura),
        "pagina_ancla": _primera_pendiente(partitura, "ancla_confirmada"),
        "pagina_barras": _primera_pendiente(partitura, "barras_confirmadas"),
    }


@login_required
def detalle(request, pk):
    """Punto de entrada "inteligente": si hay algo pendiente, te lleva
    directo ahí — nunca hace falta pasar por el menú a propósito. Si no hay
    nada pendiente (o todavía no arrancó nada), muestra el panel de estado."""
    partitura = get_object_or_404(Partitura, pk=pk, owner=request.user)
    paso = _proximo_paso(partitura)
    if paso:
        url_name, numero = paso
        return redirect(f"partituras:{url_name}", pk=pk, numero=numero)
    return render(request, "partituras/detalle.html", _contexto_estado(partitura))


@login_required
def estado(request, pk):
    """El mismo panel que `detalle`, pero sin el salto automático — para
    volver a ver el estado general a propósito (el botón "Salir" de cada
    etapa apunta acá, no a `detalle`, para no rebotar de vuelta a la misma
    pantalla que se acaba de dejar)."""
    partitura = get_object_or_404(Partitura, pk=pk, owner=request.user)
    return render(request, "partituras/detalle.html", _contexto_estado(partitura))


# ── Normalización: rotación + desalineado fino ────────────────────────────

@login_required
def iniciar_normalizacion(request, pk):
    partitura = get_object_or_404(Partitura, pk=pk, owner=request.user)
    if request.method != "POST":
        return redirect("partituras:detalle", pk=pk)

    total = contar_paginas(partitura.archivo_original.path)
    ya_confirmadas = set(
        partitura.paginas.filter(confirmada=True).values_list("numero", flat=True)
    )
    for numero in range(1, total + 1):
        if numero in ya_confirmadas:
            continue  # no pisar una página que el usuario ya revisó y confirmó
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

    partitura.estado_normalizacion = "propuesta"
    partitura.save(update_fields=["estado_normalizacion"])
    return redirect("partituras:ajuste_orientacion", pk=pk, numero=1)


@login_required
def pagina_imagen_normalizada(request, pk, numero):
    """PNG de la página con la rotación/desalineado PROPUESTOS (o ya confirmados) aplicados."""
    partitura = get_object_or_404(Partitura, pk=pk, owner=request.user)
    pagina = get_object_or_404(Pagina, partitura=partitura, numero=numero)
    img = rasterizar_pagina(partitura.archivo_original.path, numero, dpi=DPI)
    corregida = normalizar_pagina(img, pagina.rotacion_aplicada, pagina.angulo_deskew_aplicado)
    ok, buf = cv2.imencode(".png", corregida)
    if not ok:
        return HttpResponseBadRequest("No se pudo generar la imagen")
    response = HttpResponse(buf.tobytes(), content_type="image/png")
    response["Cache-Control"] = "no-store"
    return response


@login_required
def ajuste_orientacion(request, pk, numero):
    partitura = get_object_or_404(Partitura, pk=pk, owner=request.user)
    if partitura.estado_normalizacion == "pendiente":
        return redirect("partituras:detalle", pk=pk)  # todavía no se corrió "Enderezar PDF"
    pagina = get_object_or_404(Pagina, partitura=partitura, numero=numero)
    total = partitura.paginas.count()

    if request.method == "POST":
        accion = request.POST.get("accion")
        if accion == "rotar_izq":
            pagina.rotacion_aplicada = (pagina.rotacion_aplicada - 90) % 360
            pagina.save(update_fields=["rotacion_aplicada"])
        elif accion == "rotar_der":
            pagina.rotacion_aplicada = (pagina.rotacion_aplicada + 90) % 360
            pagina.save(update_fields=["rotacion_aplicada"])
        elif accion == "ajustar_angulo":
            try:
                pagina.angulo_deskew_aplicado = float(request.POST.get("angulo", 0))
            except ValueError:
                pass
            pagina.save(update_fields=["angulo_deskew_aplicado"])
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
            _generar_pdf_normalizado(partitura)
            return redirect("partituras:ajuste_margenes", pk=pk, numero=1)
        return redirect("partituras:ajuste_orientacion", pk=pk, numero=numero)

    return render(request, "partituras/ajuste_orientacion.html", {
        "partitura": partitura,
        "pagina": pagina,
        "total": total,
    })


def _generar_pdf_normalizado(partitura):
    paginas_bgr = []
    for pagina in partitura.paginas.order_by("numero"):
        img = rasterizar_pagina(partitura.archivo_original.path, pagina.numero, dpi=DPI)
        paginas_bgr.append(normalizar_pagina(img, pagina.rotacion_aplicada, pagina.angulo_deskew_aplicado))
    pdf_bytes = generar_pdf_normalizado(paginas_bgr)
    partitura.archivo_normalizado.save(
        f"{partitura.pk}_normalizado.pdf",
        ContentFile(pdf_bytes),
        save=False,
    )
    partitura.estado_normalizacion = "confirmada"
    partitura.save(update_fields=["archivo_normalizado", "estado_normalizacion"])


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
    partitura = get_object_or_404(Partitura, pk=pk, owner=request.user)
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
    sistemas = detectar_sistemas(recortada)

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
    partitura = get_object_or_404(Partitura, pk=pk, owner=request.user)
    if not partitura.margenes_completos:
        return redirect("partituras:detalle", pk=pk)  # falta terminar márgenes
    pagina = get_object_or_404(Pagina, partitura=partitura, numero=numero)
    total = partitura.paginas.count()

    if request.method == "POST":
        accion = request.POST.get("accion", "confirmar")
        ya_estaba_confirmada = pagina.sistemas_confirmados  # antes de tocar nada

        if accion == "redetectar":
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
    return render(request, "partituras/ajuste_sistemas.html", {
        "partitura": partitura,
        "pagina": pagina,
        "total": total,
        "sistemas_json": json.dumps(sistemas),
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
    ancla = encontrar_ancla(recortada)
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
    partitura = get_object_or_404(Partitura, pk=pk, owner=request.user)
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
            sistema_px = next(
                (s for s in detectar_sistemas(normalizada) if s['y0'] <= y_centro <= s['y1']),
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
        Barra.objects.bulk_create([
            Barra(
                sistema=sistema,
                x=(c['x'] + offset_x) / w,
                estado='aceptada' if c['aceptada'] else 'dudosa',
                origen='auto',
            )
            for c in candidatas
        ])


@login_required
def ajuste_barras(request, pk, numero):
    """Pantalla fusionada: ajustar barras (aceptadas/dudosas, agregar/borrar)
    Y numerar los compases que resultan de ellas, en un solo lugar — separarlas
    obligaba a ir y volver cada vez que numerar hacía notar un error de barra."""
    partitura = get_object_or_404(Partitura, pk=pk, owner=request.user)
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

    sistemas = list(pagina.sistemas.order_by("orden").values("id", "y", "height"))
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
    return render(request, "partituras/ajuste_barras.html", {
        "partitura": partitura,
        "pagina": pagina,
        "total": total,
        "sistemas_json": json.dumps(sistemas),
        "barras_json": json.dumps(barras),
        "compases_json": json.dumps(compases),
        "numero_inicial": numero_inicial_pagina(pagina),
    })
