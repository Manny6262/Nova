using System.Text.RegularExpressions;
namespace NovaNative;
internal sealed class VoicePlaybackGuard
{
    private readonly Queue<(string Text, long Time)> recent = new();
    private readonly HashSet<int> consumed = new();
    private static string Normalize(string text) => Regex.Replace(text.ToLowerInvariant(), @"[^\p{L}\p{Nd}]+", " ").Trim();
    public void Remember(string text, long milliseconds)
    {
        Prune(milliseconds); var normalized = Normalize(text);
        if (normalized.Split(' ', StringSplitOptions.RemoveEmptyEntries).Length >= 5) recent.Enqueue((normalized, milliseconds));
        while (recent.Count > 20) recent.Dequeue();
    }
    private void Prune(long now) { while (recent.Count > 0 && now - recent.Peek().Time > 12000) recent.Dequeue(); }
    public bool IsRecentPlayback(string transcript, long milliseconds)
    {
        Prune(milliseconds); var text = Normalize(transcript);
        if (text.Split(' ', StringSplitOptions.RemoveEmptyEntries).Length < 5) return false;
        return recent.Any(item => (" " + item.Text + " ").Contains(" " + text + " "));
    }
    public bool Consume(int sequence)
    {
        if (!consumed.Add(sequence)) return false;
        if(consumed.Count > 512) { var newest=consumed.Max();consumed.RemoveWhere(n=>n<newest-128); }
        return true;
    }
    public void Clear() { recent.Clear(); consumed.Clear(); }
}
