param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'NovaNative'),
    [string]$MigrateFrom = '',
    [switch]$NoShortcuts
)

$ErrorActionPreference = 'Stop'
$source = Split-Path -Parent $MyInvocation.MyCommand.Path
$sourceEngine = Join-Path $source 'engine'
if (-not (Test-Path -LiteralPath (Join-Path $source 'app\NovaNative.exe'))) {
    throw 'NovaNative.exe is missing from the package.'
}
if (-not (Test-Path -LiteralPath (Join-Path $sourceEngine 'app.py'))) {
    throw 'The private Nova Native engine is missing from the package.'
}
$destination = [IO.Path]::GetFullPath($InstallRoot)
if ($destination.TrimEnd('\') -ieq ([IO.Path]::GetFullPath($source)).TrimEnd('\')) {
    throw 'Choose a different install folder from the package source.'
}
$runningNative = Get-CimInstance Win32_Process -Filter "Name = 'NovaNative.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith((Join-Path $destination 'app'), [StringComparison]::OrdinalIgnoreCase) }
if ($runningNative) {
    throw 'Close Nova Native before updating it.'
}
# Stop this installation's recovery loop before replacing engine files.
# Otherwise it could relaunch the old engine halfway through an update.
$supervisorScript = Join-Path $destination 'background_service.py'
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -in @('python.exe','pythonw.exe') -and $_.CommandLine -and
        $_.CommandLine.IndexOf($supervisorScript, [StringComparison]::OrdinalIgnoreCase) -ge 0 } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
