from django import forms
from .models import Obra, Partitura, Segmento
from .services import parsear_compas_pulso, validar_indicacion_compas


class PartituraForm(forms.ModelForm):
    class Meta:
        model = Partitura
        fields = ['titulo', 'compositor', 'instrumento', 'parte', 'archivo_original']
        widgets = {
            'titulo': forms.TextInput(attrs={'class': 'form-control'}),
            'compositor': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Opcional'}),
            'instrumento': forms.Select(attrs={'class': 'form-select'}),
            'parte': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Ej: Clarinete 2 (opcional)'}),
            'archivo_original': forms.ClearableFileInput(attrs={'class': 'form-control', 'accept': 'application/pdf'}),
        }


class ObraForm(forms.ModelForm):
    class Meta:
        model = Obra
        fields = ['titulo', 'compositor', 'arreglista']
        widgets = {
            'titulo': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Título de la obra'}),
            'compositor': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Opcional'}),
            'arreglista': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Arreglista (opcional)'}),
        }


class SegmentoForm(forms.ModelForm):
    class Meta:
        model = Segmento
        fields = [
            'orden', 'desde_texto', 'hasta_texto',
            'indicacion_compas', 'variacion_tempo', 'bpm', 'bpm_llegada',
            'descripcion', 'tiempo_inicio',
        ]
        widgets = {
            'orden': forms.NumberInput(attrs={'class': 'form-control form-control-sm', 'style': 'width: 4.5rem;'}),
            'desde_texto': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'style': 'width: 6rem;', 'placeholder': 'compás[,pulso]'}),
            'hasta_texto': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'style': 'width: 6rem;', 'placeholder': 'compás[,pulso]'}),
            'indicacion_compas': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'style': 'width: 4.5rem;', 'placeholder': '(hereda)'}),
            'variacion_tempo': forms.Select(attrs={'class': 'form-select form-select-sm', 'style': 'width: 8rem;'}),
            'bpm': forms.NumberInput(attrs={'class': 'form-control form-control-sm', 'style': 'width: 4.5rem;'}),
            'bpm_llegada': forms.NumberInput(attrs={'class': 'form-control form-control-sm', 'style': 'width: 4.5rem;', 'placeholder': '(llegada)'}),
            'descripcion': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'style': 'min-width: 10rem;'}),
            'tiempo_inicio': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'style': 'width: 8rem;', 'placeholder': 'hh:mm:ss'}),
        }

    def clean_indicacion_compas(self):
        texto = self.cleaned_data.get('indicacion_compas', '')
        try:
            return validar_indicacion_compas(texto)
        except ValueError as e:
            raise forms.ValidationError(str(e))

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('DELETE'):
            return cleaned

        try:
            compas_desde, pulso_desde = parsear_compas_pulso(cleaned.get('desde_texto', ''), pulso_default=1)
        except ValueError:
            self.add_error('desde_texto', 'Formato inválido — usá "compás" o "compás,pulso" (ej: 4 o 4,1.5).')
        else:
            self.instance.compas_desde = compas_desde
            self.instance.pulso_desde = pulso_desde

        try:
            compas_hasta, pulso_hasta = parsear_compas_pulso(cleaned.get('hasta_texto', ''), pulso_default=None)
        except ValueError:
            self.add_error('hasta_texto', 'Formato inválido — usá "compás" o "compás,pulso" (ej: 20 o 20,3).')
        else:
            self.instance.compas_hasta = compas_hasta
            self.instance.pulso_hasta = pulso_hasta

        return cleaned


SegmentoFormSet = forms.modelformset_factory(
    Segmento, form=SegmentoForm, extra=3, can_delete=True,
)
