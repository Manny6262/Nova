# Optional local components

## Voice

The host looks for Vosk recognition models in `engine/vosk-model-small-en-us-0.15`,
Kokoro files in `engine/kokoro`, and a Piper fallback in `engine/voices`. Download
these from their publishers and review the license of each model before using or
redistributing it. No model weights, recordings, or transcripts are included here.

Create the optional Pipecat worker in a separate Python 3.11 environment:

```powershell
py -3.11 -m venv engine/.pipecat-venv
engine/.pipecat-venv/Scripts/python.exe -m pip install -r requirements-pipecat.txt
```

Pipecat and the recognition models must be installed before conversational voice
can work. The native AEC wrapper is built from `NovaNative/native-aec/nova_aec.cpp`
against the Windows SDK; Windows provides the Voice Capture DSP component.

## Local image and video generation

The optional ComfyUI adapter expects a portable ComfyUI installation. Set
`NOVA_MEDIA_ROOT` to its parent folder, or use the adapter's default `C:\NovaMedia`.
`engine/media_engine.py` lists the expected model filenames. Download compatible
models separately under their respective license terms. Model files and generated
results are intentionally excluded from this source release.

## Optional connected services

The optional CLI-backed provider uses the user's independently installed and
authenticated CLI. There is no saved login or API credential in this repository.
Local responsibilities and candidate tasks always use a downloaded local model.

The iMessage bridge requires your own Photon project and account connection. The
example JSON uses fictional numbers and placeholder credentials. Store real
`photon.json` only in your private installation data folder; never commit it.
Install bridge dependencies with `npm ci` inside `photon` if you need that feature.
The existing optional `photon/setup.py` helper depends on a local Hermes Photon
auth plugin; that plugin and its credentials are not redistributed here. An
unconfigured bridge remains inactive. Tests use only fictional messages.

## Public-copy differences

Personal display names and profile-path examples are generalized. First-run tool
switches are off, and the browser compatibility view does not inherit a personal
full-control grant. Migration of old state is opt-in. `NOVA_PYTHON`, `NOVA_NODE`, and
`NOVA_MEDIA_ROOT` support explicit runtime locations on another computer.
None of these adjustments changed the original working installation.
