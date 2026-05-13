$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location (Join-Path $root "service-ingestion")
$env:PYTHONPATH = $root
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
