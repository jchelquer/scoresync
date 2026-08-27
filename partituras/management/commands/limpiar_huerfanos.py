from django.core.management.base import BaseCommand

from partituras.media_cleanup import borrar_huerfanos, encontrar_huerfanos


class Command(BaseCommand):
    help = (
        "Lista (o, con --borrar, borra) archivos huérfanos en MEDIA_ROOT: "
        "audios/PDFs que ninguna Obra/Partitura referencia y carpetas de "
        "cache_paginas/ de partituras ya borradas. Sin --borrar es sólo "
        "diagnóstico, no toca nada. Misma lógica que usa el botón 'Limpiar "
        "huérfanos' del admin (ver partituras/media_cleanup.py)."
    )

    def add_arguments(self, parser):
        parser.add_argument('--borrar', action='store_true', help="Borra los huérfanos encontrados en vez de sólo listarlos.")

    def handle(self, *args, **options):
        huerfanos = encontrar_huerfanos()
        total = len(huerfanos["audios"]) + len(huerfanos["pdfs"]) + len(huerfanos["cache_paginas"])

        if total == 0:
            self.stdout.write(self.style.SUCCESS("No hay archivos huérfanos."))
            return

        self.stdout.write(f"Audios huérfanos: {len(huerfanos['audios'])} ({huerfanos['kb_audios']:.0f} KB)")
        for archivo in huerfanos["audios"]:
            self.stdout.write(f"  {archivo}")

        self.stdout.write(f"PDFs huérfanos: {len(huerfanos['pdfs'])} ({huerfanos['kb_pdfs']:.0f} KB)")
        for archivo in huerfanos["pdfs"]:
            self.stdout.write(f"  {archivo}")

        self.stdout.write(f"Carpetas de cache_paginas huérfanas: {len(huerfanos['cache_paginas'])} ({huerfanos['kb_cache_paginas']:.0f} KB)")
        for carpeta in huerfanos["cache_paginas"]:
            self.stdout.write(f"  {carpeta}")

        if options['borrar']:
            borrar_huerfanos(huerfanos)
            self.stdout.write(self.style.SUCCESS(f"\n{total} elemento(s) borrado(s)."))
        else:
            self.stdout.write(self.style.WARNING(f"\n{total} elemento(s) encontrado(s). Correr con --borrar para eliminarlos."))
