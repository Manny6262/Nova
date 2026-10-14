using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Documents;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Imaging;
using Microsoft.UI.Xaml.Shapes;
using NAudio.Wave;
using System.Diagnostics;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Windows.Media.Core;
using Windows.System;

namespace NovaNative;

public sealed partial class MainPage : Page
{
    private static readonly string BaseUrl = Environment.GetEnvironmentVariable("NOVA_ENGINE_URL") ?? "http://127.0.0.1:5100";
    private static readonly string LastSessionFile = System.IO.Path.Combine(
        NativeRuntime.InstallRoot, "last-session.txt");
    private readonly HttpClient http = new() { BaseAddress = new Uri(BaseUrl), Timeout = Timeout.InfiniteTimeSpan };
    private readonly SolidColorBrush muted = Brush("#A9BBDC");
    private readonly SolidColorBrush bright = Brush("#F1F5FF");
    private int? sessionId;
    private string provider = "local", model = "";
    private bool busy, selecting, voiceEnabled, recording, motion = true, compact, canvasOpen, autoVoice, initializing, dictating, transcribing;
    private WaveInEvent? input;
    private WaveFileWriter? recorder;
    private MemoryStream? audioBuffer;
    private IWavePlayer? output;
    private WaveFileReader? outputReader;
    private MemoryStream? outputBuffer;
    private DateTime listenStart, lastSpeech;
    private bool heardSpeech;
    private readonly DispatcherTimer orbTimer = new() { Interval = TimeSpan.FromMilliseconds(33) };
    private readonly Stopwatch animationClock = Stopwatch.StartNew();
    private double phase;
    private double breathPhase;
    private double lastAnimationTime;
    private double microphoneLevel;
    private double displayedVoiceEnergy;
    private double[] speechEnvelope = [];
    private readonly List<Rectangle> waveRectangles = [];
    private readonly List<(Ellipse halo, Ellipse point, Rectangle rayH, Rectangle rayV, int orbit, double offset)> orbitLights = [];
    private string orbState = "idle";
#if DEBUG || VOICE_VISUAL_QA
    private readonly string? qaVoiceFixture;
    private readonly string? qaDictationFixture;
#endif
    private string welcomeGreeting = "";
    private string canvasTab = "Context";
    private readonly List<(string name, string detail, bool ok)> toolEvents = [];
    private readonly List<(string type, string url)> mediaEvents = [];
    private readonly Dictionary<int, string> notes = [];
    private readonly DispatcherTimer proposalTimer = new() { Interval = TimeSpan.FromSeconds(30) };
    private int pendingProposalCount;
    private readonly DispatcherTimer notesSaveTimer = new() { Interval = TimeSpan.FromMilliseconds(450) };
    private readonly List<(string name, string text, byte[]? image)> attachments = [];

    private CancellationTokenSource? chatCancellation;
    private string? activeTurnId;
    private bool userStoppedChat;
    private bool resumeVoiceAfterStop, resumeCapturePending, modelCommitting;
    private int voiceSessionGeneration;
    private readonly object captureAudioLock = new();
    private CancellationTokenSource? transcriptionCancellation, speechCancellation, modelSwitchCancellation;
    private ComboBox? settingsModelPicker;

    private void UpdateControlAvailability()
    {
        var switching = selecting || modelSwitchCancellation != null;
        var idle = !busy && !transcribing && !switching && !voiceStarting;
        ModelPicker.IsEnabled = idle && !recording;
        if (settingsModelPicker != null) settingsModelPicker.IsEnabled = ModelPicker.IsEnabled;
        ToolTipService.SetToolTip(ModelPicker, ModelPicker.IsEnabled ? "Switch models without losing this conversation" : "End voice or stop the reply before switching models");
        VoiceButton.IsEnabled = idle;
        VoiceModeButton.IsEnabled = voiceEnabled || (!switching && !busy && !transcribing && !voiceStarting);
        ToolTipService.SetToolTip(VoiceModeButton, voiceEnabled ? "Cancel startup or end voice" :
            switching ? "Wait for model loading or cancel it before starting voice" :
            busy || transcribing ? "Stop the reply or finish transcription before starting voice" :
            voiceStartupError ?? "Start a spoken conversation with speaker echo cancellation");
        var stopping = chatCancellation != null || speechCancellation != null;
        SendButton.IsEnabled = !modelCommitting && (stopping || modelSwitchCancellation != null || (idle && !dictating));
        SendButton.Content = modelSwitchCancellation != null ? "Cancel" : stopping ? "Stop" : "↑";
        SendButton.Width = stopping || switching ? 62 : 40;
        SendButton.FontSize = stopping || switching ? 12 : 23;
        ToolTipService.SetToolTip(SendButton, modelSwitchCancellation != null ? "Stop waiting for model loading; keep the previous selection" :
            stopping && voiceEnabled ? "Interrupt this reply and listen again" : stopping ? "Stop this reply" : "Send message");
    }

