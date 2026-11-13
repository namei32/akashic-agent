param(
    [ValidateRange(1024, 65535)][int]$Port = 45873,
    [switch]$Check,
    [switch]$Wait,
    [string]$CliPath = "",
    [string]$CodexHome = "",
    [string]$StateDirectory = ""
)
$ErrorActionPreference = 'Stop'
$cli = $CliPath
if (!$cli) { $cli = (Get-Command codex.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source }
if (!$cli) {
    $bins = Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin'
    $cli = (Get-ChildItem -Path $bins -Filter codex.exe -Recurse -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
}
if (!$cli) {
    $package = Get-AppxPackage OpenAI.Codex | Select-Object -First 1
    if ($package) { $cli = Join-Path $package.InstallLocation 'app\resources\codex.exe' }
}
if (!$cli -or !(Test-Path -LiteralPath $cli -PathType Leaf)) { throw 'Windows native codex.exe was not found.' }
$cli = [IO.Path]::GetFullPath($cli)
$running = @(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if (!$StateDirectory) { $StateDirectory = Join-Path $env:LOCALAPPDATA 'AkashicCodexControl' }
$receipt = Join-Path $StateDirectory 'backend.json'
$effectiveHome = if ($CodexHome) { $CodexHome } elseif ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$effectiveHome = [IO.Path]::GetFullPath($effectiveHome)
if ($Check) {
    [pscustomobject]@{ mode='windows-cli'; listenerPresent=($running.Count -gt 0); codexHome=$effectiveHome;
        endpoint="ws://127.0.0.1:$Port"; cli=$cli; receipt=$receipt } | ConvertTo-Json -Compress
    exit 0
}
New-Item -ItemType Directory -Path $StateDirectory -Force | Out-Null
$oldHome = $env:CODEX_HOME
$oldExecutionPreference = $env:PSExecutionPolicyPreference
try {
    # A launcher-only execution-policy allowance must not reach Codex child processes.
    Remove-Item Env:\PSExecutionPolicyPreference -ErrorAction SilentlyContinue
    if ($CodexHome) { $env:CODEX_HOME = $CodexHome }
    if ($running.Count -gt 0) {
        if (!(Test-Path -LiteralPath $receipt)) { throw 'The requested port is already owned by another process.' }
        $saved = Get-Content -LiteralPath $receipt -Raw | ConvertFrom-Json
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($saved.pid)"
        if (!$owner -or $owner.ExecutablePath -ne $saved.cli -or $saved.cli -ne $cli -or
            $saved.port -ne $Port -or $saved.home -ne $effectiveHome -or
            $owner.CreationDate.ToUniversalTime().ToString('o') -ne $saved.createdAt -or
            $owner.CommandLine -notmatch 'app-server' -or $running[0].OwningProcess -ne $saved.pid) {
            throw 'The saved Codex backend identity no longer matches. No existing process was stopped.'
        }
        $backend = Get-Process -Id $saved.pid
    } else {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-ffff'
        $backend = Start-Process -FilePath $cli -ArgumentList @('app-server','--listen',"ws://127.0.0.1:$Port") `
            -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $StateDirectory "$stamp.stdout.log") `
            -RedirectStandardError (Join-Path $StateDirectory "$stamp.stderr.log")
        for ($attempt=0; $attempt -lt 50; $attempt++) {
            $listener = @(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
            if ($listener.Count -gt 0 -and $listener[0].OwningProcess -eq $backend.Id) { break }
            if ($backend.HasExited) { throw "Codex backend exited; inspect its logs in $StateDirectory" }
            Start-Sleep -Milliseconds 100
        }
        if (!$listener.Count -or $listener[0].OwningProcess -ne $backend.Id) { throw 'Codex backend did not become ready.' }
        $identity = Get-CimInstance Win32_Process -Filter "ProcessId=$($backend.Id)"
        $saved = [pscustomobject]@{ pid=$backend.Id; cli=$cli; port=$Port; home=$effectiveHome;
            createdAt=$identity.CreationDate.ToUniversalTime().ToString('o'); startedAt=(Get-Date -Format o) }
        $temporary = "$receipt.$([guid]::NewGuid().ToString('N')).tmp"
        [IO.File]::WriteAllText($temporary, ($saved | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $receipt -Force
    }
    [pscustomobject]@{ mode='windows-cli'; backendPid=$saved.pid; endpoint="ws://127.0.0.1:$Port";
        receipt=$receipt } | ConvertTo-Json -Compress
    if ($Wait) { $backend.WaitForExit(); exit $backend.ExitCode }
} finally {
    $env:CODEX_HOME = $oldHome
    $env:PSExecutionPolicyPreference = $oldExecutionPreference
}