$listener = Get-NetTCPConnection -LocalPort 5100 -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalAddress -eq '127.0.0.1' } | Select-Object -First 1
if ($listener) {
    try { $identity = Invoke-RestMethod -Uri 'http://127.0.0.1:5100/native-host' -TimeoutSec 3 }
    catch { throw 'Port 5100 is in use by another program. Close it before installing Nova Native.' }
    $actualEngine = [IO.Path]::GetFullPath([string]$identity.engine_path).TrimEnd('\')
    $expectedEngine = [IO.Path]::GetFullPath((Join-Path $destination 'engine')).TrimEnd('\')
    if (-not $identity.native -or $actualEngine -ine $expectedEngine) {
        throw 'Port 5100 belongs to another program or Nova Native installation.'
    }
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
    if (-not $owner -or $owner.CommandLine -notlike '*backend_host.py*') {
        throw 'Could not verify the running Nova Native engine process.'
    }
    Stop-Process -Id $listener.OwningProcess -Force
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if (-not (Get-NetTCPConnection -LocalPort 5100 -State Listen -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 250
    }
    if (Get-NetTCPConnection -LocalPort 5100 -State Listen -ErrorAction SilentlyContinue) {
        throw 'Nova Native engine did not stop for the update.'
    }
}

$appDestination = Join-Path $destination 'app'
$engineDestination = Join-Path $destination 'engine'
$photonDestination = Join-Path $destination 'photon'
New-Item -ItemType Directory -Path $appDestination,$engineDestination -Force | Out-Null
Copy-Item -Path (Join-Path $source 'app\*') -Destination $appDestination -Recurse -Force
if (Test-Path -LiteralPath (Join-Path $source 'photon')) {
    New-Item -ItemType Directory -Path $photonDestination -Force | Out-Null
    Copy-Item -Path (Join-Path $source 'photon\*') -Destination $photonDestination -Recurse -Force
}

$engineFiles = @(
    'app.py','chat_control.py','model_status.py','voice_turns.py','codex_bridge.py','jarvis_core.py','memory_store.py','responsibilities.py','fix_tasks.py','task_check_guard.py','native_background.py','media_engine.py','tools.py',
    'pdftext.py','pipecat_host.py','pipecat_service.py','realtime_voice.py',
    'voice_policy.py','wake_service.py','desktop.py','nova_mcp_server.py'
)
foreach ($name in $engineFiles) {
    Copy-Item -LiteralPath (Join-Path $sourceEngine $name) -Destination (Join-Path $engineDestination $name) -Force
}
foreach ($name in @('static','templates','kokoro','voices','vosk-model-small-en-us-0.15','.pipecat-venv')) {
    $from = Join-Path $sourceEngine $name
    if (Test-Path -LiteralPath $from) {
        $to = Join-Path $engineDestination $name
        New-Item -ItemType Directory -Path $to -Force | Out-Null
        Get-ChildItem -LiteralPath $from -Force | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $to -Recurse -Force
        }
    }
}
foreach ($name in @('nova_settings.json','.nova_mcp_policy.json')) {
    $target = Join-Path $engineDestination $name
    if (-not (Test-Path -LiteralPath $target)) {
        $previous = if ($MigrateFrom) { Join-Path $MigrateFrom $name } else { $null }
        if ($previous -and (Test-Path -LiteralPath $previous)) {
            Copy-Item -LiteralPath $previous -Destination $target -Force
        } else {
            Copy-Item -LiteralPath (Join-Path $sourceEngine ($name -replace '\.json$', '.example.json')) -Destination $target -Force
        }
    }
}
Copy-Item -LiteralPath (Join-Path $source 'backend_host.py') -Destination (Join-Path $destination 'backend_host.py') -Force
Copy-Item -LiteralPath (Join-Path $source 'background_service.py') -Destination (Join-Path $destination 'background_service.py') -Force
Copy-Item -LiteralPath (Join-Path $source 'migrate_native_data.py') -Destination (Join-Path $destination 'migrate_native_data.py') -Force
Copy-Item -LiteralPath (Join-Path $source 'Launch Nova Native Preview.ps1') -Destination (Join-Path $destination 'Launch Nova Native.ps1') -Force

$lastSession = Join-Path $destination 'last-session.txt'
$previewSession = Join-Path $source 'native-last-session.txt'
if (-not (Test-Path -LiteralPath $lastSession) -and (Test-Path -LiteralPath $previewSession)) {
    $oldId = (Get-Content -LiteralPath $previewSession -Raw).Trim()
    if ($oldId -match '^\d+$') { Set-Content -LiteralPath $lastSession -Value $oldId -NoNewline }
}

$python = if ($env:NOVA_PYTHON) { $env:NOVA_PYTHON } else { 'C:\Python314\python.exe' }
if (-not (Test-Path -LiteralPath $python)) {
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command) { $python = $command.Source }
}
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Python is required for Nova Native and was not found.'
}
$migrationSource = if ($MigrateFrom) { $MigrateFrom } else { Join-Path $destination '__no_migration_requested__' }
& $python (Join-Path $destination 'migrate_native_data.py') $engineDestination $migrationSource
if ($LASTEXITCODE -ne 0) { throw 'Nova Native data initialization failed.' }

if (-not $NoShortcuts) {
    $shell = New-Object -ComObject WScript.Shell
    $appExecutable = (& $python -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' (Join-Path $destination 'app\NovaNative.exe')).Trim()
    $locations = @(
        (Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) 'Nova Native.lnk'),
        (Join-Path ([Environment]::GetFolderPath('Programs')) 'Nova Native.lnk')
    )
    # Keep an existing Nova Native taskbar pin on the same native entry point.
    $pinnedNative = Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar\NovaNative.lnk'
    if (Test-Path -LiteralPath $pinnedNative) { $locations += $pinnedNative }
    foreach ($shortcutPath in $locations) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $shortcutPath) -Force | Out-Null
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $appExecutable
        $shortcut.Arguments = ''
        $shortcut.WorkingDirectory = Split-Path -Parent $appExecutable
        $shortcut.IconLocation = Join-Path (Split-Path -Parent $appExecutable) 'Assets\NovaOrb.ico'
        $shortcut.Save()
    }
}

Write-Output "Nova Native installed at $destination"
