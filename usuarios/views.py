from django.contrib.auth.views import LoginView, LogoutView, PasswordChangeView
from django.contrib.messages import success
from django.shortcuts import render
from django.urls import reverse_lazy
from django.utils.translation import gettext as _
from django.views.i18n import set_language as django_set_language
from django.views.decorators.http import require_POST

from .forms import SolicitudAccesoForm, ScoreSyncAuthForm
from .models import Usuario


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
    if request.method == "POST":
        form = SolicitudAccesoForm(request.POST)
        if form.is_valid():
            form.save()
            return render(request, "usuarios/solicitar_acceso_ok.html")
    else:
        form = SolicitudAccesoForm()
    return render(request, "usuarios/solicitar_acceso.html", {"form": form})
