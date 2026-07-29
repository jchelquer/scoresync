# po2mo.ps1
# Compila todos los .po del proyecto a .mo usando polib (no requiere gettext instalado).
# Uso: .\po2mo.ps1

$projectRoot = "c:\proyectos\scoresync"
$activate = "$projectRoot\venv\Scripts\Activate.ps1"

Set-Location $projectRoot

if (-not (Test-Path $activate)) {
    Write-Error "No se encontró el venv en $projectRoot\venv"
    exit 1
}

. $activate

$script = @'
import sys
from pathlib import Path
import polib

archivos = [p for p in Path(".").rglob("*.po") if "venv" not in p.parts]

if not archivos:
    print("No se encontraron archivos .po")
    sys.exit(0)

for po_path in archivos:
    mo_path = po_path.with_suffix(".mo")
    po = polib.pofile(str(po_path))
    po.save_as_mofile(str(mo_path))
    print(f"OK  {po_path} -> {mo_path}")
'@

$script | python -

Write-Host "Listo." -ForegroundColor Green
