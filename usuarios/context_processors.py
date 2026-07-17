from .models import SolicitudAcceso
from .sso import sso_url


def solicitudes_pendientes(request):
    if request.user.is_authenticated and request.user.rol == 'admin':
        count = SolicitudAcceso.objects.filter(estado=SolicitudAcceso.PENDIENTE).count()
        return {'solicitudes_pendientes': count}
    return {'solicitudes_pendientes': 0}


def sso_links(request):
    if not request.user.is_authenticated:
        return {'SSO_AFINACION': '', 'SSO_TEMPO': '', 'SSO_ENSAYOS': ''}
    username = request.user.username
    return {
        'SSO_AFINACION': sso_url(username, 'afinacion'),
        'SSO_TEMPO':     sso_url(username, 'tempo'),
        'SSO_ENSAYOS':   sso_url(username, 'ensayos'),
    }