    private async Task RefreshModelReadinessAsync()
    {
        var key = provider + "|" + model;
        try
        {
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(6));
            using var doc = JsonDocument.Parse(await http.GetStringAsync("/models/readiness", timeout.Token));
            if (key != provider + "|" + model || modelSwitchCancellation != null || selecting) return;
            var label = provider == "codex" ? "GPT" : model;
            ShowEngineStatus(label + " · " + Get(doc.RootElement, "detail", "Readiness unknown"));
        }
        catch { if (key == provider + "|" + model && !selecting) ShowEngineStatus("Model readiness unavailable · retry status"); }
    }
    private string engineStatusDetail = "Starting Nova engine…";
    private void ShowEngineStatus(string detail)
    {
        engineStatusDetail = detail;
        EngineStatus.Text = compact ? (detail.Contains("unavailable", StringComparison.OrdinalIgnoreCase) ? "!" : "●") : detail;
        ToolTipService.SetToolTip(EngineStatus, detail);
    }
    private readonly DispatcherTimer chatProgressTimer = new() { Interval = TimeSpan.FromSeconds(1) };
    private readonly Stopwatch chatClock = new();
    private string chatActivity = "Waiting for Nova";

    private void UpdateChatProgress() => TurnStatus.Text = $"{chatActivity} · {chatClock.Elapsed.TotalSeconds:0}s";

    private async Task StopChatAsync(bool resetVoice = false)
    {
        if (resetVoice) ResetNaturalRecognition();
        var turnId = activeTurnId;
        chatCancellation?.Cancel();
        if (!naturalVoice || !resumeVoiceAfterStop) EndVoiceMode();
        else { speechCancellation?.Cancel(); StopPlayback(); }
        if (turnId == null) return;
        try
        {
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            for (var attempt = 0; attempt < 3; attempt++)
            {
                using var response = await http.PostAsJsonAsync("/chat/stop", new { turn_id = turnId }, timeout.Token);
                if (response.IsSuccessStatusCode) return;
                if (response.StatusCode != System.Net.HttpStatusCode.NotFound) break;
                await Task.Delay(100, timeout.Token);
            }
        }
        catch { /* The local stream is cancelled even if the engine is unreachable. */ }
    }

    private static DateTimeOffset? MessageTime(JsonElement message)
    {
        if (!message.TryGetProperty("created_at", out var timestamp) || !timestamp.TryGetDouble(out var seconds)) return null;
        try { return DateTimeOffset.FromUnixTimeMilliseconds(checked((long)(seconds * 1000))); }
        catch (Exception) { return null; }
    }

    public MainPage()
    {
        InitializeComponent();
        chatProgressTimer.Tick += (_, _) => UpdateChatProgress();
        LoadNotes();
        notesSaveTimer.Tick += (_, _) => { notesSaveTimer.Stop(); SaveNotes(); };
        var hour = DateTime.Now.Hour;
        welcomeGreeting = (hour >= 5 && hour < 12 ? "Good morning" : hour < 17 && hour >= 12 ? "Good afternoon" : "Good evening") + ", User";
        WelcomeTitle.Text = welcomeGreeting;
        for (int i = 0; i < 39; i++)
        {
            var bar = new Rectangle { Width = 2, Height = 4, RadiusX = 2, RadiusY = 2,
                Fill = Brush(i % 5 == 0 ? "#BDA4FF" : "#83D9FF"), VerticalAlignment = VerticalAlignment.Center };
            waveRectangles.Add(bar);
            WaveBars.Children.Add(bar);
        }
        for (int i = 0; i < 7; i++)
        {
            var halo = new Ellipse { Width = i % 3 == 0 ? 19 : 14, Height = i % 3 == 0 ? 19 : 14,
                Fill = Brush(i % 2 == 0 ? "#7A777BFF" : "#7760D4FF") };
            var rayH = new Rectangle { Width = i % 3 == 0 ? 18 : 10, Height = 1,
                Fill = Brush(i % 2 == 0 ? "#B7ADFF" : "#8BDFFF"),
                RenderTransformOrigin = new Windows.Foundation.Point(.5, .5), RenderTransform = new RotateTransform() };
            var rayV = new Rectangle { Width = 1, Height = i % 3 == 0 ? 16 : 8,
                Fill = Brush(i % 2 == 0 ? "#B7ADFF" : "#8BDFFF") };
            var point = new Ellipse { Width = i % 3 == 0 ? 4.5 : 3.2, Height = i % 3 == 0 ? 4.5 : 3.2,
                Fill = Brush("#F5FAFF") };
            OrbitParticles.Children.Add(halo);
            OrbitParticles.Children.Add(rayH);
            OrbitParticles.Children.Add(rayV);
            OrbitParticles.Children.Add(point);
            orbitLights.Add((halo, point, rayH, rayV, i % 3, i * 2.35));
        }
        motion = new Windows.UI.ViewManagement.UISettings().AnimationsEnabled;
        MotionButton.Content = motion ? "Motion: on" : "Motion: off";
        Loaded += async (_, _) => { await InitializeAsync(); await RefreshProposalBadgeAsync(); };
        proposalTimer.Tick += async (_, _) => await RefreshProposalBadgeAsync();
        proposalTimer.Start();
#if DEBUG || VOICE_VISUAL_QA
        var visualState = Environment.GetCommandLineArgs().FirstOrDefault(arg => arg.StartsWith("--orb-state=", StringComparison.OrdinalIgnoreCase))?.Split('=')[1];
        if (visualState is "idle" or "listening" or "thinking" or "speaking") Loaded += (_, _) => SetState(visualState);
        qaVoiceFixture = Environment.GetCommandLineArgs().FirstOrDefault(arg => arg.StartsWith("--voice-fixture=", StringComparison.OrdinalIgnoreCase))?["--voice-fixture=".Length..];
        qaDictationFixture = Environment.GetCommandLineArgs().FirstOrDefault(arg => arg.StartsWith("--dictation-fixture=", StringComparison.OrdinalIgnoreCase))?["--dictation-fixture=".Length..];
#endif
        Unloaded += (_, _) => { _ = StopChatAsync(); chatProgressTimer.Stop(); voiceEnabled = autoVoice = dictating = false; StopCapture(false); StopPlayback(); orbTimer.Stop(); proposalTimer.Stop(); notesSaveTimer.Stop(); SaveNotes(); };
        orbTimer.Tick += (_, _) => { if (motion) AnimateOrb(); };
        orbTimer.Start();
        UpdateNavStyle("Chat");
        UpdateTabStyle();
        RenderCanvas();
        SetState("idle");
        AnimateOrb();
    }

    private static SolidColorBrush Brush(string color)
    {
        if (color == "Transparent") return new SolidColorBrush(Windows.UI.Color.FromArgb(0, 0, 0, 0));
        var hex = color.TrimStart('#');
        var a = hex.Length == 8 ? Convert.ToByte(hex[..2], 16) : (byte)255;
        var offset = hex.Length == 8 ? 2 : 0;
        return new SolidColorBrush(Windows.UI.Color.FromArgb(a,
            Convert.ToByte(hex.Substring(offset, 2), 16),
            Convert.ToByte(hex.Substring(offset + 2, 2), 16),
            Convert.ToByte(hex.Substring(offset + 4, 2), 16)));
    }
    private static TextBlock Text(string value, int size = 13, string color = "#C8D4EE") =>
        new() { Text = value, FontSize = size, Foreground = Brush(color), TextWrapping = TextWrapping.Wrap };

    private async Task InitializeAsync()
    {
        if (initializing) return;
        initializing = true;
        RetryEngineButton.IsEnabled = false;
        ShowEngineStatus("Starting Nova engine…");
        try
        {
            if (!await ((App)Application.Current).Runtime.EnsureStartedAsync() || !await EngineReadyAsync())
            {
                var reason = ((App)Application.Current).Runtime.LastError;
                ShowEngineStatus("Engine unavailable");
                TurnStatus.Text = string.IsNullOrEmpty(reason) ? "Could not connect to Nova's engine." : reason;
                RetryEngineButton.Visibility = Visibility.Visible;
                return;
            }
            RetryEngineButton.Visibility = Visibility.Collapsed;
            ShowEngineStatus("●  Engine connected");
            await LoadModelsAsync();
            await LoadSessionsAsync();
            // A launch begins a fresh, unsaved conversation. Previous chats stay in Recents;
            // the engine creates this chat's session only after the first message is sent.
            if (ConversationList.Children.Count == 0)
                AddMessage("Nova", "I'm here. What would you like to work on?", false);
#if DEBUG || VOICE_VISUAL_QA
            if (qaVoiceFixture is { Length: > 0 } && File.Exists(qaVoiceFixture))
            {
                sessionId = null;
                ConversationList.Children.Clear();
                toolEvents.Clear(); mediaEvents.Clear();
                RenderCanvas();
                voiceEnabled = true;
                autoVoice = false; // Fixture tests never open a person's microphone.
                VoiceModeButton.Content = "■  End voice";
                await TranscribeAsync(await File.ReadAllBytesAsync(qaVoiceFixture));
            }
            if (qaDictationFixture is { Length: > 0 } && File.Exists(qaDictationFixture))
                await TranscribeAsync(await File.ReadAllBytesAsync(qaDictationFixture), true);
#endif
        }
        finally
        {
            initializing = false;
            RetryEngineButton.IsEnabled = true;
        }
    }

    private async void RetryEngineButton_Click(object sender, RoutedEventArgs e) => await InitializeAsync();

    private async Task<bool> EngineReadyAsync()
    {
        try { using var response = await http.GetAsync("/health"); return response.IsSuccessStatusCode; }
        catch { return false; }
    }

    private static string Get(JsonElement element, string name, string fallback = "") =>
        element.ValueKind == JsonValueKind.Object && element.TryGetProperty(name, out var property) && property.ValueKind != JsonValueKind.Null ? property.ToString() : fallback;
    private static bool Has(JsonElement element, string name) => element.ValueKind == JsonValueKind.Object && element.TryGetProperty(name, out _);

    private async Task LoadModelsAsync()
    {
        try
        {
            using var doc = JsonDocument.Parse(await http.GetStringAsync("/models"));
            var root = doc.RootElement;
            provider = Get(root, "provider", "local");
            model = Get(root, "active");
            selecting = true;
            ModelPicker.Items.Clear();
            ModelPicker.Items.Add(new ComboBoxItem { Content = "GPT via OAuth", Tag = "codex|" });
            foreach (var item in root.GetProperty("models").EnumerateArray())
            {
                var name = Get(item, "name");
                ModelPicker.Items.Add(new ComboBoxItem { Content = name, Tag = "local|" + name });
            }
            foreach (ComboBoxItem item in ModelPicker.Items)
                if ((string)item.Tag == (provider == "codex" ? "codex|" : "local|" + model)) { ModelPicker.SelectedItem = item; break; }
            selecting = false;
            RenderCanvas();
        }
        catch (Exception error) { ShowEngineStatus("Model list unavailable: " + error.Message); }
        finally { selecting = false; UpdateControlAvailability(); }
        await RefreshModelReadinessAsync();
    }

    private async Task LoadSessionsAsync()
    {
        try
        {
            using var doc = JsonDocument.Parse(await http.GetStringAsync("/sessions"));
            SessionsList.Children.Clear();
            foreach (var item in doc.RootElement.EnumerateArray().Take(18))
            {
                var id = item.GetProperty("id").GetInt32();
                var button = new Button { Content = Get(item, "title", "New chat"), Tag = id, HorizontalAlignment = HorizontalAlignment.Stretch,
                    HorizontalContentAlignment = HorizontalAlignment.Left, Background = Brush("Transparent"), Foreground = muted,
                    BorderThickness = new Thickness(0), MinHeight = 38 };
                button.Click += async (_, _) =>
                {
                    await LoadSessionAsync(id);
                    ShowSection("Chat"); UpdateNavStyle("Chat"); WelcomeTitle.Text = welcomeGreeting;
                };
                SessionsList.Children.Add(button);
            }
        }
        catch { }
    }

    private async Task LoadSessionAsync(int id)
    {
        if (busy || transcribing) return;
        EndVoiceMode();
        try
        {
            using var doc = JsonDocument.Parse(await http.GetStringAsync("/sessions/" + id));
            sessionId = id;
            await LoadConversationProjectAsync(id);
            attachments.Clear(); Composer.Text = ""; RetryButton.Visibility = Visibility.Collapsed;
            ConversationList.Children.Clear();
            toolEvents.Clear(); mediaEvents.Clear();
            foreach (var item in doc.RootElement.EnumerateArray())
                AddMessage(Get(item, "role") == "user" ? "You" : "Nova", Get(item, "content"), false, MessageTime(item), historical: true);
            Directory.CreateDirectory(System.IO.Path.GetDirectoryName(LastSessionFile)!);
            File.WriteAllText(LastSessionFile, id.ToString());
            RenderCanvas();
        }
        catch (Exception error) { TurnStatus.Text = error.Message; }
    }

    private async void NewChatButton_Click(object sender, RoutedEventArgs e)
    {
        if (busy || transcribing) return;
        EndVoiceMode();
        try
        {
            using var response = await http.PostAsJsonAsync("/sessions", new { });
            response.EnsureSuccessStatusCode();
            using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
            sessionId = doc.RootElement.GetProperty("id").GetInt32();
            ClearConversationProject();
            Composer.Text = ""; RetryButton.Visibility = Visibility.Collapsed;
            Directory.CreateDirectory(System.IO.Path.GetDirectoryName(LastSessionFile)!);
            File.WriteAllText(LastSessionFile, sessionId.Value.ToString());
            ConversationList.Children.Clear(); toolEvents.Clear(); mediaEvents.Clear(); attachments.Clear();
            RenderCanvas(); await LoadSessionsAsync();
            ShowSection("Chat"); UpdateNavStyle("Chat"); WelcomeTitle.Text = welcomeGreeting;
            Composer.Focus(FocusState.Programmatic);
        }
        catch (Exception error) { TurnStatus.Text = error.Message; }
    }

    private void AddMessage(string sender, string content, bool streaming, DateTimeOffset? timestamp = null, bool historical = false)
    {
        var stack = CreateMessageShell(sender, timestamp, !historical);
        var body = Text("", 14, "#EEF2FC");
        RenderMarkdown(body, sender == "Nova" ? RemoveMediaMarkup(content) : content);
        body.IsTextSelectionEnabled = true;
        stack.Children.Add(body);
        if (sender == "Nova") RenderMediaFromMarkup(content, stack);
        ConversationScroll.ChangeView(null, ConversationScroll.ScrollableHeight, null);
    }

    private (TextBlock body, StackPanel stack) BeginReply()
    {
        var stack = CreateMessageShell("Nova");
        var body = Text("", 14, "#EEF2FC");
        body.IsTextSelectionEnabled = true;
        stack.Children.Add(body);
        return (body, stack);
    }

    private StackPanel CreateMessageShell(string sender, DateTimeOffset? timestamp = null, bool showCurrentTime = true)
    {
        var user = sender == "You";
        var row = new Grid { HorizontalAlignment = HorizontalAlignment.Left, MaxWidth = 820 };
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(39) });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        var avatar = new Border { Width = 31, Height = 31, CornerRadius = new CornerRadius(16),
            Background = Brush(user ? "#365898" : "#313077"),
            BorderBrush = Brush(user ? "#789BE6" : "#9D8DFF"), BorderThickness = new Thickness(1),
            VerticalAlignment = VerticalAlignment.Top, Margin = new Thickness(0, 2, 0, 0) };
        avatar.Child = new TextBlock { Text = user ? "M" : "✦", Foreground = Brush("#F5F8FF"),
            FontSize = user ? 15 : 19, HorizontalAlignment = HorizontalAlignment.Center,
            VerticalAlignment = VerticalAlignment.Center };
        row.Children.Add(avatar);
        var card = new Border { Background = Brush(user ? "#C4203150" : "#D0172948"),
            BorderBrush = Brush(user ? "#48668F" : "#556DA5"), BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(13), Padding = new Thickness(15, 10, 15, 13),
            MaxWidth = user ? 610 : 760 };
        Grid.SetColumn(card, 1);
        var stack = new StackPanel { Spacing = 9 };
        var heading = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10 };
        heading.Children.Add(Text(user ? "User" : "Nova", 12, user ? "#D9E9FF" : "#C6BCFF"));
        var timeLabel = timestamp?.ToLocalTime().ToString("MMM d, yyyy · h:mm tt")
            ?? (showCurrentTime ? DateTime.Now.ToString("h:mm tt") : "Time unavailable");
        heading.Children.Add(Text(timeLabel, 10, "#8CA7CE"));
        stack.Children.Add(heading);
        card.Child = stack;
        row.Children.Add(card);
        ConversationList.Children.Add(row);
        return stack;
    }

    private async void SendButton_Click(object sender, RoutedEventArgs e)
    {
        if (modelSwitchCancellation != null) { if (!modelCommitting) modelSwitchCancellation.Cancel(); }
        else if (chatCancellation != null)
        { userStoppedChat = true; resumeVoiceAfterStop = voiceEnabled && autoVoice; await StopChatAsync(true); }
        else if (speechCancellation != null) { speechCancellation.Cancel(); EndVoiceMode(); }
        else await SendAsync(false);
    }

    private async void RetryButton_Click(object sender, RoutedEventArgs e) => await SendAsync(false);
    private async void Composer_KeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (e.Key == VirtualKey.Enter && !Microsoft.UI.Input.InputKeyboardSource.GetKeyStateForCurrentThread(VirtualKey.Shift).HasFlag(Windows.UI.Core.CoreVirtualKeyStates.Down))
        { e.Handled = true; await SendAsync(false); }
    }

    private async Task SendAsync(bool fromVoice)
    {
        var prompt = Composer.Text.Trim();
        if (busy || selecting || modelSwitchCancellation != null || (transcribing && !fromVoice) || (prompt.Length == 0 && attachments.Count == 0)) return;
        if (!fromVoice && naturalVoice) EndVoiceMode();
        if (!naturalVoice) StopCapture(false);
        var sentAttachments = attachments.ToArray();
        var requestProvider = provider; var requestModel = model;
        using var cancellation = new CancellationTokenSource(TimeSpan.FromMinutes(3));
        chatCancellation = cancellation;
        activeTurnId = Guid.NewGuid().ToString("N");
        var turnId = activeTurnId;
        userStoppedChat = false; resumeVoiceAfterStop = false;
        busy = true; VoiceButton.IsEnabled = false; Composer.IsEnabled = AttachButton.IsEnabled = false; Composer.Text = "";
        SendButton.Content = "Stop"; SendButton.Width = 62; SendButton.FontSize = 12;
        ToolTipService.SetToolTip(SendButton, "Stop this reply; completed actions are not undone");
        RetryButton.Visibility = Visibility.Collapsed;
        ChatProgress.Visibility = Visibility.Visible;
        chatActivity = "Waiting for Nova"; chatClock.Restart(); chatProgressTimer.Start(); UpdateChatProgress();
        AddMessage("You", prompt + (sentAttachments.Length > 0 ? "\n" + string.Join(", ", sentAttachments.Select(a => "Attachment: " + a.name)) : ""), false);
        var (body, stack) = BeginReply();
        var reply = new StringBuilder();
        var streamFailed = false;
        var completed = false;
        var voiceSentences = fromVoice && naturalVoice ? System.Threading.Channels.Channel.CreateBounded<string>(32) : null;
        var voiceBuffer = new VoiceReplyBuffer();
        var speechTask = voiceSentences != null ? ConsumeVoiceReplyAsync(voiceSentences, cancellation.Token) : null;
        var files = sentAttachments.Where(a => a.image == null).Select(a => new { name = a.name, text = a.text }).ToArray();
        var images = sentAttachments.Where(a => a.image != null).Select(a => Convert.ToBase64String(a.image!)).ToArray();
        attachments.Clear();
        SetState("thinking"); UpdateControlAvailability();
        try
        {
            var request = new { message = prompt, session_id = sessionId, turn_id = turnId, provider = requestProvider, model = requestModel,
                caps = new { write = ActionToggle.IsChecked == true, exec = ActionToggle.IsChecked == true, web = ToolsToggle.IsChecked == true, control = ComputerToggle.IsChecked == true },
                approval_mode = "ask", voice_mode = fromVoice, voice_version = fromVoice ? 2 : 0, images, files };
            using var chatRequest = new HttpRequestMessage(HttpMethod.Post, "/chat") { Content = JsonContent.Create(request) };
            using var response = await http.SendAsync(chatRequest, HttpCompletionOption.ResponseHeadersRead, cancellation.Token);
            response.EnsureSuccessStatusCode();
            await using var stream = await response.Content.ReadAsStreamAsync(cancellation.Token);
            using var reader = new StreamReader(stream);
            while (await reader.ReadLineAsync(cancellation.Token) is { } line)
            {
                if (line.Length == 0) continue;
                using var doc = JsonDocument.Parse(line);
                var ev = doc.RootElement;
                if (Has(ev, "session_id") && int.TryParse(Get(ev, "session_id"), out int id)) sessionId = id;
                if (Has(ev, "fact_saved"))
                    stack.Children.Add(Card("Memory saved · " + Get(ev, "fact_scope", "Personal"), Get(ev, "fact_saved"), "#B4E5D7"));
                if (Has(ev, "fact_error"))
                    stack.Children.Add(Card("Memory was not saved", Get(ev, "fact_error"), "#F0A1A7"));
                if (Has(ev, "status"))
                {
                    chatActivity = Get(ev, "detail", "Working"); UpdateChatProgress();
                    SetState(Get(ev, "status") == "preparing_model" ? "model_loading" : "thinking");
                }
                if (Has(ev, "token"))
                {
                    chatActivity = "Receiving reply"; UpdateChatProgress(); if (output?.PlaybackState != PlaybackState.Playing) SetState("thinking");
                    var tokenText = Get(ev, "token");
                    reply.Append(tokenText);
                    if (voiceSentences != null)
                        foreach (var sentence in voiceBuffer.Append(tokenText))
                        {
                            var spokenSentence = RemoveMediaMarkup(sentence);
                            if (spokenSentence.Length > 0 && !voiceSentences.Writer.TryWrite(spokenSentence))
                                throw new IOException("Speech queue full; retry a shorter reply");
                        }
                    body.Text = RemoveMediaMarkup(reply.ToString());
                    ConversationScroll.ChangeView(null, ConversationScroll.ScrollableHeight, null);
                }
                if (Has(ev, "thought")) { chatActivity = "Thinking"; UpdateChatProgress(); }
                if (Has(ev, "tool_start"))
                {
                    var tool = ev.GetProperty("tool_start");
                    var name = Get(tool, "name", "tool");
                    chatActivity = "Using " + name; UpdateChatProgress();
                    toolEvents.Add((name, "Running", true)); RenderCanvas();
                }
                if (Has(ev, "tool_result"))
                {
                    var tool = ev.GetProperty("tool_result");
                    var name = Get(tool, "name", "tool");
                    var ok = Get(tool, "ok") == "True";
                    toolEvents.Add((name, Get(tool, "preview", Get(tool, "detail")), ok));
                    if (Has(tool, "media")) AddMedia(tool.GetProperty("media"), stack);
                    RenderCanvas();
                }
                if (Has(ev, "confirm_request")) await ConfirmAsync(ev.GetProperty("confirm_request"), cancellation.Token);
                if (Has(ev, "error"))
                {
                    body.Text += "\n\n" + Get(ev, "error"); streamFailed = true;
                    TurnStatus.Text = "Failed · draft restored"; SetState("failed");
                }
                if (Has(ev, "cancelled")) throw new OperationCanceledException();
                if (Has(ev, "done")) { completed = true; break; }
            }
            chatProgressTimer.Stop(); ChatProgress.Visibility = Visibility.Collapsed;
            if (!completed && !streamFailed) throw new IOException("The reply ended unexpectedly.");
            RenderMediaFromMarkup(reply.ToString(), stack);
            RenderMarkdown(body, body.Text);
            if (reply.Length == 0 && !streamFailed)
            {
                body.Text = "No reply was returned. Your draft is available to retry.";
                streamFailed = true; TurnStatus.Text = "No reply · draft restored"; SetState("failed");
            }
            var spoken = true;
            if (voiceSentences != null)
            {
                var tail = RemoveMediaMarkup(voiceBuffer.Finish());
                if (!streamFailed && tail.Length > 0) await voiceSentences.Writer.WriteAsync(tail, cancellation.Token);
                voiceSentences.Writer.TryComplete();
                if (speechTask != null) await speechTask;
            }
            else if (!streamFailed && fromVoice && voiceEnabled && reply.Length > 0)
                spoken = await SpeakAsync(RemoveMediaMarkup(reply.ToString()));
            else if (!streamFailed) SetState("idle");
            if (!streamFailed && spoken) { SetState(naturalVoice ? "listening" : "idle"); TurnStatus.Text = naturalVoice ? "Listening — speak whenever you are ready" : "Ready"; }
            if (!streamFailed) await LoadSessionsAsync();
        }
        catch (OperationCanceledException)
        {
            streamFailed = true;
            chatProgressTimer.Stop();
            var reason = userStoppedChat ? "Reply stopped" : "Reply timed out";
            var resumeVoice = fromVoice && userStoppedChat && resumeVoiceAfterStop;
            body.Text += "\n\n" + reason + (resumeVoice ? ". Returning to listening." : ". Your draft and attachments are ready to retry.");
            TurnStatus.Text = reason + (resumeVoice ? " · listening next" : " · draft restored");
            SetState("idle");
            await StopChatAsync();
        }
        catch (Exception error)
        {
            streamFailed = true;
            body.Text += "\n\nConnection failed: " + error.Message + "\nYour draft and attachments are ready to retry.";
            TurnStatus.Text = "Connection failed · draft restored";
            SetState("failed");
            await StopChatAsync();
        }
        finally
        {
            voiceSentences?.Writer.TryComplete();
            if (speechTask != null)
            {
                if (streamFailed) cancellation.Cancel();
                try { await speechTask; } catch (OperationCanceledException) { }
            }
            var resumeVoice = fromVoice && userStoppedChat && resumeVoiceAfterStop;
            if (streamFailed && !resumeVoice)
            {
                EndVoiceMode();
                Composer.Text = prompt;
                attachments.Clear(); attachments.AddRange(sentAttachments);
                RetryButton.Visibility = Visibility.Visible;
                ToolTipService.SetToolTip(RetryButton, "Send the restored draft again; completed actions are not undone");
            }
            chatProgressTimer.Stop(); chatClock.Stop(); ChatProgress.Visibility = Visibility.Collapsed;
            chatCancellation = null; activeTurnId = null;
            busy = false; Composer.IsEnabled = AttachButton.IsEnabled = true;
            SendButton.IsEnabled = VoiceButton.IsEnabled = true;
            SendButton.Content = "↑"; SendButton.Width = 40; SendButton.FontSize = 23;
            ToolTipService.SetToolTip(SendButton, "Send message");
            if (resumeVoice)
            {
                Composer.Text = ""; attachments.Clear(); RetryButton.Visibility = Visibility.Collapsed;
                voiceEnabled = autoVoice = true; VoiceModeButton.Content = "■  End voice";
            }
            UpdateControlAvailability();
            if (autoVoice && voiceEnabled && !recording) _ = ResumeVoiceCaptureAsync();
        }
    }

    private async Task ConfirmAsync(JsonElement info, CancellationToken cancellationToken = default)
    {
        var token = Get(info, "token");
        var summary = Get(info, "summary", "Allow this action?");
        var detail = Get(info, "detail");
        var dialog = new ContentDialog { Title = "Nova asks for approval", Content = summary + (detail.Length > 0 ? "\n\n" + detail : ""),
            PrimaryButtonText = "Allow", CloseButtonText = "Decline", DefaultButton = ContentDialogButton.Close, XamlRoot = XamlRoot };
        chatActivity = "Approval needed"; UpdateChatProgress();
        using var registration = cancellationToken.Register(() => DispatcherQueue.TryEnqueue(() => dialog.Hide()));
        var approved = await dialog.ShowAsync() == ContentDialogResult.Primary;
        cancellationToken.ThrowIfCancellationRequested();
        using var response = await http.PostAsJsonAsync("/confirm", new { token, approved }, cancellationToken);
        if (!response.IsSuccessStatusCode) TurnStatus.Text = "Approval expired";
        else TurnStatus.Text = approved ? "Action approved" : "Action declined";
    }

    private static readonly Regex MediaPattern = new(@"!?\[[^\]]*\]\((/media/[^)]+)\)", RegexOptions.Compiled | RegexOptions.IgnoreCase);
    private static string RemoveMediaMarkup(string value) => MediaPattern.Replace(value, "").Trim();
    private static readonly Regex InlinePattern = new(@"(\[[^\]]+\]\(https?://[^)]+\)|https?://[^\s<>()]+|\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)", RegexOptions.Compiled);
    private static readonly Regex LinkPattern = new(@"\[([^\]]+)\]\((https?://[^)]+)\)", RegexOptions.Compiled);
    private static void RenderMarkdown(TextBlock body, string content)
    {
        body.Inlines.Clear();
        body.LineHeight = 21;
        var lines = content.Replace("\r", "").Split('\n');
        for (int lineNumber = 0; lineNumber < lines.Length; lineNumber++)
        {
            var line = lines[lineNumber];
            if (line.StartsWith("- ")) { body.Inlines.Add(new Run { Text = "•  " }); line = line[2..]; }
            else if (line.StartsWith("# ")) line = line[2..];
            int last = 0;
            foreach (Match match in InlinePattern.Matches(line))
            {
                if (match.Index > last) body.Inlines.Add(new Run { Text = line[last..match.Index] });
                var value = match.Value;
                if (value.StartsWith("[") || value.StartsWith("http", StringComparison.OrdinalIgnoreCase))
                {
                    var linkMatch = LinkPattern.Match(value);
                    var address = linkMatch.Success ? linkMatch.Groups[2].Value : value.TrimEnd('.', ',', ';', ':', '!', '?');
                    var label = linkMatch.Success ? linkMatch.Groups[1].Value : address;
                    if (Uri.TryCreate(address, UriKind.Absolute, out var uri) && uri.Scheme is "http" or "https")
                    {
                        var link = new Hyperlink { NavigateUri = uri, Foreground = Brush("#A9DFF6") };
                        link.Inlines.Add(new Run { Text = label });
                        body.Inlines.Add(link);
                        if (!linkMatch.Success && address.Length < value.Length)
                            body.Inlines.Add(new Run { Text = value[address.Length..] });
                    }
                    else body.Inlines.Add(new Run { Text = value });
                }
                else if (value.StartsWith("**"))
                {
                    var bold = new Bold();
                    bold.Inlines.Add(new Run { Text = value[2..^2] });
                    body.Inlines.Add(bold);
                }
                else if (value.StartsWith("*"))
                {
                    var italic = new Italic();
                    italic.Inlines.Add(new Run { Text = value[1..^1] });
                    body.Inlines.Add(italic);
                }
                else body.Inlines.Add(new Run { Text = value[1..^1], Foreground = Brush("#A9DFF6"),
                    FontFamily = new FontFamily("Consolas") });
                last = match.Index + match.Length;
            }
            if (last < line.Length) body.Inlines.Add(new Run { Text = line[last..] });
            if (lineNumber < lines.Length - 1) body.Inlines.Add(new LineBreak());
        }
    }
    private void RenderMediaFromMarkup(string value, StackPanel target)
    {
        foreach (Match match in MediaPattern.Matches(value))
            AddMedia(match.Groups[1].Value.EndsWith(".mp4", StringComparison.OrdinalIgnoreCase) ? "video" : "image", match.Groups[1].Value, target);
    }
    private void AddMedia(JsonElement media, StackPanel target) => AddMedia(Get(media, "type"), Get(media, "url"), target);
    private void AddMedia(string type, string url, StackPanel target)
    {
        if (!url.StartsWith("/media/", StringComparison.Ordinal) || mediaEvents.Any(m => m.url == url)) return;
        mediaEvents.Add((type, url));
        target.Children.Add(MediaCard(type, url, false));
        RenderCanvas();
    }

    private static Border MediaCard(string type, string url, bool small)
    {
        var uri = new Uri(BaseUrl + url);
        var panel = new StackPanel { Spacing = 0 };
        if (type == "image")
            panel.Children.Add(new Image { Source = new BitmapImage(uri), Height = small ? 155 : 295,
                Stretch = Stretch.UniformToFill, HorizontalAlignment = HorizontalAlignment.Stretch });
        else if (small)
        {
            var preview = new Grid { Height = 155 };
            var player = new MediaPlayerElement { Source = MediaSource.CreateFromUri(uri),
                AreTransportControlsEnabled = false, Height = 155 };
            preview.Children.Add(player);
            var play = new Button { Content = "▶", Width = 47, Height = 47, CornerRadius = new CornerRadius(24),
                Background = Brush("#C4162B4F"), BorderBrush = Brush("#B9D7FF"), BorderThickness = new Thickness(1.5),
                Foreground = Brush("#F3F7FF"), FontSize = 18, HorizontalAlignment = HorizontalAlignment.Center,
                VerticalAlignment = VerticalAlignment.Center };
            play.Click += (_, _) => { play.Visibility = Visibility.Collapsed; player.AreTransportControlsEnabled = true; player.MediaPlayer?.Play(); };
            preview.Children.Add(play);
            panel.Children.Add(preview);
        }
        else
            panel.Children.Add(new MediaPlayerElement { Source = MediaSource.CreateFromUri(uri),
                AreTransportControlsEnabled = true, Height = 295 });
        var details = new StackPanel { Spacing = 3, Margin = new Thickness(12, 8, 12, 10) };
        details.Children.Add(Text(type == "image" ? "Generated image" : "Generated video", small ? 12 : 13, "#F0F5FF"));
        details.Children.Add(Text(type == "image" ? "Image · saved locally" : "Video · saved locally", 10, "#9AB4DB"));
        panel.Children.Add(details);
        return new Border { Child = panel, Background = Brush("#CB1A2D4E"), BorderBrush = Brush("#5076AC"),
            BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(10),
            HorizontalAlignment = HorizontalAlignment.Stretch, MaxWidth = small ? 310 : 690 };
    }

    private void RenderCanvas()
    {
        CanvasContent.Children.Clear();
        CanvasToggle.Content = mediaEvents.Count == 0 ? "▣  Canvas" : $"▣  Canvas  {mediaEvents.Count}";
        if (canvasTab == "Tools")
        {
            CanvasContent.Children.Add(Text("TOOL ACTIVITY", 10, "#91DDFB"));
            if (toolEvents.Count == 0) CanvasContent.Children.Add(Text("Tool activity appears here as Nova works."));
            foreach (var (name, detail, ok) in toolEvents.TakeLast(18).Reverse())
                CanvasContent.Children.Add(Card(name, detail, ok ? "#8CE2CA" : "#F0A1A7"));
        }
        else if (canvasTab == "Notes")
        {
            CanvasContent.Children.Add(Text("YOUR NOTES", 10, "#83DEF8"));
            var key = sessionId ?? 0;
            var box = new TextBox { PlaceholderText = "Keep notes beside your conversation…", AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, MinHeight = 220,
                Text = notes.GetValueOrDefault(key, "") };
            box.TextChanged += (_, _) => { notes[key] = box.Text; notesSaveTimer.Stop(); notesSaveTimer.Start(); };
            CanvasContent.Children.Add(box);
        }
        else
        {
            var projectButton = ActionButton(conversationProjectId == null ? "Choose project" : "Change project");
            projectButton.Click += async (_, _) => await ChooseConversationProjectAsync();
            CanvasContent.Children.Add(projectButton);
            if (conversationProjectId != null)
                CanvasContent.Children.Add(Card("Project · " + conversationProjectName,
                    conversationProjectNextStep.Length > 0 ? "Next step: " + conversationProjectNextStep : "Context linked to this conversation", "#B8DFEF"));
            CanvasContent.Children.Add(Text(mediaEvents.Count > 0 ? "GENERATED FOR YOU" : "YOUR WORKSPACE", 10, "#A9DFFC"));
            if (mediaEvents.Count == 0)
            {
                CanvasContent.Children.Add(Text("Images, videos, and other results from this conversation will appear here.", 13, "#B9CAE5"));
                CanvasContent.Children.Add(Card("Active model", provider == "codex" ? "GPT via OAuth" : model, "#C2B8FF"));
            }
            foreach (var (type, url) in mediaEvents.TakeLast(8).Reverse())
                CanvasContent.Children.Add(MediaCard(type, url, true));
        }
    }
    private static Border Card(string title, string detail, string accent)
    {
        var stack = new StackPanel { Spacing = 5 };
        stack.Children.Add(Text(title, 12, accent));
        stack.Children.Add(Text(detail, 12, "#C3CEE8"));
        return new Border { Background = Brush("#B81A2D4E"), BorderBrush = Brush("#5275A4"), BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(10), Padding = new Thickness(12), Child = stack };
    }

    private void CanvasTab_Click(object sender, RoutedEventArgs e) { canvasTab = (string)((Button)sender).Tag; UpdateTabStyle(); RenderCanvas(); }
    private void UpdateTabStyle()
    {
        foreach (var button in new[] { ContextTab, ToolsTab, NotesTab })
        {
            var selected = (string)button.Tag == canvasTab;
            button.Background = Brush(selected ? "#304872" : "Transparent");
            button.Foreground = Brush(selected ? "#F5F8FF" : "#B4C6E5");
            button.BorderBrush = Brush(selected ? "#A89EFF" : "Transparent");
            button.BorderThickness = new Thickness(selected ? 1 : 0);
        }
    }
    private void CanvasToggle_Click(object sender, RoutedEventArgs e)
    {
        canvasOpen = !canvasOpen;
        CanvasPanel.Visibility = canvasOpen || !compact ? Visibility.Visible : Visibility.Collapsed;
        CanvasToggle.Visibility = compact && !canvasOpen ? Visibility.Visible : Visibility.Collapsed;
        CanvasClose.Visibility = compact && canvasOpen ? Visibility.Visible : Visibility.Collapsed;
        if (compact && canvasOpen)
        {
            Grid.SetColumn(CanvasPanel, 1);
            CanvasPanel.HorizontalAlignment = HorizontalAlignment.Right;
            CanvasPanel.Width = Math.Min(330, LayoutRoot.ActualWidth - 85);
            CanvasPanel.Margin = new Thickness(3, 9, 12, 145);
        }
    }
    private void LayoutRoot_SizeChanged(object sender, SizeChangedEventArgs e)
    {
        DrawStarfield(e.NewSize.Width, e.NewSize.Height);
        compact = e.NewSize.Width < 1100;
        NavColumn.Width = new GridLength(compact ? 70 : 208);
        BrandText.Visibility = compact ? Visibility.Collapsed : Visibility.Visible;
        RecentHeading.Visibility = compact ? Visibility.Collapsed : Visibility.Visible;
        SessionsScroll.Visibility = compact ? Visibility.Collapsed : Visibility.Visible;
        RailPrivacy.Visibility = compact ? Visibility.Collapsed : Visibility.Visible;
        FooterPrivacy.Visibility = compact ? Visibility.Collapsed : Visibility.Visible;
        ShowEngineStatus(engineStatusDetail);
        NewChatButton.Content = compact ? "＋" : "＋   New conversation";
        ChatNav.Content = compact ? "✧" : "✧    Chat";
        StudioNav.Content = compact ? "◈" : "◈    Studio";
        LibraryNav.Content = compact ? "▣" : "▣    Library";
        UpdateTasksBadge();
        SettingsNav.Content = compact ? "⚙" : "⚙    Settings";
        WelcomeTitle.FontSize = compact ? 22 : 35;
        VoiceHeader.Height = compact ? 146 : 172;
        VoiceColumn.Width = new GridLength(compact ? 315 : 390);
        OrbitColumn.Width = new GridLength(compact ? 180 : 230);
        OrbViewbox.Width = compact ? 178 : 225;
        OrbViewbox.Height = compact ? 140 : 166;
        VoiceHint.Visibility = compact ? Visibility.Collapsed : Visibility.Visible;
        CanvasColumn.Width = compact ? new GridLength(0) : new GridLength(330);
        CanvasToggle.Visibility = compact && !canvasOpen ? Visibility.Visible : Visibility.Collapsed;
        CanvasClose.Visibility = compact && canvasOpen ? Visibility.Visible : Visibility.Collapsed;
        CanvasPanel.Visibility = compact && !canvasOpen ? Visibility.Collapsed : Visibility.Visible;
        if (compact && canvasOpen) { Grid.SetColumn(CanvasPanel, 1); CanvasPanel.HorizontalAlignment = HorizontalAlignment.Right; CanvasPanel.Width = Math.Min(330, e.NewSize.Width - 85); CanvasPanel.Margin = new Thickness(3, 9, 12, 145); }
        else { Grid.SetColumn(CanvasPanel, 2); CanvasPanel.HorizontalAlignment = HorizontalAlignment.Stretch; CanvasPanel.Width = double.NaN; CanvasPanel.Margin = new Thickness(3, 9, 12, 10); }
    }
    private void LayoutRoot_KeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (e.Key == VirtualKey.Escape && compact && canvasOpen) { CanvasToggle_Click(CanvasToggle, new RoutedEventArgs()); e.Handled = true; }
    }
    private void DrawStarfield(double width, double height)
    {
        if (width < 1 || height < 1) return;
        Starfield.Children.Clear();
        var random = new Random(1109);
        for (int i = 0; i < 60; i++)
        {
            var star = new Ellipse { Width = i % 8 == 0 ? 2 : 1, Height = i % 8 == 0 ? 2 : 1,
                Fill = Brush(i % 5 == 0 ? "#A8A4FF" : "#B6DCFF"), Opacity = i % 8 == 0 ? .7 : .36 };
            Canvas.SetLeft(star, random.NextDouble() * width);
            Canvas.SetTop(star, random.NextDouble() * height);
            Starfield.Children.Add(star);
        }
    }
    private void MotionButton_Click(object sender, RoutedEventArgs e) { motion = !motion; MotionButton.Content = motion ? "Motion: on" : "Motion: off"; }
    private void NavButton_Click(object sender, RoutedEventArgs e)
    {
        var section = (string)((Button)sender).Tag;
        ShowSection(section);
        WelcomeTitle.Text = section == "Chat" ? welcomeGreeting : section;
        UpdateNavStyle(section);
        canvasTab = section == "Studio" || section == "Library" ? "Context" : section == "Tasks" ? "Tools" : "Notes";
        UpdateTabStyle();
        RenderCanvas();
    }
    private void UpdateNavStyle(string section)
    {
        foreach (var button in new[] { ChatNav, StudioNav, LibraryNav, TasksNav, SettingsNav })
        {
            var selected = (string)button.Tag == section;
            button.Background = Brush(selected ? "#524B59A6" : "Transparent");
            button.BorderBrush = Brush(selected ? "#A29AFF" : "Transparent");
            button.BorderThickness = new Thickness(selected ? 1 : 0);
            button.Foreground = Brush(selected ? "#F8F9FF" : "#C5D5F1");
        }
    }
    private async void ModelPicker_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (selecting || ModelPicker.SelectedItem is not ComboBoxItem item) return;
        if (!VoiceSessionGate.CanSwitch(busy, transcribing, recording) || modelSwitchCancellation != null)
        {
            selecting = true;
            var selectedTag = provider == "codex" ? "codex|" : "local|" + model;
            ModelPicker.SelectedItem = ModelPicker.Items.Cast<ComboBoxItem>().FirstOrDefault(value => (string)value.Tag == selectedTag);
            selecting = false;
            TurnStatus.Text = "End voice or stop the reply before switching models";
            return;
        }
        var parts = ((string)item.Tag).Split('|', 2);
        using var cancellation = new CancellationTokenSource(TimeSpan.FromSeconds(145));
        modelSwitchCancellation = cancellation;
        selecting = true;
        EndVoiceMode();
        UpdateControlAvailability();
        chatActivity = parts[0] == "local" ? "Loading " + parts[1] : "Checking GPT connection";
        ShowEngineStatus(chatActivity);
        chatClock.Restart(); chatProgressTimer.Start(); ChatProgress.Visibility = Visibility.Visible; UpdateChatProgress();
        try
        {
            if (parts[0] == "local")
            {
                using var prepare = await http.PostAsJsonAsync("/models/prepare", new { name = parts[1] }, cancellation.Token);
                using var prepared = JsonDocument.Parse(await prepare.Content.ReadAsStringAsync(cancellation.Token));
                if (!prepare.IsSuccessStatusCode) throw new InvalidOperationException(Get(prepared.RootElement, "error", "Model preparation failed"));
                if (Get(prepared.RootElement, "ready") != "True") throw new InvalidOperationException("Model readiness could not be confirmed");
            }
            else
            {
                using var status = JsonDocument.Parse(await http.GetStringAsync("/cloud_status", cancellation.Token));
                if (Get(status.RootElement, "available") != "True" || Get(status.RootElement, "authenticated") != "True")
                    throw new InvalidOperationException("GPT connection unavailable; check the existing sign-in in Settings");
            }
            cancellation.Token.ThrowIfCancellationRequested();
            // Preparation is cancellable and never changes selection. Once
            // saving begins, finish or refresh the server's actual selection.
            modelCommitting = true; cancellation.CancelAfter(Timeout.InfiniteTimeSpan); UpdateControlAvailability();
            chatActivity = "Saving model selection"; UpdateChatProgress();
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(15));
            using var response = await http.PostAsJsonAsync("/models/select", new { provider = parts[0], name = parts[1] }, timeout.Token);
            using var selected = JsonDocument.Parse(await response.Content.ReadAsStringAsync(timeout.Token));
            if (!response.IsSuccessStatusCode) throw new InvalidOperationException(Get(selected.RootElement, "error", "Model selection failed"));
            provider = Get(selected.RootElement, "provider", parts[0]); model = Get(selected.RootElement, "active", model);
            TurnStatus.Text = parts[0] == "local" ? "Model loaded · conversation retained" : "GPT connected · conversation retained";
            RenderCanvas();
        }
        catch (OperationCanceledException) when (!modelCommitting)
        { TurnStatus.Text = "Stopped waiting · previous model selected"; await LoadModelsAsync(); }
        catch (Exception error)
        { await LoadModelsAsync(); TurnStatus.Text = "Model switch failed: " + error.Message; }
        finally
        {
            modelSwitchCancellation = null; modelCommitting = false; selecting = false;
            chatProgressTimer.Stop(); chatClock.Stop(); ChatProgress.Visibility = Visibility.Collapsed;
            UpdateControlAvailability();
            await RefreshModelReadinessAsync();
            if (currentSection == "Settings") await RefreshSectionAsync("Settings");
        }
    }

    private async void AttachButton_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            var picker = new Windows.Storage.Pickers.FileOpenPicker();
            picker.FileTypeFilter.Add(".txt"); picker.FileTypeFilter.Add(".md"); picker.FileTypeFilter.Add(".json");
            picker.FileTypeFilter.Add(".csv"); picker.FileTypeFilter.Add(".png"); picker.FileTypeFilter.Add(".jpg");
            picker.FileTypeFilter.Add(".jpeg"); picker.FileTypeFilter.Add(".pdf");
            WinRT.Interop.InitializeWithWindow.Initialize(picker, WinRT.Interop.WindowNative.GetWindowHandle(((App)Application.Current).MainWindow!));
            var file = await picker.PickSingleFileAsync();
            if (file == null) return;
            var extension = System.IO.Path.GetExtension(file.Path).ToLowerInvariant();
            if (extension is ".png" or ".jpg" or ".jpeg") attachments.Add((file.Name, "", await File.ReadAllBytesAsync(file.Path)));
            else if (extension == ".pdf")
            {
                var data = Convert.ToBase64String(await File.ReadAllBytesAsync(file.Path));
                using var response = await http.PostAsJsonAsync("/extract", new { name = file.Name, data });
                response.EnsureSuccessStatusCode();
                using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
                attachments.Add((file.Name, Get(doc.RootElement, "text"), null));
            }
            else attachments.Add((file.Name, await File.ReadAllTextAsync(file.Path), null));
            TurnStatus.Text = attachments.Count + " attachment(s) ready";
        }
        catch (Exception error) { TurnStatus.Text = "Attachment failed: " + error.Message; }
    }

    private void SetState(string state)
    {
        orbState = state is "transcribing" or "preparing_speech" or "model_loading" ? "thinking" : state;
        VoiceStateLabel.Text = state switch { "transcribing" => "Transcribing", "preparing_speech" => "Preparing voice", "model_loading" => "Preparing model", "listening" => "Listening", "thinking" => "Thinking",
            "speaking" => "Speaking", "muted" => "Microphone off", "failed" => "Needs attention", _ => "Idle · ready" };
        VoiceHint.Text = state switch { "transcribing" => "Turning speech into text", "preparing_speech" => "Microphone paused", "model_loading" => "Waiting for the selected model", "listening" => "Pause when you have finished", "thinking" => "Working through it", "speaking" => "Replying aloud", "failed" => "Try again", _ => "Speak naturally…" };
        OrbGlyph.Text = state == "muted" ? "⦸" : state == "failed" ? "!" : "✦";
        var dim = state == "muted";
        var bright = state is "listening" or "thinking" or "speaking";
        var crossing = state == "thinking";
        ThirdRing.Width = ThirdGlow.Width = crossing ? 172 : 122;
        ThirdRing.Height = ThirdGlow.Height = crossing ? 139 : 122;
        OuterRing.Opacity = dim ? .16 : bright ? .98 : .87;
        SecondRing.Opacity = dim ? .12 : bright ? .92 : .79;
        ThirdRing.Opacity = dim ? .08 : crossing ? .96 : .36;
        DashedRing.Opacity = dim ? .08 : crossing ? .75 : .5;
        OuterGlow.Opacity = dim ? .03 : bright ? .33 : .25;
        SecondGlow.Opacity = dim ? .03 : bright ? .3 : .22;
        ThirdGlow.Opacity = dim ? .02 : crossing ? .3 : .08;
        CoreGlow.Opacity = dim ? .3 : bright ? 1 : .81;
        WaveBars.Opacity = state is "listening" or "speaking" ? .94 : 0;
    }
    private void AnimateOrb()
    {
        if (orbState == "muted") return;
        var now = animationClock.Elapsed.TotalSeconds;
        var elapsed = Math.Clamp(now - lastAnimationTime, 0, .08);
        lastAnimationTime = now;
        phase += (orbState == "thinking" ? 2.15 : orbState is "listening" or "speaking" ? 1.83 : .75) * elapsed;
        var breathPeriod = orbState == "idle" ? 4.8 : orbState == "thinking" ? 3.8 : 3.3;
        breathPhase += 2 * Math.PI * elapsed / breathPeriod;
        var breathPosition = breathPhase / (2 * Math.PI) % 1;
        static double Ease(double value) => value * value * (3 - 2 * value);
        var breath = breathPosition < .38 ? Ease(breathPosition / .38) : 1 - Ease((breathPosition - .38) / .62);
        var energy = orbState == "listening" ? Math.Clamp(microphoneLevel * 11, 0, 1) :
            orbState == "speaking" && outputReader != null && speechEnvelope.Length > 0 ?
                speechEnvelope[Math.Min(speechEnvelope.Length - 1, (int)(outputReader.CurrentTime.TotalSeconds * 20))] : 0;
        displayedVoiceEnergy = displayedVoiceEnergy * .76 + energy * .24;
        var active = orbState is "listening" or "thinking" or "speaking";
        var brightness = active ? .91 : .8;
        OuterRing.Opacity = Math.Clamp(brightness + .09 * Math.Sin(phase * .8) + .07 * displayedVoiceEnergy, .5, 1);
        SecondRing.Opacity = Math.Clamp(brightness - .07 + .11 * Math.Sin(phase * .67 + 2) + .07 * displayedVoiceEnergy, .5, 1);
        ThirdRing.Opacity = Math.Clamp((orbState == "thinking" ? .89 : .34) + .08 * Math.Sin(phase * .9 + 4), .22, 1);
        DashedRing.Opacity = Math.Clamp((orbState == "thinking" ? .68 : .47) + .1 * Math.Sin(phase * .8 + 1), .2, .82);
        OuterGlow.Opacity = .21 + (active ? .09 : 0) + .04 * Math.Sin(phase * .85) + .06 * breath;
        SecondGlow.Opacity = .18 + (active ? .08 : 0) + .04 * Math.Sin(phase * .8 + 2) + .06 * breath;
        ThirdGlow.Opacity = .08 + (orbState == "thinking" ? .19 : 0) + .03 * Math.Sin(phase + 4);
        CoreGlow.Opacity = Math.Clamp(.54 + .28 * breath + (active ? .1 : 0) + .1 * displayedVoiceEnergy, .45, 1);
        CoreAura.Opacity = .45 + .22 * breath;
        if (OrbViewbox.RenderTransform is ScaleTransform orbitScale)
            orbitScale.ScaleX = orbitScale.ScaleY = .985 + .035 * breath;
        if (CoreGlow.RenderTransform is ScaleTransform glowScale)
            glowScale.ScaleX = glowScale.ScaleY = .85 + .3 * breath + .025 * displayedVoiceEnergy;
        if (CoreAura.RenderTransform is ScaleTransform auraScale)
            auraScale.ScaleX = auraScale.ScaleY = .94 + .14 * breath;
        if (CoreVisual.RenderTransform is ScaleTransform coreScale)
            coreScale.ScaleX = coreScale.ScaleY = (orbState == "speaking" ? 1.04 : orbState == "listening" ? 1.02 : 1)
                * (.94 + .14 * breath) + .025 * displayedVoiceEnergy;
        var firstAngle = (orbState == "thinking" ? -29 : -18) + 7 * Math.Sin(phase * .27);
        var secondAngle = (orbState == "idle" ? 7 : orbState == "thinking" ? 37 : 19) - 8 * Math.Sin(phase * .23 + 1);
        var thirdAngle = orbState == "thinking" ? -62 + 16 * Math.Sin(phase * .32 + 2) : 0;
        var dashAngle = 18 + 7 * Math.Sin(phase * .31 + 1);
        if (OuterRing.RenderTransform is RotateTransform first) first.Angle = firstAngle;
        if (OuterGlow.RenderTransform is RotateTransform firstGlow) firstGlow.Angle = firstAngle;
        if (SecondRing.RenderTransform is RotateTransform second) second.Angle = secondAngle;
        if (SecondGlow.RenderTransform is RotateTransform secondGlow) secondGlow.Angle = secondAngle;
        if (ThirdRing.RenderTransform is RotateTransform third) third.Angle = thirdAngle;
        if (ThirdGlow.RenderTransform is RotateTransform thirdGlow) thirdGlow.Angle = thirdAngle;
        if (DashedRing.RenderTransform is RotateTransform dashed) dashed.Angle = dashAngle;
        foreach (var (halo, point, rayH, rayV, orbit, offset) in orbitLights)
        {
            var (radiusX, radiusY, angle, speed) = orbit switch
            {
                0 => (108.0, 47.0, firstAngle, 1.0),
                1 => (102.5, 52.5, secondAngle, -.78),
                _ => orbState == "thinking" ? (86.0, 69.5, thirdAngle, .64) : (61.0, 61.0, 0.0, .45)
            };
            var position = phase * speed + offset;
            var x = radiusX * Math.Cos(position);
            var y = radiusY * Math.Sin(position);
            var rotation = angle * Math.PI / 180;
            var centerX = 112.5 + x * Math.Cos(rotation) - y * Math.Sin(rotation);
            var centerY = 83 + x * Math.Sin(rotation) + y * Math.Cos(rotation);
            var tangentX = -radiusX * Math.Sin(position) * Math.Cos(rotation) - radiusY * Math.Cos(position) * Math.Sin(rotation);
            var tangentY = -radiusX * Math.Sin(position) * Math.Sin(rotation) + radiusY * Math.Cos(position) * Math.Cos(rotation);
            if (rayH.RenderTransform is RotateTransform trail) trail.Angle = Math.Atan2(tangentY, tangentX) * 180 / Math.PI;
            Canvas.SetLeft(halo, centerX - halo.Width / 2);
            Canvas.SetTop(halo, centerY - halo.Height / 2);
            Canvas.SetLeft(point, centerX - point.Width / 2);
            Canvas.SetTop(point, centerY - point.Height / 2);
            Canvas.SetLeft(rayH, centerX - rayH.Width / 2);
            Canvas.SetTop(rayH, centerY - rayH.Height / 2);
            Canvas.SetLeft(rayV, centerX - rayV.Width / 2);
            Canvas.SetTop(rayV, centerY - rayV.Height / 2);
            var shimmer = (1 + Math.Sin(phase * 1.4 + offset)) / 2;
            halo.Opacity = .3 + .27 * shimmer;
            point.Opacity = .79 + .21 * shimmer;
            rayH.Opacity = rayV.Opacity = (orbit == 2 && orbState != "thinking" ? .17 : .34) + .32 * shimmer;
        }
        for (int i = 0; i < waveRectangles.Count; i++)
        {
            var lobe = Math.Abs(Math.Sin(Math.PI * i * 2 / (waveRectangles.Count - 1)));
            var ripple = .4 + .6 * Math.Abs(Math.Sin(i * .62 + phase * 2.4));
            waveRectangles[i].Height = 3 + (24 + 18 * displayedVoiceEnergy) * lobe * ripple;
        }
    }

    private void VoiceButton_Click(object sender, RoutedEventArgs e)
    {
        if (busy || transcribing || selecting) return;
        if (dictating && recording) { StopCapture(true); return; }
        EndVoiceMode();
        dictating = true;
        VoiceButton.Content = "■";
        ToolTipService.SetToolTip(VoiceButton, "Finish dictation");
        StartCapture();
    }

    private bool voiceStarting;
    private string? voiceStartupError;
    private bool voiceErrorDialogOpen;

    private async Task ShowVoiceFailureAsync(string message)
    {
        EndVoiceMode(); voiceStartupError = message;
        SetState("failed"); TurnStatus.Text = "Voice unavailable — click Retry voice";
        VoiceModeButton.Content = "Retry voice"; UpdateControlAvailability();
        try { File.AppendAllText(System.IO.Path.Combine(NativeRuntime.InstallRoot, "voice-diagnostics.log"),
            DateTime.UtcNow.ToString("O") + " " + message + Environment.NewLine); } catch { }
        if (voiceErrorDialogOpen) return;
        voiceErrorDialogOpen = true;
        try
        {
            var dialog = new ContentDialog { Title = "Voice could not start", Content = message,
                CloseButtonText = "Close", PrimaryButtonText = "Retry voice", XamlRoot = XamlRoot };
            if (await dialog.ShowAsync() == ContentDialogResult.Primary)
                VoiceModeButton_Click(VoiceModeButton, new RoutedEventArgs());
        }
        catch { /* The status and Retry voice button remain available. */ }
        finally { voiceErrorDialogOpen = false; }
    }

    private async void VoiceModeButton_Click(object sender, RoutedEventArgs e)
    {
        if (voiceEnabled)
        {
            resumeVoiceAfterStop = false;
            if (chatCancellation != null) { userStoppedChat = true; await StopChatAsync(); }
            else EndVoiceMode();
            return;
        }
        if (busy || transcribing || selecting || modelSwitchCancellation != null)
        { TurnStatus.Text = "Stop the reply or finish model loading/transcription before starting voice"; return; }
        if (Composer.Text.Trim().Length > 0 || attachments.Count > 0)
        { TurnStatus.Text = "Send your draft before starting voice mode"; return; }
        EndVoiceMode(); voiceStartupError = null; voiceStarting = true;
        voiceEnabled = autoVoice = true;
        var generation = voiceSessionGeneration;
        VoiceModeButton.Content = "Cancel voice startup"; UpdateControlAvailability();
        TurnStatus.Text = "Preparing voice — click Cancel to stop"; SetState("preparing_speech");
        try
        {
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(15));
            using var response = await http.PostAsJsonAsync("/voice/warmup", new { }, timeout.Token);
            response.EnsureSuccessStatusCode();
            if (generation == voiceSessionGeneration && voiceEnabled)
            { TurnStatus.Text = "Starting speaker echo cancellation"; await StartNaturalVoiceAsync(generation); }
            if (generation == voiceSessionGeneration && voiceEnabled) VoiceModeButton.Content = "End voice";
        }
        catch (Exception error)
        {
            if (generation != voiceSessionGeneration) return; // Explicit user cancellation.
            await ShowVoiceFailureAsync(VoiceStartupError.Describe(error));
        }
        finally
        {
            if (generation == voiceSessionGeneration) voiceStarting = false;
            UpdateControlAvailability();
        }
    }

    private void EndVoiceMode()
    {
        voiceSessionGeneration++; voiceStarting = false;
        StopNaturalVoice();
        voiceEnabled = autoVoice = dictating = false;
        transcriptionCancellation?.Cancel();
        speechCancellation?.Cancel();
        speechGeneration++;
        StopCapture(false); StopPlayback();
        VoiceButton.Content = "🎙";
        VoiceModeButton.Content = "Voice mode";
        ToolTipService.SetToolTip(VoiceButton, "Dictate a message");
        UpdateControlAvailability();
        if (!busy && !transcribing && !selecting) { SetState("idle"); TurnStatus.Text = "Voice mode off"; }
    }

    private async Task ResumeVoiceCaptureAsync()
    {
        if (naturalVoice || resumeCapturePending || !voiceEnabled || !autoVoice) return;
        var generation = voiceSessionGeneration;
        resumeCapturePending = true;
        try
        {
            // Keep the microphone closed while the speaker's final audio decays.
            await Task.Delay(350);
            if (generation == voiceSessionGeneration && voiceEnabled && autoVoice && !busy && !transcribing && !selecting)
                StartCapture();
        }
        finally { resumeCapturePending = false; }
    }

    private void StartCapture()
    {
        if ((!voiceEnabled && !dictating) || busy || transcribing || selecting || recording || speechCancellation != null) return;
        try
        {
            audioBuffer = new MemoryStream();
            recorder = new WaveFileWriter(audioBuffer, new WaveFormat(16000, 16, 1));
            input = new WaveInEvent { WaveFormat = new WaveFormat(16000, 16, 1), BufferMilliseconds = 100 };
            var capture = input;
            var writer = recorder;
            var generation = voiceSessionGeneration;
            var speechFrames = 0;
            var finishQueued = false;
            input.DataAvailable += (_, args) =>
            {
                lock (captureAudioLock)
                {
                    if (!recording || generation != voiceSessionGeneration || !ReferenceEquals(input, capture)) return;
                    writer.Write(args.Buffer, 0, args.BytesRecorded);
                    var samples = args.BytesRecorded / 2;
                    double sum = 0;
                    for (int i = 0; i + 1 < args.BytesRecorded; i += 2)
                    { var sample = BitConverter.ToInt16(args.Buffer, i) / 32768.0; sum += sample * sample; }
                    var rms = Math.Sqrt(sum / Math.Max(samples, 1));
                    microphoneLevel = rms;
                    if (rms > .018) { speechFrames++; heardSpeech = speechFrames >= 3; lastSpeech = DateTime.UtcNow; }
                    var pause = dictating ? 1500 : 1100;
                    if (!finishQueued && ((heardSpeech && DateTime.UtcNow - lastSpeech > TimeSpan.FromMilliseconds(pause)) ||
                        DateTime.UtcNow - listenStart > TimeSpan.FromSeconds(29)))
                    {
                        finishQueued = true;
                        DispatcherQueue.TryEnqueue(() =>
                        {
                            if (generation == voiceSessionGeneration && ReferenceEquals(input, capture)) StopCapture(true);
                        });
                    }
                }
            };
            listenStart = DateTime.UtcNow; lastSpeech = listenStart; heardSpeech = false; recording = true;
            input.StartRecording(); SetState("listening");
            TurnStatus.Text = dictating ? "Dictating · click mic to finish" : "Listening · pause when finished";
            UpdateControlAvailability();
        }
        catch (Exception error)
        {
            EndVoiceMode(); SetState("failed"); TurnStatus.Text = "Microphone unavailable: " + error.Message;
        }
    }

    private void StopCapture(bool transcribe)
    {
        if (!recording) return;
        var wasDictating = dictating;
        recording = false;
        if (wasDictating)
        {
            dictating = false; VoiceButton.Content = "🎙";
            ToolTipService.SetToolTip(VoiceButton, "Dictate a message");
        }
        try { input?.StopRecording(); input?.Dispose(); input = null; }
        catch { }
        byte[]? bytes;
        lock (captureAudioLock)
        {
            recorder?.Dispose(); recorder = null;
            bytes = audioBuffer?.ToArray();
            audioBuffer?.Dispose(); audioBuffer = null;
        }
        if (transcribe && heardSpeech && bytes is { Length: > 6400 }) _ = TranscribeAsync(bytes, wasDictating);
        else if (transcribe && voiceEnabled && autoVoice) _ = ResumeVoiceCaptureAsync();
        else
        {
            SetState("idle");
            if (wasDictating && transcribe) TurnStatus.Text = "No speech detected · try again";
        }
        UpdateControlAvailability();
    }

    private async Task TranscribeAsync(byte[] wav, bool dictation = false)
    {
        if (transcribing || busy || selecting) return;
        var generation = voiceSessionGeneration;
        using var cancellation = new CancellationTokenSource(TimeSpan.FromSeconds(90));
        transcriptionCancellation = cancellation;
        transcribing = true; UpdateControlAvailability();
        try
        {
            TurnStatus.Text = "Transcribing…"; SetState("transcribing");
            using var content = new MultipartFormDataContent();
            content.Add(new ByteArrayContent(wav), "audio", "speech.wav");
            using var response = await http.PostAsync("/transcribe", content, cancellation.Token);
            response.EnsureSuccessStatusCode();
            using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync(cancellation.Token));
            if (generation != voiceSessionGeneration) return;
            var words = Get(doc.RootElement, "text").Trim();
            if (words.Length == 0) { SetState("idle"); TurnStatus.Text = "No speech detected · try again"; return; }
            if (dictation || Composer.Text.Trim().Length > 0)
            {
                var draft = Composer.Text;
                Composer.Text = draft + (draft.Length > 0 && !char.IsWhiteSpace(draft[^1]) ? " " : "") + words;
                if (!dictation) EndVoiceMode();
                Composer.SelectionStart = Composer.Text.Length; Composer.Focus(FocusState.Programmatic);
                SetState("idle"); TurnStatus.Text = "Transcript ready · review and send";
            }
            else if (voiceEnabled)
            {
                Composer.Text = words;
                await SendAsync(true);
            }
        }
        catch (OperationCanceledException)
        {
            if (generation == voiceSessionGeneration)
            { EndVoiceMode(); SetState("idle"); TurnStatus.Text = "Transcription timed out · try dictation again"; }
        }
        catch (Exception error)
        {
            if (generation != voiceSessionGeneration) return;
            EndVoiceMode(); TurnStatus.Text = "Voice failed: " + error.Message; SetState("failed");
        }
        finally
        {
            transcriptionCancellation = null; transcribing = false; UpdateControlAvailability();
            if (voiceEnabled && autoVoice && !recording && !busy) _ = ResumeVoiceCaptureAsync();
        }
    }

    private int speechGeneration;
    private async Task<bool> SpeakAsync(string words, bool preview = false, CancellationToken cancellationToken = default)
    {
        var generation = ++speechGeneration;
        using var cancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        cancellation.CancelAfter(TimeSpan.FromMinutes(3));
        speechCancellation = cancellation; UpdateControlAvailability();
        try
        {
            foreach (var chunk in SpeechChunks(words))
            {
                if (generation != speechGeneration || (!preview && !voiceEnabled)) return false;
                SetState("preparing_speech"); TurnStatus.Text = "Preparing speech…";
                using var response = await http.PostAsJsonAsync("/speak", new { text = chunk }, cancellation.Token);
                response.EnsureSuccessStatusCode();
                var audio = await response.Content.ReadAsByteArrayAsync(cancellation.Token);
                if (generation != speechGeneration || (!preview && !voiceEnabled)) return false;
                speechEnvelope = BuildSpeechEnvelope(audio);
                outputBuffer = new MemoryStream(audio); outputReader = new WaveFileReader(outputBuffer);
                output = naturalVoice && naturalRenderDevice != null
                    ? new WasapiOut(naturalRenderDevice, NAudio.CoreAudioApi.AudioClientShareMode.Shared, false, 100)
                    : new WaveOutEvent();
                output.Init(outputReader);
                var done = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
                output.PlaybackStopped += (_, args) =>
                { if (args.Exception != null) done.TrySetException(args.Exception); else done.TrySetResult(); };
                SetState("speaking"); TurnStatus.Text = "Speaking · Stop to interrupt and listen";
                if (naturalVoice) playbackGuard.Remember(chunk, Environment.TickCount64);
                output.Play();
                await done.Task.WaitAsync(cancellation.Token);
                StopPlayback();
            }
            SetState(naturalVoice ? "listening" : "idle"); return true;
        }
        catch (OperationCanceledException)
        { SetState(naturalVoice ? "listening" : "idle"); TurnStatus.Text = "Speech stopped"; return false; }
        catch (Exception error)
        { EndVoiceMode(); TurnStatus.Text = "Speech failed: " + error.Message; SetState("failed"); return false; }
        finally { if (naturalVoice) playbackGuard.Remember(words, Environment.TickCount64); StopPlayback(); speechCancellation = null; UpdateControlAvailability(); }
    }

    private static IEnumerable<string> SpeechChunks(string text)
    {
        text = text.Trim();
        while (text.Length > 0)
        {
            var length = Math.Min(1000, text.Length);
            if (length < text.Length)
            {
                var boundary = text.LastIndexOfAny(['.', '!', '?', ' ', '\n'], length - 1, Math.Min(300, length));
                if (boundary > 0) length = boundary + 1;
            }
            var chunk = text[..length].Trim();
            if (chunk.Length > 0) yield return chunk;
            text = text[length..].TrimStart();
        }
    }
    private static double[] BuildSpeechEnvelope(byte[] wav)
    {
        var levels = new List<double>();
        using var reader = new WaveFileReader(new MemoryStream(wav));
        if (reader.WaveFormat.BitsPerSample != 16) return [];
        var chunk = new byte[Math.Max(2, reader.WaveFormat.AverageBytesPerSecond / 20)];
        int count;
        while ((count = reader.Read(chunk, 0, chunk.Length)) > 0)
        {
            double sum = 0;
            for (int i = 0; i + 1 < count; i += 2) { var sample = BitConverter.ToInt16(chunk, i) / 32768.0; sum += sample * sample; }
            levels.Add(Math.Clamp(Math.Sqrt(sum / Math.Max(1, count / 2)) * 4.5, 0, 1));
        }
        return levels.ToArray();
    }
    private void StopPlayback() { try { output?.Stop(); output?.Dispose(); outputReader?.Dispose(); outputBuffer?.Dispose(); } catch { } output = null; outputReader = null; outputBuffer = null; }
}

