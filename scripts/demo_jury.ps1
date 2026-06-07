# FlowDraft × ArchDraft — jury demo script (Windows PowerShell)
# Usage:  .\scripts\demo_jury.ps1
#         .\scripts\demo_jury.ps1 -SkipDocker    # app already running on :8000
#         .\scripts\demo_jury.ps1 -LiveParse   # needs API keys in .env or models/

param(
    [switch]$SkipDocker,
    [switch]$LiveParse
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$Base = "http://localhost:8000"

function Wait-Healthy {
    param([int]$Seconds = 120)
    Write-Host "Waiting for API healthcheck..." -ForegroundColor Cyan
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-RestMethod -Uri "$Base/health" -TimeoutSec 3
            if ($r.ok) {
                Write-Host "  OK: $($r.product)" -ForegroundColor Green
                return
            }
        } catch { Start-Sleep -Seconds 3 }
    }
    throw "Server did not become healthy within ${Seconds}s. Check: docker compose logs -f"
}

function Open-Tab([string]$Url) {
    Start-Process $Url
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Yellow
Write-Host "  FlowDraft — Jury Demo (~3 minutes)" -ForegroundColor Yellow
Write-Host "========================================" -ForegroundColor Yellow
Write-Host ""

if (-not $SkipDocker) {
    Write-Host "[1/6] Starting Docker..." -ForegroundColor Cyan
    docker compose up -d --build
    Wait-Healthy
} else {
    Wait-Healthy -Seconds 15
}

Write-Host "[2/6] Opening browser tabs..." -ForegroundColor Cyan
Open-Tab $Base
Start-Sleep -Seconds 2
Open-Tab "$Base/static/twin.html"
Open-Tab "$Base/docs"

Write-Host ""
Write-Host "--- PRESENTATION SCRIPT (read aloud) ---" -ForegroundColor Magenta
Write-Host @"

HOOK (15s)
  Engineering teams still work from flat PDFs and photos of floor plans.
  Manual takeoff is slow, error-prone, and compliance mistakes cost millions.

PRODUCT (20s)
  FlowDraft turns a floor-plan image into a structured building graph —
  rooms, areas, fixtures — then runs valuation and compliance on that graph.

DEMO STEP 1 — Load demo (30s)  [you are on $Base]
  Click:  Load demo
  Say:    'No API keys needed — this is our frozen F2 plan: 20 rooms,
           labeled overlay, instant room breakdown.'

DEMO STEP 2 — Overlay tab (30s)
  Click:  Overlay (if not already)
  Say:    'Each room polygon is colored by category with areas in m².'

DEMO STEP 3 — Rooms tab (25s)
  Click:  Rooms
  Say:    'Structured JSON backs everything — names, categories, areas.'

DEMO STEP 4 — Valuation (30s)
  Click:  Valuation
  Say:    'Hong Kong district tables × parsed area → property value + rental ROI.'

DEMO STEP 5 — Compliance (35s)
  Click:  Compliance  →  Run compliance check
  Say:    'TIA-942-style geometry rules run on the graph; violations surface
           with object IDs — catch issues before construction.'

DEMO STEP 6 — 3D Twin (20s)  [twin tab]
  Say:    'Same graph extruded in 3D — rooms, fixtures, MEP nodes when present.'

CLOSE (15s)
  Say:    'Parse → graph → overlay → finance → compliance — one pipeline,
           Docker-ready for judges. See HONESTY.md for what is live vs demo.'

"@ -ForegroundColor White

if ($LiveParse) {
    Write-Host "[3/6] Live parse smoke test (needs API keys)..." -ForegroundColor Cyan
    $img = Join-Path $Root "data\raw_floorplans\F2_original.png"
    if (-not (Test-Path $img)) { throw "Missing $img" }
    $boundary = [System.Guid]::NewGuid().ToString()
    $fileBytes = [System.IO.File]::ReadAllBytes($img)
    $fileEnc = [System.Text.Encoding]::GetEncoding("iso-8859-1").GetString($fileBytes)
    $bodyLines = @(
        "--$boundary",
        'Content-Disposition: form-data; name="file"; filename="F2_original.png"',
        "Content-Type: image/png",
        "",
        $fileEnc,
        "--$boundary--"
    )
    $body = $bodyLines -join "`r`n"
    try {
        $parse = Invoke-RestMethod -Uri "$Base/parse?diagram_type=FLOORPLAN&engine=cv-hybrid" `
            -Method Post -ContentType "multipart/form-data; boundary=$boundary" -Body $body
        $n = $parse.spaces.Count
        Write-Host "  Live parse: $n rooms, parser=$($parse.meta.parser)" -ForegroundColor Green
    } catch {
        Write-Host "  Live parse failed (check API keys): $_" -ForegroundColor Yellow
    }
} else {
    Write-Host "[3/6] Skipping live parse (use -LiveParse to test with API keys)" -ForegroundColor DarkGray
}

Write-Host "[4/6] Quick API checks..." -ForegroundColor Cyan
$demo = Invoke-RestMethod "$Base/demo/floorplan"
Write-Host "  Demo floorplan: $($demo.graph.spaces.Count) spaces" -ForegroundColor Green

Write-Host "[5/6] URLs for judges" -ForegroundColor Cyan
Write-Host "  Main UI:    $Base"
Write-Host "  3D Twin:    $Base/static/twin.html"
Write-Host "  Roadmap:    $Base/static/roadmap.html"
Write-Host "  API docs:   $Base/docs"
Write-Host "  Honesty:    https://github.com/CrashPine/BlueprintStudio/blob/main/HONESTY.md"

Write-Host ""
Write-Host "[6/6] Done. Stop with:  docker compose down" -ForegroundColor Green
Write-Host ""
