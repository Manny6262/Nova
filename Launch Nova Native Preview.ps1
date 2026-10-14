$ErrorActionPreference = 'Stop'
$base = Split-Path -Parent $MyInvocation.MyCommand.Path
$engineDir = [IO.Path]::GetFullPath((Join-Path $base 'engine')).TrimEnd('\')
$engineUrl = 'http://127.0.0.1:5100'

function Test-NativeEngine {
    try {
        $identity = Invoke-RestMethod -Uri ($engineUrl + '/native-host') -TimeoutSec 2
        if (-not $identity.native) { return $false }
        $actual = [IO.Path]::GetFullPath([string]$identity.engine_path).TrimEnd('\')
        if ($actual -ine $engineDir) { return $false }
        $null = Invoke-WebRequest -Uri ($engineUrl + '/health') -TimeoutSec 3 -UseBasicParsing
        return $true
    } catch { return $false }
}

if (-not (Test-Path -LiteralPath (Join-Path $engineDir 'app.py'))) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show('Nova Native engine files are missing. Reinstall Nova Native.', 'Nova Native')
    exit 1
}

$ready = Test-NativeEngine
if (-not $ready) {
    $python = if ($env:NOVA_PYTHON) { $env:NOVA_PYTHON } else { 'C:\Python314\python.exe' }
    if (-not (Test-Path -LiteralPath $python)) {
        $command = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($command) { $python = $command.Source }
    }
    if (-not (Test-Path -LiteralPath $python)) {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show('Python is unavailable, so Nova Native could not start its local engine.', 'Nova Native')
        exit 1
    }
    $hostScript = Join-Path $base 'backend_host.py'
    $process = Start-Process -FilePath $python -ArgumentList @('"' + $hostScript + '"', '--port', '5100') `
        -WorkingDirectory $base -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $base 'engine-startup.out.log') `
        -RedirectStandardError (Join-Path $base 'engine-startup.err.log')
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        Start-Sleep -Milliseconds 500
        if (Test-NativeEngine) { $ready = $true; break }
        if ($process.HasExited) { break }
    }
}

if (-not $ready) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show('Nova Native engine did not start. Check engine-startup.err.log in the installation folder.', 'Nova Native')
    exit 1
}

$photonConfig = Join-Path $env:LOCALAPPDATA 'NovaNative\photon.json'
$photonBridge = Join-Path $base 'photon\bridge.mjs'
if ((Test-Path -LiteralPath $photonConfig) -and (Test-Path -LiteralPath $photonBridge)) {
    $existingBridge = Get-CimInstance Win32_Process -Filter "Name = 'node.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($photonBridge) }
    if (-not $existingBridge) {
        $node = Get-Command node.exe -ErrorAction SilentlyContinue
        if ($node) {
            Start-Process -FilePath $node.Source -ArgumentList @('"' + $photonBridge + '"') `
                -WorkingDirectory (Split-Path -Parent $photonBridge) -WindowStyle Hidden `
                -RedirectStandardOutput (Join-Path $base 'photon.out.log') `
                -RedirectStandardError (Join-Path $base 'photon.err.log') | Out-Null
        }
    }
}

$app = Join-Path $base 'app\NovaNative.exe'
$env:NOVA_ENGINE_URL = $engineUrl
Start-Process -FilePath $app -WorkingDirectory (Split-Path -Parent $app)
