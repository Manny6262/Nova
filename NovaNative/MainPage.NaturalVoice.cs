using NAudio.Wave;
using NAudio.CoreAudioApi;
using System.Net.Http.Json;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Threading.Channels;

namespace NovaNative;

public sealed partial class MainPage
{
    private ClientWebSocket? naturalSocket;
    private CancellationTokenSource? naturalCancellation;
    private Channel<(bool Reference, byte[] Data)>? naturalFrames;
    private WindowsAecCapture? naturalAec;
    private MMDevice? naturalRenderDevice;
    private WasapiOut? naturalRenderKeepalive;
    private sealed class SilentRenderProvider(WaveFormat format) : IWaveProvider
    {
        public WaveFormat WaveFormat => format;
        public int Read(byte[] buffer, int offset, int count) { Array.Clear(buffer, offset, count); return count; }
    }
    private readonly VoicePlaybackGuard playbackGuard = new();
    private int naturalRecognitionEpoch;
    private bool naturalVoice, naturalUtterancePending;

    private async Task StartNaturalVoiceAsync(int generation)
    {
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(35));
        using var response = await http.PostAsJsonAsync("/voice/pipecat", new { }, timeout.Token);
        response.EnsureSuccessStatusCode();
        using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync(timeout.Token));
        if (generation != voiceSessionGeneration || !voiceEnabled) return;
        if (!doc.RootElement.GetProperty("available").GetBoolean()) throw new IOException("Natural voice engine unavailable");
        var socket = new ClientWebSocket();
        socket.Options.SetRequestHeader("Origin", BaseUrl.TrimEnd('/'));
        await socket.ConnectAsync(new Uri(Get(doc.RootElement, "url")), timeout.Token);
        if (generation != voiceSessionGeneration || !voiceEnabled) { socket.Dispose(); return; }
        naturalSocket = socket;
        naturalCancellation = new CancellationTokenSource();
        var token = naturalCancellation.Token;
        var frames = Channel.CreateBounded<(bool Reference, byte[] Data)>(new BoundedChannelOptions(96)
        { SingleReader = false, SingleWriter = false, FullMode = BoundedChannelFullMode.Wait });
        naturalFrames = frames;
        naturalVoice = recording = true;
        _ = NaturalSendAsync(socket, frames, generation, token);
        _ = NaturalReceiveAsync(socket, generation, token);
        try
        {
            playbackGuard.Clear(); naturalRecognitionEpoch = 0;
            // Source-mode AEC requires an active stream on the selected speaker.
            using var devices = new MMDeviceEnumerator();
            naturalRenderDevice = devices.GetDefaultAudioEndpoint(DataFlow.Render, Role.Console);
            naturalRenderKeepalive = new WasapiOut(naturalRenderDevice, AudioClientShareMode.Shared, false, 100);
            naturalRenderKeepalive.Init(new SilentRenderProvider(naturalRenderDevice.AudioClient.MixFormat));
            naturalRenderKeepalive.Play();
            naturalAec = new WindowsAecCapture();
            await naturalAec.StartAsync(pcm =>
            {
                if (!VoiceSessionGate.IsCurrent(generation, voiceSessionGeneration, naturalVoice)) return;
                if (!frames.Writer.TryWrite((false, pcm))) NaturalFailure(generation, "Voice processing fell behind — restart voice");
            }, message => NaturalFailure(generation, message)).WaitAsync(timeout.Token);
            if (!VoiceSessionGate.IsCurrent(generation, voiceSessionGeneration, naturalVoice)) return;
            SetState("listening"); TurnStatus.Text = "Listening — Windows speaker echo cancellation active";
            UpdateControlAvailability();
        }
        catch { throw; } // Caller owns cleanup and error reporting.
    }

    private void StopNaturalVoice()
    {
        naturalVoice = false; naturalUtterancePending = false;
        StopPlayback(); // Release speech output before disposing its shared speaker endpoint.
        naturalCancellation?.Cancel(); naturalCancellation = null;
        naturalFrames?.Writer.TryComplete();
        while (naturalFrames?.Reader.TryRead(out _) == true) { }
        naturalFrames = null;
        naturalAec?.Dispose(); naturalAec = null;
        try { naturalRenderKeepalive?.Stop(); naturalRenderKeepalive?.Dispose(); } catch { }
        naturalRenderKeepalive = null; naturalRenderDevice?.Dispose(); naturalRenderDevice = null;
        playbackGuard.Clear(); naturalRecognitionEpoch++;
        naturalSocket?.Abort(); naturalSocket?.Dispose(); naturalSocket = null;
    }

    private void NaturalFailure(int generation, string message) => DispatcherQueue.TryEnqueue(() =>
    {
        if (!VoiceSessionGate.IsCurrent(generation, voiceSessionGeneration, naturalVoice)) return;
        userStoppedChat = true; chatCancellation?.Cancel();
        _ = ShowVoiceFailureAsync(message);
    });

    private async Task NaturalSendAsync(ClientWebSocket socket, Channel<(bool Reference, byte[] Data)> frames, int generation, CancellationToken token)
    {
        try
        {
            await foreach (var frame in frames.Reader.ReadAllAsync(token))
            {
                if (frame.Reference)
                {
                    await socket.SendAsync(frame.Data, WebSocketMessageType.Text, true, token);
                }
                else await socket.SendAsync(frame.Data, WebSocketMessageType.Binary, true, token);
            }
        }
        catch (OperationCanceledException) { }
        catch (Exception) { if (!token.IsCancellationRequested) NaturalFailure(generation, "Voice connection lost — restart voice or use dictation"); }
    }

    private async Task NaturalReceiveAsync(ClientWebSocket socket, int generation, CancellationToken token)
    {
        try
        {
            var buffer = new byte[16384];
            while (!token.IsCancellationRequested)
            {
                using var message = new MemoryStream();
                WebSocketReceiveResult result;
                do
                {
                    result = await socket.ReceiveAsync(new ArraySegment<byte>(buffer), token);
                    if (result.MessageType == WebSocketMessageType.Close) throw new IOException("Voice connection closed");
                    message.Write(buffer, 0, result.Count);
                    if (message.Length > 4000000) throw new IOException("Voice message too large");
                } while (!result.EndOfMessage);
                using var doc = JsonDocument.Parse(message.ToArray());
                var ev = doc.RootElement.Clone();
                DispatcherQueue.TryEnqueue(() =>
                {
                    if (!VoiceSessionGate.IsCurrent(generation, voiceSessionGeneration, naturalVoice)) return;
                    var kind = Get(ev, "type");
                    if (kind is "speech_start" or "partial" or "utterance")
                    {
                        if (!ev.TryGetProperty("epoch", out var epoch) || epoch.GetInt32() != naturalRecognitionEpoch) return;
                    }
                    if (kind == "speech_start")
                    {
                        if (busy || speechCancellation != null)
                        {
                            userStoppedChat = true; resumeVoiceAfterStop = true;
                            speechCancellation?.Cancel(); StopPlayback();
                            _ = StopChatAsync();
                        }
                        SetState("listening"); TurnStatus.Text = "Listening — interruption audio retained";
                    }
                    else if (kind == "partial") TurnStatus.Text = "Hearing: " + Get(ev, "text");
                    else if (kind == "utterance") _ = HandleNaturalUtteranceAsync(ev, generation, token);
                    else if (kind == "error") NaturalFailure(generation, "Voice engine stopped — restart voice or use dictation");
                });
            }
        }
        catch (OperationCanceledException) { }
        catch (Exception) { if (!token.IsCancellationRequested) NaturalFailure(generation, "Voice connection lost — restart voice or use dictation"); }
    }

    private async Task HandleNaturalUtteranceAsync(JsonElement ev, int generation, CancellationToken token)
    {
        var recognitionEpoch = naturalRecognitionEpoch;
        if (!ev.TryGetProperty("id", out var sequence) || !playbackGuard.Consume(sequence.GetInt32())) return;
        if (naturalUtterancePending) { NaturalFailure(generation, "Multiple voice turns queued — use dictation to recover"); return; }
        naturalUtterancePending = true;
        var ownsPending = true;
        try
        {
            var words = Get(ev, "text").Trim();
            var confidence = ev.TryGetProperty("confidence", out var confidenceValue) ? confidenceValue.GetDouble() : 0;
            if (confidence < .72 || words.Length == 0)
            {
                if (!busy) SetState("transcribing");
                TurnStatus.Text = "Checking transcript — interruption audio retained";
                using var content = new MultipartFormDataContent();
                content.Add(new ByteArrayContent(Convert.FromBase64String(Get(ev, "wav"))), "audio", "speech.wav");
                using var response = await http.PostAsync("/transcribe", content, token);
                response.EnsureSuccessStatusCode();
                using var final = JsonDocument.Parse(await response.Content.ReadAsStringAsync(token));
                words = Get(final.RootElement, "text").Trim();
            }
            while (busy) await Task.Delay(30, token);
            if (!VoiceSessionGate.IsCurrent(generation, voiceSessionGeneration, naturalVoice) || recognitionEpoch != naturalRecognitionEpoch || token.IsCancellationRequested) return;
            if (playbackGuard.IsRecentPlayback(words, Environment.TickCount64))
            { SetState("listening"); TurnStatus.Text = "Playback echo ignored — listening for you"; return; }
            if (words.Length == 0) { SetState("listening"); TurnStatus.Text = "No clear speech — try again"; return; }
            if (Composer.Text.Trim().Length > 0 || attachments.Count > 0)
            { EndVoiceMode(); TurnStatus.Text = "Voice paused — review your existing draft"; return; }
            Composer.Text = words;
            // Release the queue guard before awaiting the reply: another user's
            // utterance may interrupt it, but pending recognition stays bounded.
            naturalUtterancePending = false; ownsPending = false;
            await SendAsync(true);
        }
        catch (OperationCanceledException) { }
        catch (Exception) { NaturalFailure(generation, "Voice transcription failed — restart voice or use dictation"); }
        finally { if (ownsPending && generation == voiceSessionGeneration && recognitionEpoch == naturalRecognitionEpoch) naturalUtterancePending = false; }
    }

    private void ResetNaturalRecognition()
    {
        if (!naturalVoice || naturalFrames == null) return;
        naturalRecognitionEpoch++; naturalUtterancePending = false;
        while (naturalFrames.Reader.TryRead(out _)) { }
        var command = Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new { type = "reset", epoch = naturalRecognitionEpoch }));
        if (!naturalFrames.Writer.TryWrite((true, command))) NaturalFailure(voiceSessionGeneration, "Voice reset failed — restart voice");
    }

    private async Task ConsumeVoiceReplyAsync(Channel<string> sentences, CancellationToken token)
    {
        await foreach (var sentence in sentences.Reader.ReadAllAsync(token))
        {
            token.ThrowIfCancellationRequested();
            if (!await SpeakAsync(sentence, cancellationToken: token)) break;
        }
    }
}

