# Windows speaker AEC

nova_aec.dll wraps the Windows Voice Capture DSP (mfwmaaec.dll), not an amplitude filter. Native conversational voice uses source mode: Windows captures and synchronizes the default console microphone and render endpoint and returns 16 kHz mono processed PCM. It opens devices only after an explicit Voice mode click. Startup failures stop conversational voice; dictation remains separate.

Build the x64 DLL with `./Build AEC.ps1` before publishing NovaNative. The installed Visual Studio 2022 toolset and Windows SDK 10.0.26100.0 are used; no downloaded AEC package is required. The DLL statically links its C++ runtime.

`C:\Python314\python.exe -B C:\NovaNext\tests\windows_aec_regressions.py` uses filter mode and synthetic synchronized reference/microphone signals, so it does not open recording or playback devices. It tests delayed, attenuated and reflected playback echo plus distinct near-end double-talk. These tests do not validate a real room, device changes, or source-mode hardware capture.

Reference: https://learn.microsoft.com/en-us/windows/win32/medfound/voicecapturedmo
