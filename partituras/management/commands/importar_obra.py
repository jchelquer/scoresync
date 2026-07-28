import glob
import json
import os
import shutil
import subprocess
import zipfile
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from actividades.models import Instrumento
from partituras.models import (
    Barra, Ciclo, Compas, EfectoTempo, MarcaNotacion, MarcaTiempoCompas,
    MarcaTiempoPulso, Obra, Pagina, Partitura, Segmento, Sistema,
)

User = get_user_model()


def _duracion(segundos):
    return None if segundos is None else timedelta(seconds=segundos)


def _sin(diccionario, *claves):
    return {k: v for k, v in diccionario.items() if k not in claves}


class Command(BaseCommand):
    help = (
        "Importa una Obra completa desde un .zip generado por exportar_obra — "
        "SIEMPRE crea una Obra nueva con PKs frescas de este entorno (nunca "
        "reemplaza ni fusiona con una existente); si ya había una versión vieja, "
        "borrala a mano después de confirmar que la importación quedó bien. "
        "Por seguridad, antes de tocar nada hace un pg_dump completo de la base "
        "(compartida con el resto del ecosistema) — usar --sin-backup para saltarlo."
    )

    def add_arguments(self, parser):
        parser.add_argument('archivo_zip', type=str)
        parser.add_argument('--owner', type=str, required=True, help="Username del dueño a asignar en este entorno.")
        parser.add_argument('--yes', action='store_true', help="No pedir confirmación antes de importar.")
        parser.add_argument('--sin-backup', action='store_true', help="Saltear el respaldo de la base antes de importar (no recomendado).")
        parser.add_argument('--backup-dir', type=str, default='.', help="Carpeta donde guardar el respaldo (default: directorio actual).")

    def handle(self, *args, **options):
        ruta = options['archivo_zip']
        if not os.path.exists(ruta):
            raise CommandError(f"No existe el archivo {ruta}")

        try:
            owner = User.objects.get(username=options['owner'])
        except User.DoesNotExist:
            raise CommandError(f"No existe un usuario con username '{options['owner']}' en este entorno.")

        with zipfile.ZipFile(ruta, 'r') as zf:
            datos = json.loads(zf.read('datos.json'))
            manifest = json.loads(zf.read('manifest.json'))

            n_partituras = len(datos['partituras'])
            n_paginas = sum(len(p['paginas']) for p in datos['partituras'])
            n_compases = sum(len(s['compases']) for p in datos['partituras'] for pg in p['paginas'] for s in pg['sistemas'])
            self.stdout.write(
                f"Obra: {datos['titulo']!r} (exportada {manifest['exportado']}, dueño original: {manifest['owner_username']})\n"
                f"  {n_partituras} partitura(s), {n_paginas} página(s), {n_compases} compás(es) guardados\n"
                f"  Audio: {'sí' if datos['audio_media'] else 'no'}\n"
                f"  Se va a crear como OBRA NUEVA en este entorno, asignada a '{owner.username}'."
            )

            if not options['yes']:
                respuesta = input("¿Confirmar importación? [s/N] ").strip().lower()
                if respuesta != 's':
                    self.stdout.write("Cancelado.")
                    return

            if not options['sin_backup']:
                self._respaldar_base(options['backup_dir'])
            else:
                self.stdout.write(self.style.WARNING("Saltando el respaldo de la base (--sin-backup) — a criterio tuyo."))

            with transaction.atomic():
                obra = self._crear_obra(zf, datos, owner)

        self.stdout.write(self.style.SUCCESS(f"Obra importada con id={obra.pk}: {obra}"))

    def _encontrar_pg_dump(self):
        """Devuelve la ruta a un pg_dump REAL y EJECUTABLE por subprocess (no
        alcanza con que shutil.which lo encuentre: en Windows, un .bat en el
        PATH lo encuentra `which` mismo, pero subprocess.run sin shell=True no
        sabe invocarlo — CreateProcess necesita el .exe posta). Primero prueba
        el PATH normal (funciona tal cual en Linux/la VPS); si el que encontró
        no es directamente ejecutable así, busca la instalación típica de
        PostgreSQL para Windows como respaldo."""
        candidato = shutil.which('pg_dump')
        if candidato and not candidato.lower().endswith(('.bat', '.cmd')):
            return candidato
        for patron in (
            "C:/Program Files/PostgreSQL/*/bin/pg_dump.exe",
            "C:/Program Files (x86)/PostgreSQL/*/bin/pg_dump.exe",
        ):
            encontrados = sorted(glob.glob(patron), reverse=True)  # la versión más nueva primero
            if encontrados:
                return encontrados[0]
        return None

    def _respaldar_base(self, carpeta):
        """pg_dump completo de la base (compartida con ensayos/afinación/tempo/
        infedu) ANTES de escribir nada — se está por hacer una importación que
        crea filas nuevas en un entorno ajeno (típicamente la VPS), y si algo
        sale mal, poder restaurar de un dump reciente es más simple y seguro
        que tratar de deshacerlo a mano. Aborta la importación si el dump
        falla — no tiene sentido seguir sin la red de seguridad, salvo que el
        usuario la salte a propósito con --sin-backup."""
        db = settings.DATABASES['default']
        pg_dump = self._encontrar_pg_dump()
        if pg_dump is None:
            raise CommandError(
                "No se encontró pg_dump (ni en el PATH ni en la instalación típica de Windows) — "
                "no se puede hacer el respaldo de seguridad. Instalá el cliente de PostgreSQL o "
                "corré con --sin-backup si estás seguro de saltearlo."
            )
        os.makedirs(carpeta, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        destino = os.path.join(carpeta, f"respaldo_{db['NAME']}_antes_de_importar_{timestamp}.sql")

        self.stdout.write(f"Haciendo respaldo de la base '{db['NAME']}' en {destino} (con {pg_dump}) ...")
        comando = [
            pg_dump,
            '-h', db['HOST'] or 'localhost',
            '-p', str(db['PORT'] or '5432'),
            '-U', db['USER'],
            '-f', destino,
            db['NAME'],
        ]
        entorno = {**os.environ, 'PGPASSWORD': db['PASSWORD']}
        resultado = subprocess.run(comando, env=entorno, capture_output=True, text=True)
        if resultado.returncode != 0 or not os.path.exists(destino) or os.path.getsize(destino) == 0:
            raise CommandError(
                f"pg_dump falló (código {resultado.returncode}) — no se sigue con la importación.\n{resultado.stderr}"
            )
        self.stdout.write(self.style.SUCCESS(f"Respaldo OK: {destino} ({os.path.getsize(destino)} bytes)."))

    def _crear_obra(self, zf, datos, owner):
        ciclo = None
        if datos.get('ciclo'):
            ciclo = Ciclo.objects.filter(
                repertorio__nombre=datos['ciclo']['repertorio'], nombre=datos['ciclo']['ciclo'],
            ).first()
            if ciclo is None:
                self.stdout.write(self.style.WARNING(
                    f"No se encontró el ciclo '{datos['ciclo']['repertorio']} — {datos['ciclo']['ciclo']}' "
                    "en este entorno — la obra importada queda sin ciclo asignado."
                ))

        obra = Obra.objects.create(
            **_sin(datos, 'owner_username', 'ciclo', 'audio_media', 'segmentos', 'marcas_tiempo_compas',
                   'marcas_tiempo_pulso', 'marcas_notacion', 'efectos_tempo', 'partituras'),
            owner=owner, ciclo=ciclo,
        )
        if datos.get('audio_media'):
            obra.audio.save(os.path.basename(datos['audio_media']), ContentFile(zf.read(datos['audio_media'])), save=True)

        for s in datos['segmentos']:
            Segmento.objects.create(obra=obra, **_sin(s, 'tiempo_inicio', 'tiempo_inicio_calculado'),
                                     tiempo_inicio=_duracion(s['tiempo_inicio']),
                                     tiempo_inicio_calculado=_duracion(s['tiempo_inicio_calculado']))
        for m in datos['marcas_tiempo_compas']:
            MarcaTiempoCompas.objects.create(obra=obra, **_sin(m, 'tiempo_inicio'), tiempo_inicio=_duracion(m['tiempo_inicio']))
        for m in datos['marcas_tiempo_pulso']:
            MarcaTiempoPulso.objects.create(obra=obra, **_sin(m, 'tiempo_inicio'), tiempo_inicio=_duracion(m['tiempo_inicio']))
        for m in datos['marcas_notacion']:
            MarcaNotacion.objects.create(obra=obra, **m)
        for e in datos['efectos_tempo']:
            EfectoTempo.objects.create(obra=obra, **e)

        instrumentos_no_encontrados = set()
        for p in datos['partituras']:
            instrumento = None
            if p.get('instrumento_nombre'):
                instrumento = Instrumento.objects.filter(nombre=p['instrumento_nombre']).first()
                if instrumento is None:
                    instrumentos_no_encontrados.add(p['instrumento_nombre'])

            partitura = Partitura.objects.create(
                **_sin(p, 'instrumento_nombre', 'owner_username', 'archivo_media', 'paginas'),
                obra=obra, instrumento=instrumento, owner=owner,
            )
            partitura.archivo_original.save(
                os.path.basename(p['archivo_media']), ContentFile(zf.read(p['archivo_media'])), save=True,
            )

            for pg in p['paginas']:
                pagina = Pagina.objects.create(partitura=partitura, **_sin(pg, 'sistemas'))
                for s in pg['sistemas']:
                    sistema = Sistema.objects.create(pagina=pagina, **_sin(s, 'barras', 'compases'))
                    for b in s['barras']:
                        Barra.objects.create(sistema=sistema, **b)
                    for c in s['compases']:
                        Compas.objects.create(sistema=sistema, **c)

        if instrumentos_no_encontrados:
            self.stdout.write(self.style.WARNING(
                "Instrumento(s) no encontrados en este entorno (quedaron sin asignar en la partitura importada): "
                + ", ".join(sorted(instrumentos_no_encontrados))
            ))
        return obra
