import json
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from .models import Comentario
from .changelog import get_changelog, get_version_actual


@login_required
def novedades_json(request):
    return JsonResponse({'changelog': get_changelog(), 'version_actual': get_version_actual()})


@login_required
@require_POST
def enviar_comentario(request):
    puede = getattr(request.user, 'suscripcion', '') == 'beta' or getattr(request.user, 'es_admin', False)
    if not puede:
        return JsonResponse({'ok': False, 'error': 'Sin permiso.'}, status=403)

    texto = request.POST.get('texto', '').strip()
    if not texto:
        return JsonResponse({'ok': False, 'error': 'El comentario no puede estar vacío.'}, status=400)

    tipo = request.POST.get('tipo', '')
    try:
        metadata = json.loads(request.POST.get('metadata', '{}'))
    except (json.JSONDecodeError, ValueError):
        metadata = {}

    Comentario.objects.create(
        usuario=request.user,
        version=get_version_actual(),
        tipo=tipo,
        texto=texto,
        metadata=metadata,
    )
    return JsonResponse({'ok': True})
