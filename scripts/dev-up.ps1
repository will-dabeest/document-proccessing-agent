$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $root "infra\docker-compose.yml"

Write-Host "Starting full stack (LocalStack, Terraform, ingestion, worker, frontend)..."
docker compose -f $composeFile up --build -d

Write-Host "Services are starting. UI: http://localhost:5173  API: http://localhost:8000  Jaeger: http://localhost:16686"
Write-Host "Logs: docker compose -f infra/docker-compose.yml logs -f"
