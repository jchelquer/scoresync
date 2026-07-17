"""
Agrega una entrada al tope de changelog.yaml con los datos del último commit.
Editar el archivo después para ajustar versión, tipo y texto.

Uso:  python add_changelog.py
"""

import re
import subprocess
from pathlib import Path

CHANGELOG = Path(__file__).parent / "changelog.yaml"

result = subprocess.run(
    ["git", "log", "-1", "--format=%s|%as"],
    capture_output=True, text=True, check=True
)
subject, date = result.stdout.strip().split("|")

# Extraer versión del inicio del mensaje (ej. "V7.5 Historial..." → "7.5")
match = re.match(r"[Vv](\S+)\s*(.*)", subject)
if match:
    version = match.group(1).rstrip(".")
    description = match.group(2).strip() or subject
else:
    version = "x.x"
    description = subject

new_entry = (
    f'- version: "{version}"\n'
    f'  date: "{date}"\n'
    f'  changes:\n'
    f'    - type: nuevo\n'
    f'      text: "{description}"\n'
)

lines = CHANGELOG.read_text(encoding="utf-8").splitlines(keepends=True)

# Insertar después del bloque de comentarios iniciales
insert_at = next(
    (i for i, l in enumerate(lines) if l.startswith("- version:")),
    len(lines)
)
lines.insert(insert_at, new_entry)
CHANGELOG.write_text("".join(lines), encoding="utf-8")

print(f"Entrada agregada: v{version} — {description}")
print(f"Editá {CHANGELOG.name} para ajustar.")
