from django.conf import settings
from django.shortcuts import redirect
from django.utils import translation

from .sso import terminos_redirect_url

# Rutas que nunca deben quedar bloqueadas por TerminosMiddleware: login/
# logout/recuperar contraseña, el propio SSO, el admin y estáticos. Sin esto,
# una cuenta sin aceptar no podría ni deslogearse.
RUTAS_EXENTAS_TERMINOS = ('/usuarios/', '/sso/', '/admin/', '/accounts/', '/static/', '/media/')


class IdiomaUsuarioMiddleware:
    """
    Para usuarios logueados, el idioma guardado en el propio Usuario manda por
    sobre la cookie/sesión (que LocaleMiddleware ya activó más arriba en la
    cadena): así viaja con la cuenta entre sesiones y dispositivos, en vez de
    depender de la cookie del navegador.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            idioma = request.user.idioma
            if idioma:
                translation.activate(idioma)
                request.LANGUAGE_CODE = translation.get_language()
        return self.get_response(request)


class TerminosMiddleware:
    """
    Si la cuenta logueada no aceptó la versión vigente de los Términos y
    Condiciones / Política de Privacidad (settings.TERMINOS_VERSION), la
    manda a infedu.com.ar/aceptar-terminos/ antes de dejarla seguir. Al
    aceptar, infedu-jch la redirige de vuelta a la URL original: la sesión de
    esta app nunca se toca, así que no hace falta volver a loguearse acá.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if (
            user.is_authenticated
            and user.terminos_version != settings.TERMINOS_VERSION
            and not request.path.startswith(RUTAS_EXENTAS_TERMINOS)
        ):
            return redirect(terminos_redirect_url(user.username, request.get_full_path()))
        return self.get_response(request)
