using System.Runtime.InteropServices;
namespace NovaNative;
internal static class VoiceStartupError
{
    public static string Describe(Exception error)=>error switch
    {
        COMException=>error.Message,
        OperationCanceledException=>"Voice startup timed out. Click Retry voice. If it repeats, check that Windows has a default microphone and speaker.",
        DllNotFoundException=>"The Windows AEC bridge could not load. Restart Nova and click Retry voice; report code 0x"+error.HResult.ToString("X8")+" if it repeats.",
        _=>"Voice startup failed ("+error.GetType().Name+", 0x"+error.HResult.ToString("X8")+"). Click Retry voice; dictation remains available."
    };
}
