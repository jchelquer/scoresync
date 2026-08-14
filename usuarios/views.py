from django.contrib.auth.views import LoginView, LogoutView, PasswordChangeView
from django.contrib.messages import success
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils.translation import gettext as _
from django.views.i18n import set_language as django_set_language
from django.views.decorators.http import require_POST

from .forms import ScoreSyncAuthForm
from .models import Usuario
from . import sso


class ScoreSyncLoginView(LoginView):
    template_name = "usuarios/login.html"
    authentication_form = ScoreSyncAuthForm


class ScoreSyncLogoutView(LogoutView):
    pass


class CambiarPasswordView(PasswordChangeView):
    template_name = "usuarios/cambiar_password.html"
    success_url = reverse_lazy("login")

    def form_valid(self, form):
        success(self.request, _("Contraseña actualizada correctamente."))
        return super().form_valid(form)


@require_POST
def cambiar_idioma(request):
    """
    Delega en la vista estándar de Django (cookie + sesión) y, si hay un
    usuario logueado, además persiste la elección en el propio Usuario para
    que viaje con la cuenta entre sesiones/dispositivos y no dependa solo de
    la cookie del navegador.
    """
    response = django_set_language(request)
    idioma = request.POST.get('language')
    if request.user.is_authenticated and idioma in dict(Usuario.IDIOMAS):
        if request.user.idioma != idioma:
            request.user.idioma = idioma
            request.user.save(update_fields=['idioma'])
    return response


def solicitar_acceso(request):
    """
    El pedido de acceso se gestiona en un único lugar (ensayos), para no
    mantener el mismo formulario duplicado en cada app hermana.
    """
    return redirect(f"{sso.APPS['ensayos']}/usuarios/solicitar-acceso/?programa={sso.APP_KEY}")
