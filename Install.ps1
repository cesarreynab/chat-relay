[CmdletBinding()]
param(
    [string]$Python = 'python.exe',
    [switch]$NoLaunch,
    [switch]$NoShortcuts
)

$ErrorActionPreference = 'Stop'
$pythonCommand = Get-Command $Python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $pythonCommand) {
    throw 'Instala Python 3.11 o posterior con Tcl/Tk y habilita python.exe en PATH.'
}
& $pythonCommand.Source -c 'import sys, tkinter; sys.exit(sys.version_info < (3, 11))'
if ($LASTEXITCODE -ne 0) {
    throw 'Chat Relay requiere Python 3.11 o posterior con Tcl/Tk.'
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
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo crear el entorno de Chat Relay.' }
}
& $runtime -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'No se pudieron instalar las dependencias.' }
$packageDirectory = Join-Path $appDirectory 'chat_relay'
New-Item -ItemType Directory -Path $packageDirectory -Force | Out-Null
Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'chat_relay') -File -Filter '*.py' |
    Copy-Item -Destination $packageDirectory -Force

$icon = Join-Path $installDirectory 'ChatRelay.ico'
Push-Location -LiteralPath $appDirectory
try {
    & $runtime -m chat_relay --write-icon $icon
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo generar el icono de Chat Relay.' }
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
Write-Output "Chat Relay instalado en $installDirectory"
