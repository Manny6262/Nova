$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.pipecat-venv/Scripts/python.exe')) {
    uv venv --python 3.11 .pipecat-venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create voice environment' }
}
uv pip install --python .pipecat-venv/Scripts/python.exe -r requirements-pipecat.txt
if ($LASTEXITCODE -ne 0) { throw 'Could not install voice dependencies' }
