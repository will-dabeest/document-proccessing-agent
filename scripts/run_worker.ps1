$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location (Join-Path $root "service-worker")
$env:PYTHONPATH = $root
python -m worker_app.worker
