"""Detección/borrado de archivos huérfanos bajo MEDIA_ROOT: archivos en disco
que ningún modelo referencia (surgen del reemplazo de un FileField vía admin,
que Django no limpia solo -- a diferencia del borrado de fila, que sí está
cubierto por los post_delete de signals.py), y carpetas de cache_paginas/ de
partituras ya borradas (esas nunca se limpiaron, ver caché en views.py).

Un solo módulo reusado por el management command y la vista de admin, para
no duplicar la lógica de scaneo (mismo patrón usado en music_core)."""

import shutil
from pathlib import Path

from django.conf import settings

from .models import Obra, Partitura


def _archivos_referenciados():
    audios = {
        Path(nombre).name
        for nombre in Obra.objects.values_list("audio", flat=True)
        if nombre
    }
    pdfs = {
        Path(nombre).name
        for nombre in Partitura.objects.values_list("archivo_original", flat=True)
        if nombre
    }
    return audios, pdfs


def _tamano(paths, es_carpeta=False):
    total = 0
    for p in paths:
        if es_carpeta:
            total += sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        else:
            total += p.stat().st_size
    return total


def encontrar_huerfanos():
    """Devuelve un dict con 3 categorías de huérfanos: 'audios' y 'pdfs'
    (archivos sueltos en media/partituras/u*/, distinguidos por el prefijo
    'audio_' que usa _upload_path_audio) y 'cache_paginas' (carpetas enteras
    de páginas cacheadas cuya Partitura ya no existe). Cada lista trae Paths
    ordenados; kb_* trae el tamaño total de cada categoría."""
    media_root = Path(settings.MEDIA_ROOT)
    audios_ref, pdfs_ref = _archivos_referenciados()

    audios, pdfs = [], []
    carpeta_partituras = media_root / "partituras"
    if carpeta_partituras.is_dir():
        for carpeta_owner in carpeta_partituras.iterdir():
            if not carpeta_owner.is_dir():
                continue
            for archivo in carpeta_owner.iterdir():
                if not archivo.is_file():
                    continue
                if archivo.name.startswith("audio_"):
                    if archivo.name not in audios_ref:
                        audios.append(archivo)
                elif archivo.name not in pdfs_ref:
                    pdfs.append(archivo)

    cache_paginas = []
    carpeta_cache = media_root / "cache_paginas"
    if carpeta_cache.is_dir():
        ids_existentes = set(Partitura.objects.values_list("pk", flat=True))
        for carpeta_partitura in carpeta_cache.iterdir():
            if not carpeta_partitura.is_dir():
                continue
            try:
                pk = int(carpeta_partitura.name)
            except ValueError:
                continue
            if pk not in ids_existentes:
                cache_paginas.append(carpeta_partitura)

    return {
        "audios": sorted(audios),
        "pdfs": sorted(pdfs),
        "cache_paginas": sorted(cache_paginas),
        "kb_audios": _tamano(audios) / 1024,
        "kb_pdfs": _tamano(pdfs) / 1024,
        "kb_cache_paginas": _tamano(cache_paginas, es_carpeta=True) / 1024,
    }


def borrar_huerfanos(huerfanos):
    """Borra los archivos/carpetas de un resultado de encontrar_huerfanos().
    Llamar con un resultado recién escaneado -- no vuelve a verificar contra
    la DB, así que si algo cambió entremedio (p.ej. una subida en curso)
    podría borrar de más."""
    for archivo in huerfanos["audios"] + huerfanos["pdfs"]:
        archivo.unlink(missing_ok=True)
    for carpeta in huerfanos["cache_paginas"]:
        shutil.rmtree(carpeta, ignore_errors=True)
