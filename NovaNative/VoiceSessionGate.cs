namespace NovaNative;
internal static class VoiceSessionGate
{
    public static bool IsCurrent(int captured, int current, bool enabled) => enabled && captured == current;
    public static bool CanSwitch(bool busy, bool transcribing, bool recording) => !busy && !transcribing && !recording;
}
