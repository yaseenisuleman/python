<#
.SYNOPSIS
    Builds the wheel (.whl) for one or more libraries into the root dist\ folder.

.EXAMPLE
    .\build.ps1                 # build every library in the workspace
    .\build.ps1 pytoolkit       # build one library (folder name)
    .\build.ps1 -Clean          # delete every wheel in dist\ first, then build everything

.NOTES
    dist\ is committed to git and cloned onto the Kestra hosts. -Clean also deletes
    older versions that Kestra flows may still pin to, so use it with care.
#>
param(
    [string[]]$Library,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$dist = Join-Path $root 'dist'

New-Item -ItemType Directory -Force $dist | Out-Null

if ($Clean) {
    Get-ChildItem $dist -Filter *.whl | Remove-Item -Force
}

# uv creates dist\.gitignore containing "*" when none exists, which would stop the
# wheels being committed. Keep our own copy in place so uv leaves it alone.
$gitignore = Join-Path $dist '.gitignore'
if (-not (Test-Path $gitignore) -or (Get-Content $gitignore -Raw).Trim() -eq '*') {
    Set-Content $gitignore -Encoding ascii -Value @(
        '# Built wheels in this folder are committed on purpose (Kestra hosts clone this repo).'
        '# This file stops uv from creating its own ".gitignore" containing "*" here.'
    )
}

# Default to every folder under the root that has its own pyproject.toml.
if (-not $Library) {
    $Library = Get-ChildItem $root -Directory |
        Where-Object { Test-Path (Join-Path $_.FullName 'pyproject.toml') } |
        Select-Object -ExpandProperty Name
}

foreach ($lib in $Library) {
    $path = Join-Path $root $lib
    if (-not (Test-Path (Join-Path $path 'pyproject.toml'))) {
        throw "No pyproject.toml found in '$path'"
    }
    Write-Host "Building $lib -> $dist" -ForegroundColor Cyan
    uv build $path --wheel --out-dir $dist
    if ($LASTEXITCODE -ne 0) { throw "Build failed for $lib" }
}

Get-ChildItem $dist -Filter *.whl | Select-Object Name, LastWriteTime
