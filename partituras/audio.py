"""
Normalización del audio de referencia subido a una obra (ver
Obra.audio/obra_detalle): detecta mp3 codificado en VBR/ABR y lo convierte
a CBR antes de guardarlo.

Por qué: el navegador busca posición en un mp3 (audio.currentTime = X, ver
navegador_obra.html/sincronizar_compases.html) usando la tabla Xing/LAME de
sólo 100 puntos que trae el archivo — en un mp3 VBR eso es una
interpolación lineal entre 100 puntos, no una posición exacta, y el
bitrate real varía a lo largo del archivo (pasajes densos vs. simples).
Caso real diagnosticado a mano el 2026-08-25 (obra 32, compás 142): el
tiempo guardado en MarcaTiempoCompas era exacto, pero saltar directo a ese
compás sonaba hasta ~0.5s desalineado del audio real; recodificar ese mismo
archivo a CBR (bitrate constante, búsqueda lineal exacta, sin tabla) hizo
desaparecer el desfasaje. Confirmado en vivo contra el mp3 real de esa
obra, no es una hipótesis sin probar.

Alcance a propósito: sólo actúa en la SUBIDA de un audio nuevo (ver
obra_detalle) — no toca audios ya subidos, para no tocar de golpe obras que
ya están sincronizadas y funcionando (aunque puedan tener el mismo problema
de fondo; ver Pendientes para una pasada retroactiva más adelante).
Requiere ffmpeg instalado en el sistema (no es un paquete de Python) — si
falta, o cualquier paso del proceso falla, no aborta la subida: el archivo
original sigue su curso tal cual llegó (ver normalizar_audio_referencia).
"""

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.core.files.base import ContentFile
from django.utils.translation import gettext as _
from mutagen import MutagenError
from mutagen.mp3 import MP3, BitrateMode

logger = logging.getLogger(__name__)

# Un CBR justo al promedio del VBR original le saca margen a los pasajes
# más densos (el VBR original les daba más bits que a los tramos simples,
# un CBR al promedio ya no puede) — subir un escalón estándar de mp3 por
# encima del promedio evita perder calidad ahí, sin inflar el archivo de
# más. Bitrates estándar de mp3 (los que entiende cualquier encoder/reproductor).
_BITRATES_CBR_ESTANDAR = [32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
_FACTOR_MARGEN_BITRATE = 1.15

_TIMEOUT_FFMPEG_SEGUNDOS = 120


def _bitrate_cbr_sugerido(bitrate_promedio_bps):
    objetivo_kbps = bitrate_promedio_bps / 1000 * _FACTOR_MARGEN_BITRATE
    for candidato in _BITRATES_CBR_ESTANDAR:
        if candidato >= objetivo_kbps:
            return candidato
    return _BITRATES_CBR_ESTANDAR[-1]


def normalizar_audio_referencia(archivo_subido):
    """archivo_subido: un UploadedFile de Django (ver obra_detalle,
    request.FILES["audio"]). Si es un mp3 realmente VBR/ABR, lo convierte a
    CBR con ffmpeg y devuelve (nuevo_archivo, mensaje) — nuevo_archivo es
    un django.core.files.File listo para asignar a Obra.audio, mensaje es
    un texto para avisarle al usuario qué se hizo (pensado para colgar de
    un messages.info). Si no hace falta convertir (ya es CBR, no es mp3
    válido, o algo falló en el intento — ffmpeg ausente, archivo corrupto,
    timeout, etc.) devuelve (None, None) sin tocar nada: quien llama sigue
    con el archivo original tal cual se subió, la conversión es una mejora
    y nunca un requisito para poder subir audio."""
    archivo_subido.seek(0)
    try:
        info = MP3(archivo_subido).info
    except (MutagenError, OSError):
        archivo_subido.seek(0)
        return None, None
    archivo_subido.seek(0)

    # mutagen.mp3.BitrateMode no es un enum.Enum estándar (es un decorador
    # propio de mutagen, ver mutagen._util.enum) — no expone .name de forma
    # confiable, por eso se compara por igualdad en vez de leer el nombre.
    if info.bitrate_mode not in (BitrateMode.VBR, BitrateMode.ABR):
        return None, None
    modo_str = "VBR" if info.bitrate_mode == BitrateMode.VBR else "ABR"

    bitrate_cbr = _bitrate_cbr_sugerido(info.bitrate)

    # Directorio temporal en vez de NamedTemporaryFile: en Windows, ffmpeg
    # no puede abrir para escritura un archivo que este proceso todavía
    # tiene abierto (a diferencia de POSIX) — con un directorio, cada
    # archivo se abre y cierra una sola vez, sin ese conflicto en ningún SO.
    tmpdir = tempfile.mkdtemp(prefix="ss_audio_")
    try:
        ruta_entrada = Path(tmpdir) / "entrada.mp3"
        ruta_salida = Path(tmpdir) / "salida.mp3"
        with open(ruta_entrada, "wb") as f:
            for chunk in archivo_subido.chunks():
                f.write(chunk)
        archivo_subido.seek(0)

        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error", "-i", str(ruta_entrada),
                    "-c:a", "libmp3lame", "-b:a", f"{bitrate_cbr}k",
                    "-ar", str(info.sample_rate), "-ac", str(info.channels),
                    str(ruta_salida),
                ],
                check=True, capture_output=True, timeout=_TIMEOUT_FFMPEG_SEGUNDOS,
            )
        except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
            logger.warning("No se pudo convertir audio VBR/ABR a CBR: %s", e)
            return None, None

        contenido = ruta_salida.read_bytes()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if not contenido:
        return None, None

    mensaje = _(
        "El audio subido estaba codificado en %(modo)s (bitrate variable) — eso puede "
        "desalinear la posición hasta medio segundo al saltar directo a un compás (en "
        "Practicar o en Sincronizar compases), aunque los tiempos guardados sean exactos. "
        "Se convirtió automáticamente a mp3 CBR (%(bitrate)s kbps) antes de guardarlo."
    ) % {"modo": modo_str, "bitrate": bitrate_cbr}
    return ContentFile(contenido, name="audio_cbr.mp3"), mensaje
