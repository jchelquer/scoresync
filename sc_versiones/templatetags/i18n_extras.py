from django import template
from django.utils.html import escapejs
from django.utils.translation import gettext

register = template.Library()


@register.simple_tag
def trans_js(text):
    """
    Traduce `text` y lo escapa para insertarlo dentro de un string JS
    (comillas simples/dobles/backticks) embebido en el template. A diferencia
    de {% translate %}, blindado contra apóstrofes u otros caracteres que
    rompan la sintaxis del <script> — el string traducido puede contener
    cualquier cosa (comillas, barras invertidas) sin romper el JS.
    """
    return escapejs(gettext(text))
