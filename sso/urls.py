from django.urls import path
from . import views

app_name = 'sso'

urlpatterns = [
    path('token-login/', views.token_login, name='token_login'),
]
