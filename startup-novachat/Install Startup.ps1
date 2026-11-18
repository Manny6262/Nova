param(
    [string]$AppDirectory = 'C:\NovaChat',
    [string]$Pythonw = 'C:\Python314\pythonw.exe'
)
$ErrorActionPreference = 'Stop'
foreach ($path in @($Pythonw, (Join-Path $AppDirectory 'desktop.py'), (Join-Path $AppDirectory 'wake_service.py'))) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing required file: $path" }
}
$startup = [Environment]::GetFolderPath('Startup')
$shell = New-Object -ComObject WScript.Shell
foreach ($entry in @(@{ Name = 'Nova'; Script = 'desktop.py' }, @{ Name = 'Nova Wake'; Script = 'wake_service.py' })) {
    $shortcut = $shell.CreateShortcut((Join-Path $startup ($entry.Name + '.lnk')))
    $shortcut.TargetPath = $Pythonw
    $shortcut.Arguments = '"' + (Join-Path $AppDirectory $entry.Script) + '"'
    $shortcut.WorkingDirectory = $AppDirectory
    $shortcut.Save()
}
Write-Output 'Created Nova and Nova Wake shortcuts for the current Windows user.'
