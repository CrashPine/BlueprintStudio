# Hit the local FastAPI server (start it first).
# Usage:
#   # terminal 1
#   python -m uvicorn src.api:app --reload --host 127.0.0.1 --port 8000
#   # terminal 2
#   .\scripts\api_smoke.ps1

$Base = "http://127.0.0.1:8000"
$ErrorActionPreference = "Stop"

function Get-Json($url) {
    Invoke-RestMethod -Uri $url -Method Get
}

function Post-Json($url, $body) {
    Invoke-RestMethod -Uri $url -Method Post -ContentType "application/json" -Body ($body | ConvertTo-Json -Depth 20)
}

Write-Host "GET /health"
Get-Json "$Base/health" | ConvertTo-Json

Write-Host "`nGET /compliance/pue"
Get-Json "$Base/compliance/pue" | ConvertTo-Json -Depth 5

Write-Host "`nPOST /loads (demo datacentre graph)"
$graph = Get-Json "$Base/demo-datacentre"
Post-Json "$Base/loads" @{ graph = $graph } | ConvertTo-Json -Depth 5

Write-Host "`nPOST /parse (floor plan 13 — needs API key + may take ~30s)"
$img = Resolve-Path "data/raw_floorplans/13.png"
$form = @{
    file         = Get-Item $img
    diagram_type = "FLOORPLAN"
    engine       = "claude"
}
try {
    $r = Invoke-RestMethod -Uri "$Base/parse" -Method Post -Form $form
    Write-Host "Parsed spaces:" ($r.spaces.Count) "nodes:" ($r.nodes.Count)
    ($r | ConvertTo-Json -Depth 6).Substring(0, [Math]::Min(800, ($r | ConvertTo-Json -Depth 6).Length))
    Write-Host "..."
} catch {
    Write-Warning "POST /parse failed (check API key and server logs): $_"
}

Write-Host "`nDone. Swagger UI: $Base/docs"
