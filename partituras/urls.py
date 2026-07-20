from django.urls import path
from . import views

app_name = 'partituras'

urlpatterns = [
    path('', views.biblioteca, name='biblioteca'),
    path('subir/', views.subir, name='subir'),
    path('<int:pk>/', views.detalle, name='detalle'),
    path('<int:pk>/estado/', views.estado, name='estado'),
    path('<int:pk>/obra/', views.gestionar_obra, name='gestionar_obra'),

    path('obras/', views.obras, name='obras'),
    path('obras/nueva/', views.crear_obra, name='crear_obra'),
    path('obras/<int:pk>/', views.obra_detalle, name='obra_detalle'),
    path('obras/<int:pk>/adjuntar/', views.adjuntar_a_obra, name='adjuntar_a_obra'),
    path('obras/<int:pk>/itinerario/', views.itinerario_obra, name='itinerario_obra'),
    path('obras/<int:pk>/navegador/', views.navegador_obra, name='navegador_obra'),
    path('obras/<int:pk>/plan/', views.plan_obra, name='plan_obra'),

    path('<int:pk>/normalizar/', views.iniciar_normalizacion, name='iniciar_normalizacion'),
    path('<int:pk>/orientacion/<int:numero>/', views.ajuste_orientacion, name='ajuste_orientacion'),
    path('<int:pk>/orientacion/<int:numero>/imagen.png', views.pagina_imagen_normalizada, name='pagina_imagen_normalizada'),

    path('<int:pk>/margenes/<int:numero>/', views.ajuste_margenes, name='ajuste_margenes'),
    path('<int:pk>/sistemas/<int:numero>/', views.ajuste_sistemas, name='ajuste_sistemas'),
    path('<int:pk>/ancla/<int:numero>/', views.ajuste_ancla, name='ajuste_ancla'),
    path('<int:pk>/barras/<int:numero>/', views.ajuste_barras, name='ajuste_barras'),
]
