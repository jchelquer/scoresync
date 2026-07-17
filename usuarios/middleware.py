from django.utils import translation


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
