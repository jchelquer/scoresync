from django.urls import path
from . import views

app_name = 'versiones'

urlpatterns = [
    path('novedades/', views.novedades_json, name='novedades'),
    path('comentario/', views.enviar_comentario, name='comentario'),
]
