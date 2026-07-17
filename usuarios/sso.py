from django.conf import settings
from django.core import signing
from urllib.parse import urlencode

APPS = {
    'afinacion': 'https://afinacion.infedu.com.ar',
    'tempo':     'https://tempo.infedu.com.ar',
    'ensayos':   'https://ensayos.infedu.com.ar',
}


def sso_url(username, app, next_path='/'):
    """Genera una URL de token-login SSO para la app destino."""
    base = APPS.get(app, '')
    if not base or not settings.SSO_SECRET:
        return base + next_path
    token = signing.dumps({'u': username}, key=settings.SSO_SECRET, salt='sso')
    params = urlencode({'token': token, 'next': next_path})
    return f'{base}/sso/token-login/?{params}'
