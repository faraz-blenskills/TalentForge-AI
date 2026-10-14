# Copies the app's settings from .env to the linked Vercel project (Production) and creates the two
# generated secrets. Values are piped straight into `vercel env add` and are never printed.
#
#   cd talentforge
#   vercel login ; vercel link
#   powershell -ExecutionPolicy Bypass -File scripts\vercel_env.ps1

# Windows PowerShell 5.1 turns anything a native program writes to stderr into an error; the Vercel CLI
# prints its banner there, so we judge success by exit codes instead.
param([switch]$RotateSecrets)   # -RotateSecrets regenerates TALENTFORGE_SECRET / CRON_SECRET (signs everyone out)
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".vercel\project.json")) { throw "Run 'vercel link' in $root first." }
if (-not (Test-Path ".env")) { throw ".env not found in $root" }

# parse KEY=VALUE lines of .env
$cfg = @{}
foreach ($line in Get-Content ".env") {
    $t = $line.Trim()
    if ($t -eq "" -or $t.StartsWith("#") -or -not $t.Contains("=")) { continue }
    $k, $v = $t.Split("=", 2)
    $v = $v.Trim()
    if ($v.Length -ge 2 -and ($v.StartsWith('"') -and $v.EndsWith('"') -or $v.StartsWith("'") -and $v.EndsWith("'"))) {
        $v = $v.Substring(1, $v.Length - 2)
    }
    $cfg[$k.Trim()] = $v
}

function Invoke-Vercel {
    # use a globally installed CLI when there is one, otherwise run it through npx.
    # $input must be forwarded explicitly: a native command inside a function does not receive it otherwise.
    if (Get-Command vercel -ErrorAction SilentlyContinue) { $input | & vercel @args }
    else { $input | & npx --yes vercel @args }
}

function Add-VercelEnv([string]$Name, [string]$Value, [bool]$Force, [bool]$Sensitive) {
    $cliArgs = @("env", "add", $Name, "production")
    if ($Force) { $cliArgs += "--force" }
    if ($Sensitive) { $cliArgs += "--sensitive" }
    $Value | Invoke-Vercel @cliArgs *> $null
    if ($LASTEXITCODE -eq 0) { Write-Host "  set    $Name" } else { Write-Host "  kept   $Name (already exists or failed)" }
}

Write-Host "Copying settings from .env to Vercel (Production):"
$secretKeys = @("DATABASE_URL", "ANTHROPIC_API_KEY", "ADZUNA_APP_KEY", "RAPIDAPI_KEY")
$copyKeys = @("DATABASE_URL", "SUPABASE_URL", "SUPABASE_ANON_KEY", "ALLOW_PASSWORD_LOGIN",
              "ANTHROPIC_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY", "ADZUNA_COUNTRY", "RAPIDAPI_KEY")
foreach ($k in $copyKeys) {
    if ($cfg.ContainsKey($k) -and $cfg[$k] -ne "") {
        Add-VercelEnv $k $cfg[$k] $true ($secretKeys -contains $k)
    }
}
if (-not $cfg.ContainsKey("DATABASE_URL") -or $cfg["DATABASE_URL"] -eq "") {
    Write-Warning "DATABASE_URL is empty in .env - the deployed app would fall back to a throwaway database."
}

function New-Secret { $b = New-Object byte[] 48; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); [Convert]::ToBase64String($b) }
Write-Host "Generated secrets (created only if they do not exist yet, so sessions are not reset on re-runs):"
Add-VercelEnv "TALENTFORGE_SECRET" (New-Secret) $RotateSecrets.IsPresent $true
Add-VercelEnv "CRON_SECRET" (New-Secret) $RotateSecrets.IsPresent $true

Write-Host "Done. Deploy with:  vercel deploy --prod"
