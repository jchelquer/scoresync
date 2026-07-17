from django.urls import path
from . import views

urlpatterns = [
    path("login/", views.ScoreSyncLoginView.as_view(), name="login"),
    path("logout/", views.ScoreSyncLogoutView.as_view(), name="logout"),
    path("solicitar-acceso/", views.solicitar_acceso, name="solicitar_acceso"),
    path("cambiar-password/", views.CambiarPasswordView.as_view(), name="cambiar_password"),
    path("idioma/", views.cambiar_idioma, name="cambiar_idioma"),
]
