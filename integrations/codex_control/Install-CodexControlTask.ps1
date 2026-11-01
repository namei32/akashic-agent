param([string]$CliPath = "", [switch]$Check)
$ErrorActionPreference = 'Stop'
$taskName = 'Akashic Codex Control'
$marker = 'Akashic Windows CLI control backend; no desktop process management.'
$launcher = Join-Path $PSScriptRoot 'Start-CodexControl.ps1'
$existing = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue
if ($Check) {
    if (!$existing) { [pscustomobject]@{ installed=$false } | ConvertTo-Json -Compress; exit 0 }
    $info = Get-ScheduledTaskInfo -InputObject $existing
    [pscustomobject]@{ installed=$true; state=[string]$existing.State; result=$info.LastTaskResult;
        execute=$existing.Actions.Execute; arguments=$existing.Actions.Arguments } | ConvertTo-Json -Compress
    exit 0
}
if ($existing -and $existing.Description -ne $marker) { throw 'An unrelated task has the same name; no task was replaced.' }
if ($CliPath -match '["\r\n]') { throw 'Invalid CLI path.' }
if (!(Test-Path -LiteralPath $launcher)) { throw 'The adjacent reviewed CLI launcher is missing.' }
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$arguments = '-NoProfile -ExecutionPolicy RemoteSigned -File "{0}" -Wait' -f $launcher
if ($CliPath) { $arguments += ' -CliPath "{0}"' -f ([IO.Path]::GetFullPath($CliPath)) }
$action = New-ScheduledTaskAction -Execute (Join-Path $PSHOME 'powershell.exe') -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $taskName -TaskPath '\' -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Description $marker -Force | Out-Null
Start-ScheduledTask -TaskName $taskName -TaskPath '\'
[pscustomobject]@{ installed=$true; task=$taskName; user=$user; launcher=$launcher } | ConvertTo-Json -Compress
