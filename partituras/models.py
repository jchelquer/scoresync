import uuid
from django.conf import settings
from django.db import models


def _upload_path_original(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "pdf"
    return f"partituras/u{instance.owner_id}/{uuid.uuid4().hex}_original.{ext}"


def _upload_path_normalizado(instance, filename):
    return f"partituras/u{instance.owner_id}/{uuid.uuid4().hex}_normalizado.pdf"


class Partitura(models.Model):
    ESTADOS_NORM = [
        ('pendiente', 'Pendiente'),
        ('propuesta', 'Propuesta'),
        ('confirmada', 'Confirmada'),
    ]
    ESTADOS_ANALISIS = [
        ('pendiente', 'Pendiente'),
        ('propuesto', 'Propuesto'),
        ('confirmado', 'Confirmado'),
    ]

    titulo = models.CharField(max_length=200)
    compositor = models.CharField(max_length=200, blank=True)
    instrumento = models.ForeignKey(
        'actividades.Instrumento',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        db_constraint=False,
        related_name='partituras',
    )
    parte = models.CharField(max_length=100, blank=True, help_text="Ej: Clarinete 2")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='partituras',
    )
    archivo_original = models.FileField(upload_to=_upload_path_original)
    archivo_normalizado = models.FileField(upload_to=_upload_path_normalizado, null=True, blank=True)
    estado_normalizacion = models.CharField(max_length=12, choices=ESTADOS_NORM, default='pendiente')
    estado_analisis = models.CharField(max_length=12, choices=ESTADOS_ANALISIS, default='pendiente')
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-creado']
        verbose_name = 'Partitura'
        verbose_name_plural = 'Partituras'

    def __str__(self):
        return f"{self.titulo} ({self.parte})" if self.parte else self.titulo


class Pagina(models.Model):
    partitura = models.ForeignKey(Partitura, related_name='paginas', on_delete=models.CASCADE)
    numero = models.PositiveIntegerField()
    rotacion_detectada = models.IntegerField(default=0)
    rotacion_aplicada = models.IntegerField(default=0)
    angulo_deskew_detectado = models.FloatField(default=0)
    angulo_deskew_aplicado = models.FloatField(default=0)
    # Recuadro de contenido real (0-1), excluyendo artefactos de escaneo (ver vision.detectar_margenes)
    margen_x0_detectado = models.FloatField(default=0.0)
    margen_y0_detectado = models.FloatField(default=0.0)
    margen_x1_detectado = models.FloatField(default=1.0)
    margen_y1_detectado = models.FloatField(default=1.0)
    margen_x0_aplicado = models.FloatField(default=0.0)
    margen_y0_aplicado = models.FloatField(default=0.0)
    margen_x1_aplicado = models.FloatField(default=1.0)
    margen_y1_aplicado = models.FloatField(default=1.0)
    margen_confirmado = models.BooleanField(default=False)
    confirmada = models.BooleanField(default=False)

    # Rectángulo del ancla (0-1, relativo a la página normalizada+recortada a
    # márgenes): región donde se encontró (o el usuario ubicó) una barra de
    # compás confiable, usada como referencia de tamaño/posición para buscar
    # el resto — ver vision.encontrar_ancla / buscar_barra_en_rectangulo.
    ancla_x0 = models.FloatField(null=True, blank=True)
    ancla_y0 = models.FloatField(null=True, blank=True)
    ancla_x1 = models.FloatField(null=True, blank=True)
    ancla_y1 = models.FloatField(null=True, blank=True)
    # Posición exacta de la barra encontrada DENTRO del rectángulo (no el
    # rectángulo en sí) — para poder mostrarle al usuario una línea fina que
    # debería superponerse con la barra real, y así pueda juzgar si la
    # detección fue precisa o no. None si la búsqueda no encontró nada.
    ancla_linea_x = models.FloatField(null=True, blank=True)
    ancla_linea_y0 = models.FloatField(null=True, blank=True)
    ancla_linea_y1 = models.FloatField(null=True, blank=True)
    ancla_confirmada = models.BooleanField(default=False)
    barras_confirmadas = models.BooleanField(default=False)
    compases_confirmados = models.BooleanField(default=False)
    ignorada = models.BooleanField(default=False, help_text="Página en blanco, portada, etc. — se excluye del análisis.")

    class Meta:
        unique_together = [('partitura', 'numero')]
        ordering = ['numero']
        verbose_name = 'Página'
        verbose_name_plural = 'Páginas'

    def __str__(self):
        return f"{self.partitura} — pág. {self.numero}"

    @property
    def sistemas_confirmados(self):
        return self.sistemas.exists() and not self.sistemas.filter(confirmado=False).exists()

    @property
    def tiene_sistemas(self):
        return self.sistemas.exists()

    @property
    def tiene_margen_detectado(self):
        """False sólo si los 4 campos siguen en el default de fábrica (0,0,1,1)
        y nunca se confirmó — o sea, 'iniciar_deteccion_margenes' nunca corrió
        para esta página. No 100% infalible (un margen detectado que coincida
        exactamente con el default de fábrica se vería igual), pero es una
        aproximación razonable para distinguir "nunca corrido" de "corrido
        pero no confirmado" en la tabla de estado."""
        return self.margen_confirmado or not (
            self.margen_x0_aplicado == 0.0 and self.margen_y0_aplicado == 0.0
            and self.margen_x1_aplicado == 1.0 and self.margen_y1_aplicado == 1.0
        )

    @property
    def tiene_ancla_detectada(self):
        return self.ancla_x0 is not None

    @property
    def tiene_barras_detectadas(self):
        return Barra.objects.filter(sistema__pagina=self).exists()


