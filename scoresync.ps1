$projectRoot = "c:\proyectos\scoresync"
$activate = "$projectRoot\venv\Scripts\Activate.ps1"
$Port = 8005

Set-Location $projectRoot

if (-not (Test-Path $activate)) {
    Write-Error "No se encontró el venv en $projectRoot\venv"
    exit 1
}

. $activate

# Abre el navegador después de 2 segundos (cuando el servidor ya levantó)
$null = Start-Job -ScriptBlock { param($port) Start-Sleep 2; Start-Process "http://localhost:$port" } -ArgumentList $Port

python manage.py runserver $Port
