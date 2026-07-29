from django import forms
from .models import EfectoTempo, MarcaNotacion, Obra, Partitura, Segmento
from .services import normalizar_armadura, parsear_compas_pulso, validar_indicacion_compas


class PartituraForm(forms.ModelForm):
    """Cargar una parte nueva — siempre dentro de una obra (ver views.subir),
    así que el título no se pide acá: se toma directo de la obra. El orden
    de 'fields' es el orden de renderizado del form: el archivo va
    inmediatamente después del título (mostrado aparte, de sólo lectura, en
    el template) — ver subir.html."""
    class Meta:
        model = Partitura
        fields = ['archivo_original', 'instrumento', 'parte']
        widgets = {
            'instrumento': forms.Select(attrs={'class': 'form-select'}),
            'parte': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Ej: Clarinete 2 (opcional; si se deja vacío se usa el instrumento)'}),
            'archivo_original': forms.ClearableFileInput(attrs={'class': 'form-control', 'accept': 'application/pdf'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['instrumento'].required = True


class PartituraEditForm(forms.ModelForm):
    """Igual que PartituraForm pero sin archivo_original — cambiar el PDF
    de una partitura ya en uso invalidaría todas las páginas/sistemas/
    barras/compases ya detectados y confirmados; esto es sólo para
    corregir el nombre/metadatos. El título sólo se deja editable para
    partes sueltas (ver editar_partitura, que deshabilita el campo si la
    parte ya pertenece a una obra — ahí el título es el de la obra)."""
    class Meta:
        model = Partitura
        fields = ['titulo', 'instrumento', 'parte']
        widgets = {
            'titulo': forms.TextInput(attrs={'class': 'form-control'}),
            'instrumento': forms.Select(attrs={'class': 'form-select'}),
            'parte': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Ej: Clarinete 2 (opcional; si se deja vacío se usa el instrumento)'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['instrumento'].required = True


class ObraForm(forms.ModelForm):
    class Meta:
        model = Obra
        fields = ['titulo', 'compositor', 'arreglista']
        widgets = {
            'titulo': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Título de la obra'}),
            'compositor': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Opcional'}),
            'arreglista': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Arreglista (opcional)'}),
        }


class ObraEditForm(forms.ModelForm):
    """Corrige los datos de una obra ya creada — título/compositor/
    arreglista/ciclo (organización de biblioteca, ver Obra.ciclo). No el
    audio (ver obra_detalle, que lo maneja aparte) ni publicada (switch
    propio, ver alternar_publicacion_obra). Repertorio/Ciclo en sí se
    gestionan sólo desde el admin — acá sólo se elige entre los que ya
    existen."""
    class Meta:
        model = Obra
        fields = ['titulo', 'compositor', 'arreglista', 'ciclo']
        widgets = {
            'titulo': forms.TextInput(attrs={'class': 'form-control'}),
            'compositor': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Opcional'}),
            'arreglista': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Opcional'}),
            'ciclo': forms.Select(attrs={'class': 'form-select'}),
        }


class SegmentoForm(forms.ModelForm):
    class Meta:
        model = Segmento
        fields = [
            'orden', 'desde_texto', 'hasta_texto',
            'descripcion',
        ]
        widgets = {
            'orden': forms.NumberInput(attrs={'class': 'form-control form-control-sm', 'style': 'width: 4.5rem;'}),
            'desde_texto': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'style': 'width: 6rem;', 'placeholder': 'compás[,pulso]'}),
            'hasta_texto': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'style': 'width: 6rem;', 'placeholder': 'compás[,pulso]'}),
            'descripcion': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'style': 'min-width: 10rem;'}),
        }

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


class MarcaNotacionForm(forms.ModelForm):
    class Meta:
        model = MarcaNotacion
        fields = ['tipo', 'compas', 'pasada', 'valor']
        widgets = {
            'tipo': forms.Select(attrs={'class': 'form-select form-select-sm', 'style': 'width: 9rem;'}),
            'compas': forms.NumberInput(attrs={'class': 'form-control form-control-sm', 'style': 'width: 5rem;'}),
            'pasada': forms.NumberInput(attrs={'class': 'form-control form-control-sm', 'style': 'width: 5rem;', 'placeholder': '(todas)'}),
            'valor': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'style': 'width: 8rem;'}),
        }

    def clean_valor(self):
        tipo = self.cleaned_data.get('tipo')
        valor = self.cleaned_data.get('valor', '')
        if tipo == 'compas':
            try:
                valor = validar_indicacion_compas(valor)
            except ValueError as e:
                raise forms.ValidationError(str(e))
            if not valor:
                raise forms.ValidationError('La indicación de compás no puede quedar vacía acá — completá "N/D" (ej: 4/4).')
        elif tipo == 'tempo':
            try:
                bpm = int(valor)
                if bpm <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise forms.ValidationError('El tempo tiene que ser un número entero de bpm mayor que 0.')
        elif tipo == 'armadura':
            try:
                valor = normalizar_armadura(valor)
            except ValueError as e:
                raise forms.ValidationError(str(e))
        return valor


