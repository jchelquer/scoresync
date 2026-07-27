import uuid
from django.conf import settings
from django.db import models


def _upload_path_original(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "pdf"
    return f"partituras/u{instance.owner_id}/{uuid.uuid4().hex}_original.{ext}"


# Ya no la usa ningún campo (se sacó archivo_normalizado) — queda acá porque
# la migración 0001_initial todavía la referencia por nombre (Django resuelve
# el upload_to de un FileField importando la función en vivo, no la congela
# en la migración) y borrarla rompe cualquier `migrate` desde cero.
def _upload_path_normalizado(instance, filename):
    return f"partituras/u{instance.owner_id}/{uuid.uuid4().hex}_normalizado.pdf"


def _upload_path_audio(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "mp3"
    return f"partituras/u{instance.owner_id}/audio_{uuid.uuid4().hex}.{ext}"


class Repertorio(models.Model):
    """Agrupador de más alto nivel para organizar la biblioteca (p.ej. las
    obras de una banda) — compartido entre todos los usuarios, igual que la
    biblioteca misma (ver Obra.ciclo). Se gestiona desde el admin, mismo
    criterio que crear una Obra (ver views.crear_obra)."""
    nombre = models.CharField(max_length=200, unique=True)

    class Meta:
        ordering = ['nombre']
        verbose_name = 'Repertorio'
        verbose_name_plural = 'Repertorios'

    def __str__(self):
        return self.nombre


class Ciclo(models.Model):
    """Sub-agrupador dentro de un Repertorio (p.ej. por año) — una Obra se
    asigna a lo sumo a un Ciclo (ver Obra.ciclo); el Repertorio de esa obra
    sale siempre de ciclo.repertorio, no hay un campo separado en Obra para
    eso (evita duplicar el dato)."""
    repertorio = models.ForeignKey(Repertorio, related_name='ciclos', on_delete=models.CASCADE)
    nombre = models.CharField(max_length=200)

    class Meta:
        unique_together = [('repertorio', 'nombre')]
        ordering = ['repertorio__nombre', 'nombre']
        verbose_name = 'Ciclo'
        verbose_name_plural = 'Ciclos'

    def __str__(self):
        return f"{self.repertorio} — {self.nombre}"


class Obra(models.Model):
    """La pieza musical en sí, independiente de cualquier instrumento — varias
    Partitura (una por parte/instrumento) pueden pertenecer a la misma Obra.
    Dueña de los datos que son propiedad de la obra y no de una parte en
    particular (itinerario de repeticiones/saltos, grabación de referencia
    para sincronizar) — ver notas de diseño del proyecto."""
    titulo = models.CharField(max_length=200)
    compositor = models.CharField(max_length=200, blank=True)
    arreglista = models.CharField(max_length=200, blank=True)
    ciclo = models.ForeignKey(
        Ciclo, null=True, blank=True, on_delete=models.SET_NULL, related_name='obras',
        help_text="Organización opcional de biblioteca (repertorio/ciclo) — no afecta la ejecución de la obra.",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='obras',
    )
    audio = models.FileField(
        upload_to=_upload_path_audio, null=True, blank=True,
        help_text="Grabación de referencia (mp3) para sincronizar el itinerario — ver Segmento.tiempo_inicio "
                   "y la pantalla de sincronización.",
    )
    publicada = models.BooleanField(
        default=True,
        help_text="Si está en False, sólo el dueño y los admins la ven (en la biblioteca y entrando "
                   "directo por URL) — el resto de los usuarios logueados no la ve en absoluto.",
    )
    creado = models.DateTimeField(auto_now_add=True)
    actualizado = models.DateTimeField(
        auto_now=True,
        help_text="Se actualiza sola en cualquier cambio de la obra en sí (título, audio, publicada...), "
                   "y también cuando cambia su itinerario (Segmento) o sus marcas de tiempo por compás "
                   "(MarcaTiempoCompas) — ver signals.py, que la toca aparte porque son modelos distintos. "
                   "NO cambia por preferencias de usuario (PreferenciaObra/PreferenciaParte) — esas son "
                   "por-usuario, no un cambio real de la obra.",
    )

    class Meta:
        ordering = ['-creado']
        verbose_name = 'Obra'
        verbose_name_plural = 'Obras'

    def __str__(self):
        return f"{self.titulo} ({self.compositor})" if self.compositor else self.titulo


class Segmento(models.Model):
    """Una fila del itinerario de ejecución de una Obra — un tramo contiguo
    de compases que se toca de corrido. Donde una fila no continúa en el
    compás/pulso exacto donde terminó la anterior, ahí hay un salto
    (repetición, D.C./D.S., al Coda — lo que sea): no hace falta representar
    el símbolo ni el motivo, sólo el tramo real que se toca. Una repetición
    simple son dos filas con el mismo rango; una primera/segunda vez son dos
    filas que divergen en el mismo punto.

    La última fila de una obra es un ancla de cierre: sólo tiene
    `tiempo_inicio` (dónde termina el último compás real), sin rango de
    compases propio (`compas_desde` null) — así la duración de CUALQUIER
    fila, incluida la última con contenido real, sale siempre de la misma
    cuenta: tiempo_inicio de la fila siguiente menos el propio. No hace
    falta un campo `tiempo_fin` aparte que sólo usaría la última fila.

    Indicación de compás, armadura y tempo base NO son campos de acá —
    son hechos de la partitura por POSICIÓN, no de esta fila (ver
    MarcaNotacion). Accelerando/ritardando/calderón tampoco: son hechos de
    posición también (ver EfectoTempo) — antes vivían acá como
    bpm_llegada/variacion_tempo, atados a esta fila, lo que impedía que un
    efecto cruzara un límite de fila."""

    obra = models.ForeignKey(Obra, related_name='segmentos', on_delete=models.CASCADE)
    orden = models.PositiveIntegerField(
        help_text="Numerar de a 10 (10, 20, 30…) para poder insertar filas en el medio sin renumerar el resto.",
    )
    # compas_desde/pulso_desde/compas_hasta/pulso_hasta son los campos
    # "de verdad" (los que usa el resto del código) — desde_texto/hasta_texto
    # son lo que el usuario tipea en la tabla ("4" o "4,1") y se procesan al
    # guardar (ver itinerario_obra). Quedan ambos a propósito, aunque sea
    # redundante: es más simple que armar un campo de formulario custom que
    # reparta un solo input en dos campos de modelo.
    compas_desde = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Vacío sólo en la fila final de cierre (sin contenido propio, sólo marca dónde termina el último compás).",
    )
    pulso_desde = models.FloatField(
        null=True, blank=True,
        help_text="Vacío = 1 (el primer pulso) — se resuelve así al leer, nunca se guarda el 1 explícito. "
                   "Puede ser decimal (1.5 = la corchea después del primer pulso).",
    )
    compas_hasta = models.PositiveIntegerField(null=True, blank=True)
    pulso_hasta = models.FloatField(
        null=True, blank=True,
        help_text="Vacío = hasta el final del último pulso del compás — se resuelve así al leer. También puede ser decimal.",
    )
    desde_texto = models.CharField(
        max_length=20, blank=True,
        help_text='Lo que se tipea en la tabla: "4" (compás 4, pulso 1) o "4,1.5" (compás 4, pulso 1 y medio) — '
                   'coma separa compás de pulso, punto es el decimal DENTRO del pulso.',
    )
    hasta_texto = models.CharField(
        max_length=20, blank=True,
        help_text='Igual que desde_texto, pero "4" sin coma acá significa "hasta el final del compás 4", no pulso 1.',
    )
    descripcion = models.CharField(max_length=200, blank=True, help_text="Rótulo libre (ej: 'Exposición', 'Coda') — sin efecto en la secuencia.")
    tiempo_inicio = models.DurationField(
        null=True, blank=True,
        help_text="Tiempo transcurrido desde el inicio de la grabación hasta acá — vacío hasta sincronizar con audio/video real.",
    )
    tiempo_inicio_calculado = models.DurationField(
        null=True, blank=True,
        help_text="Estimación a partir de bpm/bpm_llegada de cada tramo (no de una grabación real) — se recalcula "
                   "solo cada vez que se guarda el itinerario, queda como referencia independiente de tiempo_inicio.",
    )

    class Meta:
        ordering = ['obra', 'orden']
        unique_together = [('obra', 'orden')]
        verbose_name = 'Segmento'
        verbose_name_plural = 'Segmentos'

    def __str__(self):
        if self.compas_desde is None:
            return f"{self.obra} — cierre"
        return f"{self.obra} — c.{self.compas_desde}–{self.compas_hasta}"


class MarcaTiempoCompas(models.Model):
    """Tiempo real de una ocurrencia PUNTUAL de compás (no de una fila entera
    del itinerario, ver Segmento.tiempo_inicio) — sincronización fina,
    compás a compás, marcada escuchando el audio (ver sincronizar_compases).
    compas+pasada identifica la ocurrencia exacta (si el compás se repite —
    2da vez, D.C., etc. — cada pasada tiene su propia marca), mismo criterio
    de "pasada" que usa buscar_posicion.

    Convive con Segmento.tiempo_inicio en vez de reemplazarlo: en la
    ejecución, cada fuente ("por itinerario" o "por compases") se usa por
    separado según elija el usuario — nunca se mezclan entre sí (ver
    construir_plan en services.py)."""
    obra = models.ForeignKey(Obra, on_delete=models.CASCADE, related_name='marcas_tiempo_compas')
    compas = models.PositiveIntegerField()
    pasada = models.PositiveIntegerField(default=1)
    tiempo_inicio = models.DurationField()
    explicita = models.BooleanField(
        default=True,
        help_text="True si la puso el usuario (tap, edición manual, desplazamiento en bloque); "
                   "False si la generó la interpolación de sincronizar_compases — esas no sirven "
                   "de ancla para futuras interpolaciones, y se muestran atenuadas.",
    )

    class Meta:
        unique_together = [('obra', 'compas', 'pasada')]
        ordering = ['obra', 'compas', 'pasada']
        verbose_name = 'Marca de tiempo de compás'
        verbose_name_plural = 'Marcas de tiempo de compás'

    def __str__(self):
        return f"{self.obra} — c.{self.compas} ({self.pasada}ra vez)"


class MarcaTiempoPulso(models.Model):
    """Tiempo real de un PULSO puntual dentro de una ocurrencia de compás —
    extensión más fina de MarcaTiempoCompas (compas+pasada), para el caso
    en que el pulso calculado (interpolado entre dos MarcaTiempoCompas
    siguiendo la curva de bpm) no cae exactamente donde se escucha en la
    grabación real. Sparse a propósito: sólo existe para los pulsos que el
    usuario corrigió a mano arrastrando en sincronizar_compases.html — el
    resto de los pulsos de ese mismo compás sigue saliendo de la
    interpolación de siempre.

    Es una ancla MÁS de la misma fuente "por compases" (nunca "por
    itinerario", mismo criterio que MarcaTiempoCompas) — no una fuente
    nueva: _anclas_globales la agrega al mismo anclas_compases, sólo más
    precisa que una marca de compás entero (ver services.py)."""
    obra = models.ForeignKey(Obra, on_delete=models.CASCADE, related_name='marcas_tiempo_pulso')
    compas = models.PositiveIntegerField()
    pasada = models.PositiveIntegerField(default=1)
    pulso = models.PositiveIntegerField()
    tiempo_inicio = models.DurationField()
    explicita = models.BooleanField(default=True)

    class Meta:
        unique_together = [('obra', 'compas', 'pasada', 'pulso')]
        ordering = ['obra', 'compas', 'pasada', 'pulso']
        verbose_name = 'Marca de tiempo de pulso'
        verbose_name_plural = 'Marcas de tiempo de pulso'

    def __str__(self):
        return f"{self.obra} — c.{self.compas} p.{self.pulso} ({self.pasada}ra vez)"


class MarcaNotacion(models.Model):
    """Hecho de la PARTITURA (indicación de compás, armadura, tempo base) —
    a diferencia de Segmento, que es movimiento (qué se toca en qué orden),
    esto es una propiedad de la POSICIÓN en la obra: el compás 47 tiene la
    misma indicación/armadura/tempo la primera vez que se toca y en
    cualquier repetición. Por eso se busca por compás (nunca por fila de
    itinerario) — la búsqueda "la marca vigente más cercana hacia atrás"
    reemplaza la vieja herencia por orden de itinerario, que se rompía en
    saltos/repeticiones lejanas (heredaba de lo que sonó justo antes del
    salto, no del pasaje real).

    pasada=None (el caso normal) = rige en CUALQUIER pasada por ese compás.
    pasada completo = override puntual sólo para esa ocurrencia exacta
    (mismo criterio de "pasada" que MarcaTiempoCompas/buscar_posicion) —
    para el caso raro de que una repetición puntual difiera (ver
    servicios._resolver_marca_notacion).

    No incluye accelerando/ritardando/calderón — a diferencia de esto (un
    valor "vigente hasta la próxima marca"), esos son tramos/puntos
    ACOTADOS, no un valor persistente (ver EfectoTempo)."""

    TIPOS = [
        ('compas', 'Indicación de compás'),
        ('armadura', 'Armadura'),
        ('tempo', 'Tempo'),
    ]

    obra = models.ForeignKey(Obra, on_delete=models.CASCADE, related_name='marcas_notacion')
    tipo = models.CharField(max_length=10, choices=TIPOS)
    compas = models.PositiveIntegerField(help_text="A partir de este compás rige, hasta la próxima marca del mismo tipo.")
    pasada = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Vacío = rige en cualquier pasada por este compás. Completo = override sólo para esa pasada puntual.",
    )
    valor = models.CharField(
        max_length=20,
        help_text='Según tipo: "4/4" (compás), "96" (tempo, en bpm), texto libre (armadura).',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['obra', 'tipo', 'compas'], condition=models.Q(pasada__isnull=True),
                name='unico_general_por_obra_tipo_compas',
            ),
            models.UniqueConstraint(
                fields=['obra', 'tipo', 'compas', 'pasada'], condition=models.Q(pasada__isnull=False),
                name='unico_puntual_por_obra_tipo_compas_pasada',
            ),
        ]
        ordering = ['obra', 'tipo', 'compas', 'pasada']
        verbose_name = 'Marca de notación'
        verbose_name_plural = 'Marcas de notación'

    def __str__(self):
        sufijo = f" ({self.pasada}ra vez)" if self.pasada else ""
        return f"{self.obra} — c.{self.compas}{sufijo}: {self.get_tipo_display()} {self.valor}"


