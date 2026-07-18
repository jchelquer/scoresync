from django import forms
from .models import Partitura


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
