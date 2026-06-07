# Publish the app image to Docker Hub (run once before the jury deadline).
# Prerequisites: Docker Hub account + `docker login`
#
# Usage:
#   .\scripts\publish_docker.ps1
#   .\scripts\publish_docker.ps1 -Tag v1.0.0

param(
    [string]$Image = "crashpine/blueprintstudio",
    [string]$Tag = "latest"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$Full = "${Image}:${Tag}"
Write-Host "Building $Full ..." -ForegroundColor Cyan
docker compose build app

Write-Host "Tagging and pushing $Full ..." -ForegroundColor Cyan
docker push $Full

Write-Host ""
Write-Host "Done. Jurors can now run:" -ForegroundColor Green
Write-Host "  git clone https://github.com/CrashPine/BlueprintStudio.git"
Write-Host "  cd BlueprintStudio"
Write-Host "  docker compose pull"
Write-Host "  docker compose up"
Write-Host "  → http://localhost:8000"
