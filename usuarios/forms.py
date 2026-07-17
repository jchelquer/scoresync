from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.utils.translation import gettext_lazy as _
from .models import SolicitudAcceso


_INPUT = "form-control"


class ScoreSyncAuthForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", _INPUT)


class SolicitudAccesoForm(forms.ModelForm):
    class Meta:
        model = SolicitudAcceso
        fields = ['nombre', 'apellido', 'email', 'celular', 'instrumento', 'mensaje']
        widgets = {
            'nombre':      forms.TextInput(attrs={'class': _INPUT}),
            'apellido':    forms.TextInput(attrs={'class': _INPUT}),
            'email':       forms.EmailInput(attrs={'class': _INPUT}),
            'celular':     forms.TextInput(attrs={'class': _INPUT, 'placeholder': _('Opcional')}),
            'instrumento': forms.TextInput(attrs={'class': _INPUT, 'placeholder': _('Opcional')}),
            'mensaje':     forms.Textarea(attrs={'class': _INPUT, 'rows': 3,
                                                 'placeholder': _('Contanos brevemente para qué lo vas a usar (opcional)')}),
        }
