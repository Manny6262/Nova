# Dependencies and assets

This publication contains application source and interface assets, not restored
NuGet packages, npm modules, Python environments, Windows SDK files, or model weights.

- .NET, WinUI / Windows App SDK, Windows SDK BuildTools, and NAudio are referenced
  through `NovaNative.csproj`. Restore them through the package manager and retain
  their license notices when distributing compiled releases.
- Python libraries are declared in the requirements files. Optional voice and
  media models must be obtained separately, with their individual model licenses.
- `photon/package.json` and its lockfile declare the optional Spectrum bridge.
  No third-party JavaScript implementation is vendored.
- `native-aec/nova_aec.cpp` is the application's wrapper around a Windows system
  audio component. Microsoft Windows audio binaries are not included.
- Interface artwork and icons are retained as application assets. This publication
  includes no screenshots of personal chats, browser pages, or account settings.

This notice does not grant a project license or replace any dependency's terms.
The original SDK/template acknowledgments in source files remain present.
