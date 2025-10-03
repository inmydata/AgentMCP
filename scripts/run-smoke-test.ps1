<#
PowerShell wrapper to run the smoke test.
Prompts for required INMYDATA_* variables, sets them in the current process, and runs the Python smoke test.
#>

param(
    [switch]$UseEnvFile
)

function Read-Secret([string]$prompt) {
    # Avoid interpolation issues with ':' inside double-quoted strings
    Write-Host -NoNewline ($prompt + ': ') -ForegroundColor Yellow
    $s = Read-Host -AsSecureString
    return [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
}

if ($UseEnvFile) {
    if (-Not (Test-Path .env)) {
        Write-Host ".env file not found in repo root." -ForegroundColor Red
        exit 1
    }
    # Load .env into process env (robust parser)
    $setKeys = @()
    Get-Content .env | ForEach-Object {
        $line = $_
        # skip comments and blank lines
        if ($line -match "^\s*#") { return }
        if ($line -match "^\s*$") { return }

        $parts = $line -split "=", 2
        if ($parts.Count -eq 2) {
            # strip BOM on first key (if present), trim whitespace, and remove surrounding quotes
            $k = $parts[0].Trim()
            $k = $k -replace '^[\uFEFF\u200B]',''
            $v = $parts[1].Trim()
            # remove surrounding single/double quotes if present
            if ($v.StartsWith('"') -and $v.EndsWith('"')) { $v = $v.Substring(1, $v.Length - 2) }
            if ($v.StartsWith("'") -and $v.EndsWith("'")) { $v = $v.Substring(1, $v.Length - 2) }

            [System.Environment]::SetEnvironmentVariable($k, $v, 'Process')
            $setKeys += $k
        }
    }
    if ($setKeys.Count -gt 0) {
        Write-Host "Loaded .env into process environment (not written to disk). Set variables: $($setKeys -join ', ')"
    } else {
        Write-Host "No variables found in .env" -ForegroundColor Yellow
    }
} else {
    $apiKey = Read-Host "INMYDATA_API_KEY (paste)"
    $tenant = Read-Host "INMYDATA_TENANT"
    $calendar = Read-Host "INMYDATA_CALENDAR"
    $user = Read-Host "INMYDATA_USER (optional)"
    $session = Read-Host "INMYDATA_SESSION_ID (optional)"

    [System.Environment]::SetEnvironmentVariable('INMYDATA_API_KEY', $apiKey, 'Process')
    [System.Environment]::SetEnvironmentVariable('INMYDATA_TENANT', $tenant, 'Process')
    [System.Environment]::SetEnvironmentVariable('INMYDATA_CALENDAR', $calendar, 'Process')
    if ($user -and $user.Trim() -ne '') { [System.Environment]::SetEnvironmentVariable('INMYDATA_USER', $user, 'Process') }
    if ($session -and $session.Trim() -ne '') { [System.Environment]::SetEnvironmentVariable('INMYDATA_SESSION_ID', $session, 'Process') }
}

Write-Host "Running smoke test..." -ForegroundColor Green
python scripts/smoke_test.py
