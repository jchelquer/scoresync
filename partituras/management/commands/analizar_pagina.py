import cv2
from django.core.management.base import BaseCommand, CommandError

from partituras.models import Partitura
from partituras.pdf import rasterizar_pagina
from partituras.vision import detectar_sistemas_y_compases


class Command(BaseCommand):
    help = (
        "Corre la detección OpenCV de sistemas/compases sobre una página de una "
        "Partitura ya subida, y guarda un PNG de diagnóstico con lo detectado "
        "dibujado encima. No persiste Sistema/Compas — es solo para validar "
        "visualmente la calidad de la detección."
    )

    def add_arguments(self, parser):
        parser.add_argument('partitura_id', type=int)
        parser.add_argument('numero_pagina', type=int, nargs='?', default=1)
        parser.add_argument('--dpi', type=int, default=300)
        parser.add_argument('--out', type=str, default=None)

    def handle(self, *args, **options):
        try:
            partitura = Partitura.objects.get(pk=options['partitura_id'])
        except Partitura.DoesNotExist:
            raise CommandError(f"No existe Partitura {options['partitura_id']}")

        numero_pagina = options['numero_pagina']
        img = rasterizar_pagina(partitura.archivo_original.path, numero_pagina, dpi=options['dpi'])
        h, w = img.shape[:2]

        sistemas = detectar_sistemas_y_compases(img)

        overlay = img.copy()
        total_compases = 0
        for sistema in sistemas:
            y0 = int(sistema['y'] * h)
            y1 = int((sistema['y'] + sistema['height']) * h)
            cv2.rectangle(overlay, (0, y0), (w - 1, y1), (255, 0, 0), 2)
            for bx in sistema['barras_x']:
                x = int(bx * w)
                cv2.line(overlay, (x, y0), (x, y1), (0, 0, 255), 2)
            total_compases += len(sistema['compases'])

        out_path = options['out'] or f"diagnostico_p{partitura.pk}_pag{numero_pagina}.png"
        cv2.imwrite(out_path, overlay)

        self.stdout.write(self.style.SUCCESS(
            f"{len(sistemas)} sistema(s), {total_compases} compás(es) detectados. "
            f"Diagnóstico guardado en {out_path}"
        ))
