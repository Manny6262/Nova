# Publication review

Prepared 2026-10-14 from the active Nova Native 1.0.8 source, as a separate copy.
The original source, installed application, and personal state were not edited.

## Included

WinUI/C# source, the native C++ AEC wrapper, Python engine modules, browser
compatibility assets, application artwork, optional Photon bridge source and
lockfile, isolated tests, example configuration, and reproducible setup notes.
`PUBLIC_FILES.json` records the exact reviewed file list used for source export.

## Excluded

SQLite databases and chat/memory history; active configuration and OAuth/Photon
connections; phone/account state; workspaces and task captures; generated media,
audio and personal screenshots; backups and diagnostic logs; compiled packages,
SDKs and dependency environments; model weights; historical copies of source;
and the installation-specific Native-build baseline ZIP.

## Adjustments made only in this public copy

Personal names and profile paths were generalized. Optional tool permissions
start off. The compatibility browser view's personal full-control grant was
removed. Runtime locations can be set explicitly. Migration is opt-in and uses
example configuration for a fresh installation. The local Native-build demo menu
entry is omitted because its reviewed private baseline is not distributed.
The AEC build uses an x64 Visual Studio developer environment instead of a fixed
compiler installation path.
The optional bridge lockfile overrides were updated for OpenTelemetry core and
protobufjs. Pillow and pypdf pins were updated after advisory checks; none of
these dependency changes were made in the working installation.

## Validation

Checks completed on the selected source snapshot:

- 73 isolated Python tests and two bridge helper tests passed; local-agent
  milestone assertions also passed.
- The Windows x64 native app published successfully from this source.
- The C++ echo-cancellation wrapper compiled; synthetic audio checks passed
  with 22.78 dB echo reduction and 0.936 near-end correlation.
- The updated bridge SDK imports and image/PDF dependency smoke checks passed.
- Python, JSON, XML, and PowerShell source parsing passed.
- Gitleaks and the additional source/privacy checks found no credentials or
  personal state in the reviewed files. The source ZIP uses only the 106-file
  publication manifest; Git history and generated build files are excluded.
- NuGet and npm audits reported no known vulnerable packages. OSV reported no
  known advisories for the 23 checked, directly pinned Python dependencies;
  this did not audit the complete optional Python dependency graph.
- The 85 selected original source files still match their pre-review SHA-256
  hashes. The working source and installed Nova were not changed.

These checks describe this snapshot, not a guarantee of complete application
security. Isolated tests do not validate every live hardware or account
integration. No messages, microphone recordings, or live model requests were
used in this review. Optional integrations still need each user's own setup.

## Publishing

Extract the source archive and upload the repository files, including `.gitignore`,
instead of uploading only a ZIP blob. Start with a new repository history. Enable
GitHub secret scanning/push protection for additional checks. A project license
has deliberately not been assigned on the owner's behalf; select one explicitly
if granting reuse rights. See the links in README and SECURITY.
