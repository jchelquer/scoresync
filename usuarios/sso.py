from django.conf import settings
from django.core import signing
from urllib.parse import urlencode

APPS = {
    'afinacion': 'https://afinacion.infedu.com.ar' if settings.IS_PRODUCTION else 'http://localhost:8001',
    'tempo':     'https://tempo.infedu.com.ar' if settings.IS_PRODUCTION else 'http://localhost:8002',
    'ensayos':   'https://ensayos.infedu.com.ar' if settings.IS_PRODUCTION else 'http://localhost:8004',
}

APP_KEY = 'scoresync'
INFEDU_TERMINOS_URL = (
    'https://infedu.com.ar/aceptar-terminos/'
    if settings.IS_PRODUCTION
    else 'http://localhost:8003/aceptar-terminos/'
)


def sso_url(username, app, next_path='/'):
    """Genera una URL de token-login SSO para la app destino."""
    base = APPS.get(app, '')
    if not base or not settings.SSO_SECRET:
        return base + next_path
    token = signing.dumps({'u': username}, key=settings.SSO_SECRET, salt='sso')
    params = urlencode({'token': token, 'next': next_path})
    return f'{base}/sso/token-login/?{params}'


def terminos_redirect_url(username, next_path='/'):
    """
    URL a infedu.com.ar donde el usuario lee y acepta los Términos y la
    Política de Privacidad vigentes (usada por usuarios.middleware.
    TerminosMiddleware cuando Usuario.terminos_version no coincide con
    settings.TERMINOS_VERSION). El token no autentica nada acá: solo le dice
    a infedu-jch qué cuenta y a qué URL de esta app volver una vez aceptado.
    La sesión de esta app no se toca, así que no hace falta un login SSO de
    vuelta.
    """
    if not settings.SSO_SECRET:
        return INFEDU_TERMINOS_URL
    token = signing.dumps(
        {'u': username, 'app': APP_KEY, 'next': next_path},
        key=settings.SSO_SECRET, salt='terminos',
    )
    return f'{INFEDU_TERMINOS_URL}?{urlencode({"token": token})}'
