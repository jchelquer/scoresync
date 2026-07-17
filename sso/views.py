from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.core import signing
from django.shortcuts import redirect

User = get_user_model()


def token_login(request):
    token = request.GET.get('token', '')
    next_url = request.GET.get('next', '/')
    try:
        data = signing.loads(token, key=settings.SSO_SECRET, salt='sso', max_age=300)
        username = data['u']
        user = User.objects.get(username=username)
        user.backend = 'django.contrib.auth.backends.ModelBackend'
        login(request, user)
    except Exception:
        pass
    return redirect(next_url)
