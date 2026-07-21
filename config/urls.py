import mimetypes
import os
import re

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.http import FileResponse, HttpResponse, HttpResponseNotFound
from django.utils._os import safe_join
from django.views.generic import TemplateView
from django.views.i18n import JavaScriptCatalog

_RANGO_RE = re.compile(r"bytes=(\d*)-(\d*)$")


def _servir_media_con_rango(request, path, document_root=None, show_indexes=False):
    """Como django.views.static.serve (sólo para DEBUG, ver más abajo), pero
    soportando Range requests (RFC 7233) — django.views.static.serve nunca
    los soportó (limitación conocida: está pensada sólo para desarrollo, en
    producción un servidor de verdad se encarga). Sin esto, el <audio> del
    navegador no puede buscar/seekear de forma confiable dentro del archivo
    — currentTime se ignora y la reproducción arranca siempre desde 0."""
    fullpath = safe_join(document_root, path)
    if not os.path.isfile(fullpath):
        return HttpResponseNotFound()

    content_type, _ = mimetypes.guess_type(fullpath)
    content_type = content_type or "application/octet-stream"
    tamano = os.path.getsize(fullpath)

    coincidencia = _RANGO_RE.match(request.META.get("HTTP_RANGE", ""))
    if not coincidencia:
        response = FileResponse(open(fullpath, "rb"), content_type=content_type)
        response["Accept-Ranges"] = "bytes"
        return response

    inicio = int(coincidencia.group(1)) if coincidencia.group(1) else 0
    fin = min(int(coincidencia.group(2)), tamano - 1) if coincidencia.group(2) else tamano - 1
    largo = max(fin - inicio + 1, 0)

    with open(fullpath, "rb") as f:
        f.seek(inicio)
        cuerpo = f.read(largo)
    response = HttpResponse(cuerpo, status=206, content_type=content_type)
    response["Content-Range"] = f"bytes {inicio}-{fin}/{tamano}"
    response["Accept-Ranges"] = "bytes"
    response["Content-Length"] = str(largo)
    return response

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", login_required(TemplateView.as_view(template_name="home.html")), name="home"),
    path("jsi18n/", JavaScriptCatalog.as_view(domain="django"), name="javascript-catalog"),
    path("usuarios/", include("usuarios.urls")),
    path("partituras/", include("partituras.urls", namespace="partituras")),
    path("versiones/", include("sc_versiones.urls", namespace="versiones")),
    path("sso/", include("sso.urls", namespace="sso")),
    path("accounts/password-reset/", auth_views.PasswordResetView.as_view(
        template_name="registration/password_reset.html",
    ), name="password_reset"),
    path("accounts/password-reset/enviado/", auth_views.PasswordResetDoneView.as_view(
        template_name="registration/password_reset_done.html",
    ), name="password_reset_done"),
    path("accounts/reset/<uidb64>/<token>/", auth_views.PasswordResetConfirmView.as_view(
        template_name="registration/password_reset_confirm.html",
    ), name="password_reset_confirm"),
    path("accounts/reset/completado/", auth_views.PasswordResetCompleteView.as_view(
        template_name="registration/password_reset_complete.html",
    ), name="password_reset_complete"),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT, view=_servir_media_con_rango)