class EfectoTempo(models.Model):
    """Accelerando/ritardando/calderón — a diferencia de MarcaNotacion (un
    valor vigente hasta la próxima marca), esto es un tramo (accelerando/
    ritardando, con extremo propio) o un punto (calderón) ACOTADO, anclado
    a POSICIÓN en la obra (compás+pulso, nunca fila de itinerario) — así
    puede empezar a mitad de una fila del itinerario y terminar a mitad de
    la fila siguiente, sin que el límite de fila le importe. Reemplaza a
    Segmento.bpm_llegada/variacion_tempo (ver docstring de ese modelo).

    Sin campo `pasada` (a diferencia de MarcaNotacion/MarcaTiempoCompas):
    el efecto aplica siempre que se pase por esa posición, no hace falta
    distinguir 1ra/2da vez.

    accelerando/ritardando: el bpm rampa linealmente, por CANTIDAD DE
    PULSOS (no de tiempo), desde el bpm vigente en (compas_desde,
    pulso_desde) hasta `valor` (bpm de llegada), terminando en
    (compas_hasta, pulso_hasta) — compas_hasta/pulso_hasta obligatorios.
    calderón: sólo tiene (compas_desde, pulso_desde) — compas_hasta/
    pulso_hasta quedan vacíos — multiplica la duración calculada de ESE
    pulso por `valor` (factor, ej. "1.5" = 50% más largo). No dice nada de
    cómo se reparte visualmente dentro del compás — eso es tarea de
    sincronización con audio real, no de este modelo."""

    TIPOS = [
        ('accelerando', 'Accelerando'),
        ('ritardando', 'Ritardando'),
        ('calderon', 'Calderón'),
    ]

    obra = models.ForeignKey(Obra, on_delete=models.CASCADE, related_name='efectos_tempo')
    tipo = models.CharField(max_length=12, choices=TIPOS)
    # Mismo patrón que Segmento.desde_texto/hasta_texto -> compas_desde/
    # pulso_desde/compas_hasta/pulso_hasta (ver parsear_compas_pulso):
    # texto crudo + campos resueltos, a propósito, en vez de un campo de
    # formulario custom que reparta un solo input en dos campos de modelo.
    desde_texto = models.CharField(
        max_length=20,
        help_text='Dónde arranca el efecto: "compás" (pulso 1) o "compás,pulso" (ej: "10,2.5").',
    )
    compas_desde = models.PositiveIntegerField()
    pulso_desde = models.FloatField(null=True, blank=True)
    hasta_texto = models.CharField(
        max_length=20, blank=True,
        help_text='Vacío en calderón. Dónde termina (accelerando/ritardando) — mismo formato que desde_texto.',
    )
    compas_hasta = models.PositiveIntegerField(null=True, blank=True)
    pulso_hasta = models.FloatField(null=True, blank=True)
    valor = models.CharField(
        max_length=20,
        help_text='Según tipo: bpm de llegada (accelerando/ritardando) o factor de duración (calderón, ej: "1.5").',
    )

    class Meta:
        ordering = ['obra', 'compas_desde', 'pulso_desde']
        verbose_name = 'Efecto de tempo'
        verbose_name_plural = 'Efectos de tempo'

    def __str__(self):
        if self.tipo == 'calderon':
            return f"{self.obra} — calderón en {self.desde_texto}"
        return f"{self.obra} — {self.get_tipo_display().lower()} {self.desde_texto}–{self.hasta_texto} → {self.valor}"


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
    obra = models.ForeignKey(
        Obra,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='partituras',
        help_text="La obra a la que pertenece esta parte, si ya se agrupó — separarla no borra la partitura.",
    )
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
    estado_normalizacion = models.CharField(max_length=12, choices=ESTADOS_NORM, default='pendiente')
    estado_analisis = models.CharField(max_length=12, choices=ESTADOS_ANALISIS, default='pendiente')
    publicada = models.BooleanField(
        default=True,
        help_text="Si está en False, sólo el dueño de la parte, el dueño de la obra a la que pertenece "
                   "(si tiene) y los admins la ven — le da al dueño de la obra la potestad de aceptar o "
                   "rechazar una parte que subió otro usuario.",
    )
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-creado']
        verbose_name = 'Partitura'
        verbose_name_plural = 'Partituras'

    def __str__(self):
        nombre = self.nombre_parte
        return f"{self.titulo} ({nombre})" if nombre else self.titulo

    @property
    def nombre_parte(self):
        """Lo que distingue a esta parte de las demás de la misma obra: el
        nombre de parte si se cargó (ej. "Clarinete 2"), si no el
        instrumento — nunca queda en blanco si hay instrumento cargado."""
        return self.parte or (str(self.instrumento) if self.instrumento_id else "")

    def _paginas_activas(self):
        return self.paginas.filter(ignorada=False)

    def _etapa_completa(self, campo):
        """True si hay al menos una página activa y NINGUNA tiene `campo`
        en False — una partitura sin páginas todavía (o sin ninguna activa)
        nunca cuenta como "completa" (sería vacuamente cierto, y no arrancó)."""
        activas = self._paginas_activas()
        return activas.exists() and not activas.filter(**{campo: False}).exists()

    @property
    def margenes_completos(self):
        return self._etapa_completa('margen_confirmado')

    @property
    def sistemas_completos(self):
        activas = list(self._paginas_activas())
        return bool(activas) and all(p.sistemas_confirmados for p in activas)

    @property
    def ancla_completa(self):
        return self._etapa_completa('ancla_confirmada')

    @property
    def barras_completas(self):
        return self._etapa_completa('barras_confirmadas')


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

    # % (0-1) de tinta requerida en una columna para considerarla "con
    # contenido" al buscar dónde empieza/termina realmente un sistema (ver
    # vision._segmentos_contenido_sistema) — None usa el default global
    # (vision.UMBRAL_CONTENIDO_SISTEMA_DEFAULT). No hay un valor universal:
    # una página escaneada con ruido de fondo necesita uno más alto que una
    # generada por editor; ajustable por página para cuando el usuario ve
    # que la detección de bordes de sistema falla y sospecha de esto.
    umbral_contenido_sistema = models.FloatField(null=True, blank=True)

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
        y nunca se confirmó — o sea, todavía no se detectó nada para esta
        página. No 100% infalible (un margen detectado que coincida
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
    contenido_x0 = models.FloatField(
        null=True, blank=True,
        help_text="Columna real (0-1) donde arranca el contenido de este sistema (clave/armadura) — "
                   "a diferencia del margen de página, que es un dato de toda la página. Vacío si no "
                   "se pudo detectar (sistema vacío): el primer compás cae al margen en ese caso.",
    )
    contenido_x1 = models.FloatField(
        null=True, blank=True,
        help_text="Columna real (0-1) donde termina el contenido de este sistema, SÓLO cuando no "
                   "coincide con la última barra aceptada (si coincide, esa barra ya cierra bien el "
                   "compás y esto queda en None — no hace falta ningún límite virtual de cierre).",
    )

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
    repeticiones = models.PositiveIntegerField(
        default=1,
        help_text="Cuántos compases reales representa éste — 1 en el caso normal; más de 1 para "
                   "un silencio de varios compases marcado a mano por el usuario (no se detecta "
                   "automáticamente). El siguiente compás numera desde numero + repeticiones, no "
                   "siempre numero + 1.",
    )
    origen = models.CharField(max_length=10, choices=ORIGENES, default='auto')
    confirmado = models.BooleanField(default=False)

    class Meta:
        ordering = ['sistema__pagina__numero', 'sistema__orden', 'x']
        verbose_name = 'Compás'
        verbose_name_plural = 'Compases'

    def __str__(self):
        return f"Compás {self.numero} — {self.sistema}"


class PreferenciaObra(models.Model):
    """Cómo un usuario en particular prefiere ver/ejecutar una obra en el
    navegador — rango, loop, velocidad, compases al aire y qué parte sigue.
    Se autoguarda solo (sin botón) cada vez que cambia algo, para
    precompletar el navegador la próxima vez en vez de arrancar de cero.
    Separado de PreferenciaParte porque estos campos no dependen de qué
    parte se esté mirando (el rango que querés tocar es el mismo tramo de
    la obra sea cual sea la parte)."""
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='preferencias_obra')
    obra = models.ForeignKey(Obra, on_delete=models.CASCADE, related_name='preferencias_usuario')
    desde_compas = models.CharField(max_length=20, blank=True)
    desde_pasada = models.PositiveIntegerField(default=1)
    hasta_compas = models.CharField(max_length=20, blank=True)
    hasta_pasada = models.PositiveIntegerField(default=1)
    loop = models.BooleanField(default=False)
    velocidad = models.PositiveIntegerField(default=100)
    compases_al_aire = models.PositiveIntegerField(default=1)
    ejecutar_con_audio = models.BooleanField(
        default=False,
        help_text="Si la ejecución sigue el audio de referencia (tiempos reales, velocidad fija) en vez "
                   "del reloj calculado — ver sincronizar_itinerario. Se recuerda igual que el resto de estas preferencias.",
    )
    FUENTES_TEMPORIZACION = [
        ('itinerario', 'Itinerario'),
        ('compases', 'Compases'),
    ]
    fuente_temporizacion = models.CharField(
        max_length=12, choices=FUENTES_TEMPORIZACION, default='compases',
        help_text="Qué fuente de tiempos reales gobierna el cursor/sombreado durante la ejecución: "
                   "'itinerario' usa sólo Segmento.tiempo_inicio (con tiempo_inicio_calculado como base "
                   "siempre disponible, ver sincronizar_itinerario); 'compases' usa sólo MarcaTiempoCompas "
                   "(sin caer al cálculo puro fuera del tramo cubierto, ver sincronizar_compases). Es "
                   "independiente de ejecutar_con_audio (ese controla si SUENA el audio de referencia, "
                   "esto controla de qué reloj sale la posición del cursor).",
    )
    parte_seguida = models.ForeignKey(
        Partitura, null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
        help_text="Última parte elegida explícitamente en el selector — desempata antes que 'mi propia parte'.",
    )
    MODOS_SCORE = [
        ('compas', 'Compás'),
        ('partitura', 'Partitura'),
    ]
    modo_score = models.CharField(
        max_length=10, choices=MODOS_SCORE, blank=True,
        help_text="Modo compás vs. modo partitura en el navegador — en blanco significa que todavía no "
                   "se guardó ninguna elección, y se usa el default viejo (ancho de pantalla) en su lugar.",
    )
    actualizado = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('usuario', 'obra')]
        verbose_name = 'Preferencia de obra'
        verbose_name_plural = 'Preferencias de obra'

    def __str__(self):
        return f"{self.usuario} — {self.obra}"


class PreferenciaParte(models.Model):
    """Zoom preferido de un usuario para una parte puntual — aparte de
    PreferenciaObra porque depende de la diagramación propia de CADA parte
    (dos partes de la misma obra pueden necesitar zooms bien distintos),
    no de la obra en general."""
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='preferencias_parte')
    partitura = models.ForeignKey(Partitura, on_delete=models.CASCADE, related_name='preferencias_usuario')
    nivel_zoom = models.FloatField(default=1.0)
    actualizado = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('usuario', 'partitura')]
        verbose_name = 'Preferencia de parte'
        verbose_name_plural = 'Preferencias de parte'

    def __str__(self):
        return f"{self.usuario} — {self.partitura}"
