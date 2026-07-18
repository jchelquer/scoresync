from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import TemplateView
from django.views.i18n import JavaScriptCatalog

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
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
