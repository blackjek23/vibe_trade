<#
.SYNOPSIS
  Import a prod DB snapshot (copied from the working bot) into a SANDBOX file.

.DESCRIPTION
  Lands the snapshot as data\vibe_trade_prod_<yyyyMMdd>.db so it NEVER touches
  your real data\vibe_trade.db. Optionally verifies the sha256 produced by
  export_db.sh, then runs scripts\verify_db.py (integrity + row counts).

.EXAMPLE
  scripts\import_prod_db.ps1 -Source "D:\usb\vibe_trade-20260613-140500.db"

.EXAMPLE
  scripts\import_prod_db.ps1 -Source "D:\usb\vibe_trade-...db" -Sha256 "D:\usb\vibe_trade-...db.sha256"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Source,

    # Either a sha256 hex string or a path to the .sha256 file from export_db.sh.
    [string]$Sha256
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$dataDir  = Join-Path $repoRoot "data"

if (-not (Test-Path -LiteralPath $Source)) {
    throw "Source file not found: $Source"
}
if (-not (Test-Path -LiteralPath $dataDir)) {
    New-Item -ItemType Directory -Path $dataDir | Out-Null
}

# --- optional checksum verification -----------------------------------------
if ($Sha256) {
    $expected = $Sha256.Trim()
    if (Test-Path -LiteralPath $expected) {
        # A .sha256 file: "<hash>  <filename>" — take the first whitespace token.
        $expected = ((Get-Content -LiteralPath $expected -Raw).Trim() -split '\s+')[0]
    }
    $actual = (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash
    if ($actual -ieq $expected) {
        Write-Host "sha256: OK ($actual)" -ForegroundColor Green
    }
    else {
        throw "sha256 MISMATCH`n  expected: $expected`n  actual:   $actual"
    }
}

# --- pick a non-clobbering destination --------------------------------------
$stamp = Get-Date -Format "yyyyMMdd"
$dest  = Join-Path $dataDir "vibe_trade_prod_$stamp.db"
$n = 2
while (Test-Path -LiteralPath $dest) {
    $dest = Join-Path $dataDir "vibe_trade_prod_${stamp}_$n.db"
    $n++
}

Copy-Item -LiteralPath $Source -Destination $dest
Write-Host "Imported -> $dest" -ForegroundColor Cyan

# --- verify with the shared Python checker ----------------------------------
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$verify = Join-Path $PSScriptRoot "verify_db.py"
if (Test-Path -LiteralPath $python) {
    Write-Host ""
    & $python $verify $dest
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "verify_db.py reported a problem (exit $LASTEXITCODE)."
    }
}
else {
    Write-Warning "venv python not found at $python — skipping verify. Run manually:`n  python scripts\verify_db.py `"$dest`""
}

Write-Host ""
Write-Host "Work on it with, e.g.:" -ForegroundColor DarkGray
Write-Host "  .venv\Scripts\python -m vibe_trade status --db `"$dest`"" -ForegroundColor DarkGray
