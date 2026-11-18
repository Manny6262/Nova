# NovaChat: the Windows startup version

Source snapshot from `C:\NovaChat` on November 17, 2026. The installed Windows
startup shortcuts target `desktop.py` and `wake_service.py`, both using
`C:\Python314\pythonw.exe` with working directory `C:\NovaChat`.
This folder preserves that version separately from Nova Native at the repository root.
Application source files are copied unchanged.

## Restore on Windows

1. Copy this folder's contents to `C:\NovaChat`. The original app contains absolute
   paths, so retaining this location and Python location avoids changing its code.
2. Install Python 3.14 at `C:\Python314` and install the packages in
   `requirements.txt`. This requirements list is taken from the existing Nova Native
   repository as a dependency starting point, not a verified lock of the original
   environment. Some speech packages may need a compatible Python environment.
3. Install Ollama and pull the models you want. The fast voice default is
   `qwen2.5:3b`. Install and sign into Codex separately for the Codex integration.
4. Restore the local speech assets listed in `MODEL_FILES.json` to their matching
   relative paths. They are not uploaded to this source repository. Copy them from
   the original `C:\NovaChat` before erasing or replacing the machine. Whisper also
   needs its cached/downloaded `distil-small.en` model. The optional Pipecat worker
   uses a separate Python 3.11 environment; see `PIPECAT_VOICE.md` and
   `setup-pipecat.ps1`.
5. Test `C:\Python314\python.exe C:\NovaChat\desktop.py` and then test
   `C:\Python314\python.exe C:\NovaChat\wake_service.py` with the desktop closed.
6. Run `Install Startup.ps1` to recreate the two shortcuts for the current user.
   They run at Windows sign-in, rather than before a user signs in.

The optional image/video engine lives outside this folder at `C:\NovaMedia` and
must be migrated separately. Ollama model weights are also outside this folder.
This snapshot has not been installed or tested on the destination server.

## What is deliberately separate

`nova.db` (conversations, memory and tasks), settings, workspace selections, account
credentials, logs, generated media, backups, virtual environments, and unrelated
trading projects are not published. To retain personal state, transfer it privately
from the old installation while Nova is stopped; never commit it to this public repo.
This upload is a source backup, not a complete disk or personal-data backup.

`SOURCE_FILES.json` records SHA-256 hashes of the copied source files for verification.
`MODEL_FILES.json` records local speech assets and their hashes for a later transfer.
