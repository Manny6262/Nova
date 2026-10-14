$ErrorActionPreference = 'Stop'
# Run from the x64 Native Tools / Developer PowerShell environment.
$compiler = Get-Command cl.exe -ErrorAction SilentlyContinue
if (-not $compiler) { throw 'Open an x64 Visual Studio developer shell with the Windows SDK installed, then run this script.' }
& $compiler.Source /nologo /LD /EHsc /std:c++17 /MT /O2 (Join-Path $PSScriptRoot 'nova_aec.cpp') ("/Fo"+(Join-Path $PSScriptRoot 'nova_aec.obj')) /link ole32.lib uuid.lib strmiids.lib msdmo.lib ("/OUT:"+(Join-Path $PSScriptRoot 'nova_aec.dll'))
if ($LASTEXITCODE -ne 0) { throw 'AEC build failed' }