class MarcaNotacionBaseFormSet(forms.BaseModelFormSet):
    """Valida acá (no sólo con la restricción de la base) que no haya dos
    filas del mismo envío pisándose: dos marcas GENERALES (pasada vacía)
    para el mismo (tipo, compás) — la restricción de unicidad condicional
    de MarcaNotacion no la detecta Model.validate_unique() (Django no valida
    UniqueConstraint con condition a nivel Python, sólo a nivel de base) así
    que sin este chequeo el error saldría como un IntegrityError feo en vez
    de un mensaje por fila."""

    def clean(self):
        super().clean()
        vistos = {}
        for form in self.forms:
            if not hasattr(form, 'cleaned_data') or form.cleaned_data.get('DELETE'):
                continue
            tipo = form.cleaned_data.get('tipo')
            compas = form.cleaned_data.get('compas')
            pasada = form.cleaned_data.get('pasada')
            if tipo is None or compas is None:
                continue
            clave = (tipo, compas, pasada)
            if clave in vistos:
                form.add_error('compas', 'Ya hay otra fila con el mismo tipo/compás' + (f'/pasada {pasada}' if pasada else ' (marca general)') + ' en este envío.')
            else:
                vistos[clave] = form


MarcaNotacionFormSet = forms.modelformset_factory(
    MarcaNotacion, form=MarcaNotacionForm, formset=MarcaNotacionBaseFormSet, extra=3, can_delete=True,
)


class EfectoTempoForm(forms.ModelForm):
    class Meta:
        model = EfectoTempo
        fields = ['tipo', 'desde_texto', 'hasta_texto', 'valor']
        widgets = {
            'tipo': forms.Select(attrs={'class': 'form-select form-select-sm', 'style': 'width: 9rem;'}),
            'desde_texto': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'style': 'width: 6rem;', 'placeholder': 'compás[,pulso]'}),
            'hasta_texto': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'style': 'width: 6rem;', 'placeholder': 'compás[,pulso]'}),
            'valor': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'style': 'width: 8rem;'}),
        }

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('DELETE'):
            return cleaned

        tipo = cleaned.get('tipo')

        try:
            compas_desde, pulso_desde = parsear_compas_pulso(cleaned.get('desde_texto', ''), pulso_default=1)
        except ValueError:
            compas_desde = None
            self.add_error('desde_texto', 'Formato inválido — usá "compás" o "compás,pulso" (ej: 10 o 10,2.5).')
        else:
            if compas_desde is None:
                self.add_error('desde_texto', 'Hace falta indicar dónde arranca el efecto.')
            self.instance.compas_desde = compas_desde
            self.instance.pulso_desde = pulso_desde

        hasta_texto = cleaned.get('hasta_texto', '')
        if tipo == 'calderon':
            if hasta_texto:
                self.add_error('hasta_texto', 'Un calderón es un punto — dejá "Hasta" vacío.')
            self.instance.compas_hasta = None
            self.instance.pulso_hasta = None
        else:
            try:
                compas_hasta, pulso_hasta = parsear_compas_pulso(hasta_texto, pulso_default=None)
            except ValueError:
                compas_hasta = None
                self.add_error('hasta_texto', 'Formato inválido — usá "compás" o "compás,pulso" (ej: 20 o 20,3).')
            else:
                if compas_hasta is None:
                    self.add_error('hasta_texto', 'Un accelerando/ritardando necesita dónde termina.')
                self.instance.compas_hasta = compas_hasta
                self.instance.pulso_hasta = pulso_hasta

        return cleaned

    def clean_valor(self):
        tipo = self.cleaned_data.get('tipo')
        valor = self.cleaned_data.get('valor', '')
        if tipo == 'calderon':
            try:
                factor = float(valor)
                if factor <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise forms.ValidationError('El factor de duración del calderón tiene que ser un número mayor que 0 (ej: 1.5).')
        else:
            try:
                bpm = int(valor)
                if bpm <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise forms.ValidationError('El bpm de llegada tiene que ser un número entero mayor que 0.')
        return valor


EfectoTempoFormSet = forms.modelformset_factory(
    EfectoTempo, form=EfectoTempoForm, extra=3, can_delete=True,
)
