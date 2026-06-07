# Local smoke tests for v3 cherry-pick (no API calls unless -LiveParse).
# Usage (from repo root, venv activated):
#   .\scripts\test_local.ps1
#   .\scripts\test_local.ps1 -LiveParse          # also call Claude on floor plan 13
#   .\scripts\test_local.ps1 -SkipDatacenter     # skip DC re-parse (needs API key)

param(
    [switch]$LiveParse,
    [switch]$SkipDatacenter
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

Step "1. Offline checks (schema, enrichment, PUE/loads, visualizer)"
python scripts/test_local.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Step "2. Re-render floor-plan overlay (v3 visualizer)"
python scripts/visualize.py `
    data/parsed_floor_13.json `
    data/raw_floorplans/13.png `
    --out data/overlay_13_v3.png
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "Wrote data/overlay_13_v3.png"

Step "3. Re-render datacenter overlays from cached JSON (no API)"
$dcPairs = @(
    @("data/parsed_dc_datahall.json",   "data/datacenter/preview.webp",       "data/overlay_dc_datahall_v3.png"),
    @("data/parsed_dc_serverroom.json", "data/datacenter/preview (2).webp",   "data/overlay_dc_serverroom_v3.png"),
    @("data/parsed_dc_cooling.json",   "data/datacenter/preview (1).webp",   "data/overlay_dc_cooling_v3.png")
)
foreach ($p in $dcPairs) {
    python scripts/visualize.py $p[0] $p[1] --out $p[2]
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "Wrote $($p[2])"
}

if (-not $SkipDatacenter) {
    Step "4. Datacenter live parse (CV-hybrid + Claude; needs API key)"
    python scripts/parse_datacenter.py --all
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if ($LiveParse) {
    Step "5. Live Claude floor-plan parse (needs API key)"
    python src/07_claude_parse.py data/raw_floorplans/13.png `
        --type FLOORPLAN `
        --out data/parsed_floor_13_live.json
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    python scripts/visualize.py `
        data/parsed_floor_13_live.json `
        data/raw_floorplans/13.png `
        --out data/overlay_13_live.png
}

Step "Done"
Write-Host @"

Next: start the API on localhost (separate terminal):
  python -m uvicorn src.api:app --reload --host 127.0.0.1 --port 8000

Then open:
  http://127.0.0.1:8000/docs

Quick API checks (PowerShell):
  Invoke-RestMethod http://127.0.0.1:8000/health
  Invoke-RestMethod http://127.0.0.1:8000/compliance/pue

"@ -ForegroundColor Green
