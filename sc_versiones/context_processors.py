from django.utils import timezone
from .changelog import get_changelog, get_version_actual, get_novedades_desde


def versiones(request):
    version_actual = get_version_actual()
    ctx = {
        'VERSION_ACTUAL': version_actual,
        'CHANGELOG': get_changelog(),
        'NOVEDADES_VERSIONES': set(),
        'MOSTRAR_NOVEDADES': False,
        'PUEDE_COMENTAR': False,
    }

    if not request.user.is_authenticated:
        return ctx

    ctx['PUEDE_COMENTAR'] = (
        getattr(request.user, 'suscripcion', '') == 'beta'
        or getattr(request.user, 'es_admin', False)
    )

    from .models import PerfilVersiones
    perfil, _ = PerfilVersiones.objects.get_or_create(usuario=request.user)

    if perfil.ultima_version_vista != version_actual:
        novedades = get_novedades_desde(perfil.ultima_version_vista)
        if novedades:
            ctx['NOVEDADES_VERSIONES'] = {e['version'] for e in novedades}
            ctx['MOSTRAR_NOVEDADES'] = True
        perfil.ultima_version_vista = version_actual

    perfil.ultimo_acceso = timezone.now()
    perfil.save(update_fields=['ultima_version_vista', 'ultimo_acceso'])

    return ctx
