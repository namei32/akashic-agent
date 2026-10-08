param([string]$Root=(Join-Path $env:LOCALAPPDATA 'Akashic\lol-tracker'))
$ErrorActionPreference='Stop'
New-Item -ItemType Directory -Force -Path $Root | Out-Null
$scriptPath=Join-Path $Root 'collect.ps1'
if ([IO.Path]::GetFullPath($scriptPath) -ne [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'collect.ps1'))) {
    # Windows PowerShell 5 reads non-ASCII scripts correctly with a UTF-8 BOM.
    [IO.File]::WriteAllText($scriptPath, [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'collect.ps1')), [Text.UTF8Encoding]::new($true))
}
$action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "'+$scriptPath+'" -Root "'+$Root+'"')
$repeat=New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$login=New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
$principal=New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType S4U -RunLevel Limited
$settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'Akashic LoL Collector' -Action $action -Trigger @($repeat,$login) -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName 'Akashic LoL Collector'
Get-ScheduledTask -TaskName 'Akashic LoL Collector' | Select-Object TaskName,State
