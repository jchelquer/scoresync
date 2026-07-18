from django.urls import path
from . import views

app_name = 'partituras'

urlpatterns = [
    path('', views.biblioteca, name='biblioteca'),
    path('subir/', views.subir, name='subir'),
    path('<int:pk>/', views.detalle, name='detalle'),
    path('<int:pk>/estado/', views.estado, name='estado'),

    path('<int:pk>/normalizar/', views.iniciar_normalizacion, name='iniciar_normalizacion'),
    path('<int:pk>/orientacion/<int:numero>/', views.ajuste_orientacion, name='ajuste_orientacion'),
    path('<int:pk>/orientacion/<int:numero>/imagen.png', views.pagina_imagen_normalizada, name='pagina_imagen_normalizada'),

    path('<int:pk>/margenes/<int:numero>/', views.ajuste_margenes, name='ajuste_margenes'),
    path('<int:pk>/sistemas/<int:numero>/', views.ajuste_sistemas, name='ajuste_sistemas'),
    path('<int:pk>/ancla/<int:numero>/', views.ajuste_ancla, name='ajuste_ancla'),
    path('<int:pk>/barras/<int:numero>/', views.ajuste_barras, name='ajuste_barras'),
]
