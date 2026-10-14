using System.Text;
namespace NovaNative;

internal sealed class VoiceReplyBuffer
{
    private readonly StringBuilder pending = new();
    public IEnumerable<string> Append(string token)
    {
        pending.Append(token);
        while (true)
        {
            var text = pending.ToString();
            var boundary = -1;
            for (int i = 0; i < text.Length; i++)
                if ((text[i] is '.' or '!' or '?' or '\n') && i >= 24 &&
                    (i + 1 == text.Length || char.IsWhiteSpace(text[i + 1]))) { boundary = i + 1; break; }
            if (boundary < 0 && text.Length >= 240)
            { boundary = text.LastIndexOf(' ', 239); if (boundary < 1) boundary = 240; }
            if (boundary < 0) yield break;
            var sentence = text[..boundary].Trim(); pending.Remove(0, boundary);
            if (sentence.Length > 0) yield return sentence;
        }
    }
    public string Finish() { var text = pending.ToString().Trim(); pending.Clear(); return text; }
}
