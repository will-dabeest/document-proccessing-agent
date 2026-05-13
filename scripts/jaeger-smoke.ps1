$ErrorActionPreference = "Stop"
try {
  $r = Invoke-WebRequest -Uri "http://localhost:16686" -UseBasicParsing -TimeoutSec 5
  Write-Host "Jaeger UI OK status:" $r.StatusCode
} catch {
  Write-Warning "Jaeger not reachable at http://localhost:16686 — start infra/docker-compose.yml"
  exit 1
}
