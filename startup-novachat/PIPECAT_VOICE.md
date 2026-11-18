# Nova Pipecat voice

Start Nova with the usual desktop shortcut. Open Voice and use **Voice engine**
to choose **Pipecat — continuous conversation** or **Classic — previous voice
engine**. Changing engines closes the microphone; press Voice again to resume.
Pipecat is the default for a profile without an existing engine preference.

The separate **Voice tools** selector still controls the brain and permissions:
Realtime local uses the existing fast local model; Selected model uses the
dashboard cloud/local selection; Computer control uses Nova's existing local
control route. Selecting Pipecat does not change models or grant permissions.

## Implementation

- `pipecat_host.py`: Nova starts the isolated helper on demand, without a console.
- `pipecat_service.py`: real Pipecat 1.8.1 PipelineWorker, Silero VAD, local Smart
  Turn v3.2, Vosk partial/final transcripts, and a concurrent speech processor.
- `static/pipecat-client.js`: continuous 16 kHz microphone transport, bounded
  utterance queue, and cancellable sentence audio requests.
- `static/voice-capture-worklet.js`: capture runs on the audio rendering thread;
  older WebViews fall back to the existing PCM callback.
- The existing dashboard `/chat` adapter retains conversation history, model
  routing, streaming text, and tool execution. Its sentence chunks travel back
  through the Pipecat speech processor to the existing local Kokoro `/speak` API.
- Weak recognition and every spoken approval/interruption still go through
  Nova's existing Whisper/acoustic verification. Approval candidates carry the
  pending action token from speech start, protecting against stale approvals.
- Noise never directly aborts an action. Verified ordinary interruptions mute
  the reply and queue follow-up speech; explicit cancellation uses Nova's
  existing cancellation path. Tool work remains owned by Nova, not an audio task.

Smart Turn evaluates speech after the chosen pause. If it judges the turn
incomplete, Nova waits up to an additional 0.75 seconds (minimum total 1.4 seconds)
before the silence fallback. An utterance is capped at 30 seconds.

Browser echo cancellation and noise suppression are requested. Actual echo
removal depends on WebView2 and the audio device. Test speakers/headphones with
your own voice: synthetic audio tests cannot establish room-level performance.

## Privacy and lifecycle

The helper listens only on 127.0.0.1 (Nova port + 12, normally 5012), checks the
Nova browser origin, and requires a randomly generated process credential. It
opens no microphone itself. Audio is kept in memory, not logged or saved.
Smart Turn audio logging is explicitly disabled by the launcher.

One voice connection is allowed at a time. Exit Voice or say “stop listening”
to close input and playback. The microphone closes after 2 minutes without
accepted speech or 15 minutes per session. The helper exits after 2 minutes
without a connection or approximately 15 seconds after Nova stops responding.
Service failures release waiters and return the UI to Classic recognition.
The helper starts again on the next Pipecat voice session.

## Setup and validation

Dependencies are isolated in `C:\NovaChat\.pipecat-venv` (Python 3.11).
To install again, run `powershell -File C:\NovaChat\setup-pipecat.ps1` with uv
available. Top-level versions are pinned in `requirements-pipecat.txt`.

With Nova running:

```powershell
C:\NovaChat\.pipecat-venv\Scripts\python.exe C:\NovaChat\tests\pipecat_smoke.py
node --test C:\NovaChat\tests\pipecat-client.cjs
node --test C:\NovaChat\tests\voice-regression.cjs
```

`tests/pipecat_browser.py` requires Python Playwright with Edge installed. It
uses generated audio, mocks chat/actions/approvals, and verifies the actual
dashboard → Pipecat → Kokoro path without accessing a physical microphone.
`pipecat-service.log` contains service failures, not spoken content.

## Rollback and limitations

Choose **Classic** for immediate rollback. Original application files are saved
under `C:\NovaChat\backups\pre-pipecat-20260908`; restore only with Nova closed.

This uses a loopback PCM WebSocket adapter to preserve Nova's desktop workflow;
it is not a remote WebRTC transport. Audio is synthesized and buffered one
sentence/phrase at a time. It is not a native speech-to-speech model, and it
does not accelerate LLM generation. Follow-up instructions during running tools
are queued; it does not launch a second independent agent conversation during
the same task. History still stores the generated reply, not word-level playback
receipts. Those are separate future changes, not claims of this integration.
