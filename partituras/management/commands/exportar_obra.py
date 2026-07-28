import json
import zipfile
from datetime import datetime, timezone

from django.core.management.base import BaseCommand, CommandError
from django.db.models import DurationField
from django.utils.text import slugify

from partituras.models import Obra


def _campos_simples(instancia, excluir):
    """Serializa todos los campos NO relacionales de una instancia (menos 'id'
    y lo que se pase en `excluir` — típicamente la FK al padre, manejada
    aparte, y cualquier FileField, que se exporta como archivo dentro del
    zip, no como JSON). DurationField sale como segundos (float) para que
    sea JSON-serializable; se reconstruye como timedelta al importar."""
    datos = {}
    for campo in instancia._meta.fields:
        if campo.name == 'id' or campo.name in excluir or campo.is_relation:
            continue
        valor = getattr(instancia, campo.name)
        if isinstance(campo, DurationField) and valor is not None:
            valor = valor.total_seconds()
        datos[campo.name] = valor
    return datos


def _extension(nombre_archivo):
    partes = nombre_archivo.rsplit('.', 1)
    return ('.' + partes[1]) if len(partes) == 2 else ''


class Command(BaseCommand):
    help = (
        "Exporta una Obra completa (itinerario, marcas de notación/tempo, marcas de "
        "tiempo por compás/pulso, y cada Partitura con sus páginas/sistemas/barras/"
        "compases) más los archivos media asociados (audio, PDFs) a un .zip — para "
        "llevarla entre el entorno local y la VPS (bases de datos físicamente "
        "separadas). Ver importar_obra para el otro lado."
    )

    def add_arguments(self, parser):
        parser.add_argument('obra_id', type=int)
        parser.add_argument('--out', type=str, default=None, help="Ruta del .zip de salida (default: obra_<id>_<slug>.zip)")

    def handle(self, *args, **options):
        try:
            obra = Obra.objects.select_related('ciclo__repertorio', 'owner').get(pk=options['obra_id'])
        except Obra.DoesNotExist:
            raise CommandError(f"No existe Obra {options['obra_id']}")

        out_path = options['out'] or f"obra_{obra.pk}_{slugify(obra.titulo)}.zip"

        with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            datos = _campos_simples(obra, {'audio', 'creado', 'actualizado'})
            datos['owner_username'] = obra.owner.username
            datos['ciclo'] = None
            if obra.ciclo_id:
                datos['ciclo'] = {'repertorio': obra.ciclo.repertorio.nombre, 'ciclo': obra.ciclo.nombre}

            datos['audio_media'] = None
            if obra.audio:
                nombre = 'media/obra_audio' + _extension(obra.audio.name)
                with obra.audio.open('rb') as f:
                    zf.writestr(nombre, f.read())
                datos['audio_media'] = nombre

            datos['segmentos'] = [_campos_simples(s, {'obra'}) for s in obra.segmentos.all()]
            datos['marcas_tiempo_compas'] = [_campos_simples(m, {'obra'}) for m in obra.marcas_tiempo_compas.all()]
            datos['marcas_tiempo_pulso'] = [_campos_simples(m, {'obra'}) for m in obra.marcas_tiempo_pulso.all()]
            datos['marcas_notacion'] = [_campos_simples(m, {'obra'}) for m in obra.marcas_notacion.all()]
            datos['efectos_tempo'] = [_campos_simples(e, {'obra'}) for e in obra.efectos_tempo.all()]

            datos['partituras'] = []
            for i, partitura in enumerate(obra.partituras.select_related('instrumento', 'owner').all()):
                datos_partitura = _campos_simples(partitura, {'obra', 'instrumento', 'owner', 'archivo_original', 'creado'})
                datos_partitura['instrumento_nombre'] = partitura.instrumento.nombre if partitura.instrumento_id else None
                datos_partitura['owner_username'] = partitura.owner.username

                nombre_pdf = f'media/partitura_{i}' + _extension(partitura.archivo_original.name)
                with partitura.archivo_original.open('rb') as f:
                    zf.writestr(nombre_pdf, f.read())
                datos_partitura['archivo_media'] = nombre_pdf

                datos_partitura['paginas'] = []
                for pagina in partitura.paginas.all():
                    datos_pagina = _campos_simples(pagina, {'partitura'})
                    datos_pagina['sistemas'] = []
                    for sistema in pagina.sistemas.all():
                        datos_sistema = _campos_simples(sistema, {'pagina'})
                        datos_sistema['barras'] = [_campos_simples(b, {'sistema'}) for b in sistema.barras.all()]
                        datos_sistema['compases'] = [_campos_simples(c, {'sistema'}) for c in sistema.compases.all()]
                        datos_pagina['sistemas'].append(datos_sistema)
                    datos_partitura['paginas'].append(datos_pagina)

                datos['partituras'].append(datos_partitura)

            manifest = {
                'version': 1,
                'exportado': datetime.now(timezone.utc).isoformat(),
                'obra_titulo': obra.titulo,
                'owner_username': obra.owner.username,
            }
            zf.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
            zf.writestr('datos.json', json.dumps(datos, ensure_ascii=False, indent=2))

        n_partituras = len(datos['partituras'])
        n_paginas = sum(len(p['paginas']) for p in datos['partituras'])
        n_compases = sum(len(s['compases']) for p in datos['partituras'] for pg in p['paginas'] for s in pg['sistemas'])
        self.stdout.write(self.style.SUCCESS(
            f"Exportado a {out_path}: {n_partituras} partitura(s), {n_paginas} página(s), "
            f"{n_compases} compás(es) guardados, audio: {'sí' if datos['audio_media'] else 'no'}."
        ))
