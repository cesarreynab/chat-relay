[CmdletBinding()]
param(
    [string]$Python = 'python.exe',
    [switch]$NoLaunch,
    [switch]$NoShortcuts
)

$ErrorActionPreference = 'Stop'
$pythonCommand = Get-Command $Python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $pythonCommand) {
    throw 'Install Python 3.11 or later with Tcl/Tk and add python.exe to PATH.'
}
& $pythonCommand.Source -c 'import sys, tkinter; sys.exit(sys.version_info < (3, 11))'
if ($LASTEXITCODE -ne 0) {
    throw 'Chat Relay requires Python 3.11 or later with Tcl/Tk.'
}

$installDirectory = if ($env:CHAT_RELAY_HOME) { $env:CHAT_RELAY_HOME } else {
    Join-Path $env:LOCALAPPDATA 'ChatRelay'
}
$appDirectory = Join-Path $installDirectory 'app'
$environmentDirectory = Join-Path $installDirectory 'venv'
New-Item -ItemType Directory -Path $appDirectory -Force | Out-Null
$runtime = Join-Path $environmentDirectory 'Scripts\python.exe'
$windowedRuntime = Join-Path $environmentDirectory 'Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $runtime)) {
    & $pythonCommand.Source -m venv $environmentDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Chat Relay virtual environment.' }
}
& $runtime -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Could not install Chat Relay dependencies.' }
$packageDirectory = Join-Path $appDirectory 'chat_relay'
New-Item -ItemType Directory -Path $packageDirectory -Force | Out-Null
Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'chat_relay') -File -Filter '*.py' |
    Copy-Item -Destination $packageDirectory -Force

$icon = Join-Path $installDirectory 'ChatRelay.ico'
Push-Location -LiteralPath $appDirectory
try {
    & $runtime -m chat_relay --write-icon $icon
    if ($LASTEXITCODE -ne 0) { throw 'Could not generate the Chat Relay icon.' }
} finally {
    Pop-Location
}
if (-not $NoShortcuts) {
    $shell = New-Object -ComObject WScript.Shell
    foreach ($shortcut in @(
        @{ Folder = [Environment]::GetFolderPath('Programs'); Arguments = '-m chat_relay --settings' }
        @{ Folder = [Environment]::GetFolderPath('Startup'); Arguments = '-m chat_relay' }
    )) {
        $link = $shell.CreateShortcut((Join-Path $shortcut.Folder 'Chat Relay.lnk'))
        $link.TargetPath = $windowedRuntime
        $link.Arguments = $shortcut.Arguments
        $link.WorkingDirectory = $appDirectory
        $link.IconLocation = "$icon,0"
        $link.Description = 'Chat Relay'
        $link.Save()
    }
}
if (-not $NoLaunch) {
    Start-Process -FilePath $windowedRuntime -ArgumentList '-m chat_relay --settings' -WorkingDirectory $appDirectory -WindowStyle Hidden
}
Write-Output "Chat Relay installed in $installDirectory"
