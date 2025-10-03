<#
Launch wrapper for Claude Desktop (stdio).

This script loads `.env` from the repo root into the process environment (without writing secrets
to disk) and then execs `python server.py` in the same process so the calling program (Claude Desktop)
can communicate with the MCP server over stdio.

Usage (Claude should run this as the command):
  powershell -NoProfile -ExecutionPolicy Bypass -File "C:\path\to\repo\scripts\claude-launch.ps1"
#>

Push-Location -Path (Split-Path -Parent $MyInvocation.MyCommand.Definition)
Set-Location ..

if (Test-Path .env) {
    Get-Content .env | ForEach-Object {
        $line = $_
        if ($line -match "^\s*#") { return }
        if ($line -match "^\s*$") { return }
        $parts = $line -split "=", 2
        if ($parts.Count -eq 2) {
            $k = $parts[0].Trim()
            $k = $k -replace '^[\uFEFF\u200B]',''
            $v = $parts[1].Trim()
            if ($v.StartsWith('"') -and $v.EndsWith('"')) { $v = $v.Substring(1, $v.Length - 2) }
            if ($v.StartsWith("'") -and $v.EndsWith("'")) { $v = $v.Substring(1, $v.Length - 2) }
            [System.Environment]::SetEnvironmentVariable($k, $v, 'Process')
        }
    }
}

# Exec python server.py in-place so stdout/stdin are connected to the calling process (Claude)
& "C:\Python310\python.exe" server.py

Pop-Location