class Sistema(models.Model):
    ORIGENES = [('auto', 'Automático'), ('manual', 'Manual')]

    pagina = models.ForeignKey(Pagina, related_name='sistemas', on_delete=models.CASCADE)
    orden = models.PositiveIntegerField()
    y = models.FloatField(help_text="Posición vertical relativa (0-1) del borde superior")
    height = models.FloatField(help_text="Alto relativo (0-1)")
    origen = models.CharField(max_length=10, choices=ORIGENES, default='auto')
    confirmado = models.BooleanField(default=False)

    class Meta:
        ordering = ['pagina__numero', 'orden']
        verbose_name = 'Sistema'
        verbose_name_plural = 'Sistemas'

    def __str__(self):
        return f"{self.pagina} — sistema {self.orden}"


class Barra(models.Model):
    """Posición horizontal de una barra de compás candidata dentro de un
    sistema — precursor liviano de Compas (que todavía requiere pares de
    barras consecutivas para construirse, etapa futura). Una candidata que
    cruza el pentagrama pero no pasa el filtro de ancho queda 'dudosa' en
    vez de descartarse directamente, para que el usuario la revise."""
    ESTADOS = [('dudosa', 'Dudosa'), ('aceptada', 'Aceptada')]
    ORIGENES = [('auto', 'Automático'), ('manual', 'Manual')]

    sistema = models.ForeignKey(Sistema, related_name='barras', on_delete=models.CASCADE)
    x = models.FloatField(help_text="Posición horizontal relativa (0-1) de la página")
    estado = models.CharField(max_length=10, choices=ESTADOS, default='dudosa')
    origen = models.CharField(max_length=10, choices=ORIGENES, default='auto')

    class Meta:
        ordering = ['sistema__pagina__numero', 'sistema__orden', 'x']
        verbose_name = 'Barra'
        verbose_name_plural = 'Barras'

    def __str__(self):
        return f"{self.sistema} — x={self.x:.3f} ({self.estado})"


class Compas(models.Model):
    ORIGENES = [('auto', 'Automático'), ('manual', 'Manual')]

    sistema = models.ForeignKey(Sistema, related_name='compases', on_delete=models.CASCADE)
    numero = models.PositiveIntegerField()
    x = models.FloatField()
    y = models.FloatField()
    width = models.FloatField()
    height = models.FloatField()
    origen = models.CharField(max_length=10, choices=ORIGENES, default='auto')
    confirmado = models.BooleanField(default=False)

    class Meta:
        ordering = ['sistema__pagina__numero', 'sistema__orden', 'x']
        verbose_name = 'Compás'
        verbose_name_plural = 'Compases'

    def __str__(self):
        return f"Compás {self.numero} — {self.sistema}"
