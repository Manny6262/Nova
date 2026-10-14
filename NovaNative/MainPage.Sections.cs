using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System.Net.Http.Json;
using System.Text.Json;

namespace NovaNative;

public sealed partial class MainPage
{
    private static readonly string NotesFile = Path.Combine(
        NativeRuntime.InstallRoot, "notes.json");
    private string currentSection = "Chat";
    private int sectionGeneration;

    private void UpdateTasksBadge() => TasksNav.Content = (compact ? "◷" : "◷    Tasks") +
        (pendingProposalCount > 0 ? $"  • {pendingProposalCount}" : "");

    private async Task RefreshProposalBadgeAsync()
    {
        try
        {
            using var doc = JsonDocument.Parse(await http.GetStringAsync("/proposals"));
            using var inbox = JsonDocument.Parse(await http.GetStringAsync("/inbox"));
            pendingProposalCount = doc.RootElement.EnumerateArray().Count(item => Get(item, "status") == "pending") +
                inbox.RootElement.EnumerateArray().Count(item => Get(item, "read_at").Length == 0);
            UpdateTasksBadge();
        }
        catch { /* Engine status is shown elsewhere; keep the previous count. */ }
    }

    private void LoadNotes()
    {
        try
        {
            if (!File.Exists(NotesFile)) return;
            var saved = JsonSerializer.Deserialize<Dictionary<int, string>>(File.ReadAllText(NotesFile));
            if (saved == null) return;
            foreach (var (key, value) in saved) notes[key] = value;
        }
        catch (Exception error) { TurnStatus.Text = "Notes could not be loaded: " + error.Message; }
    }

    private void SaveNotes()
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(NotesFile)!);
            var temporary = NotesFile + ".tmp";
            File.WriteAllText(temporary, JsonSerializer.Serialize(notes));
            File.Move(temporary, NotesFile, true);
        }
        catch (Exception error) { TurnStatus.Text = "Notes could not be saved: " + error.Message; }
    }

    private void ShowSection(string section)
    {
        currentSection = section;
        sectionGeneration++;
        var chat = section == "Chat";
        ConversationPanel.Visibility = chat ? Visibility.Visible : Visibility.Collapsed;
        ComposerPanel.Visibility = chat ? Visibility.Visible : Visibility.Collapsed;
        SectionPanel.Visibility = chat ? Visibility.Collapsed : Visibility.Visible;
        if (chat) return;
        _ = RefreshSectionAsync(section);
    }

    private async Task RefreshSectionAsync(string section)
    {
        var generation = ++sectionGeneration;
        SectionContent.Children.Clear();
        try
        {
            if (section == "Studio") await BuildStudioAsync(generation);
            else if (section == "Library") await BuildLibraryAsync(generation);
            else if (section == "Tasks") await BuildTasksAsync(generation);
            else if (section == "Settings") await BuildSettingsAsync(generation);
        }
        catch (Exception error)
        {
            if (currentSection == section && sectionGeneration == generation)
                SectionContent.Children.Add(Card("Could not load " + section, error.Message, "#F0A1A7"));
        }
    }

    private void SectionHeader(string title, string description)
    {
        SectionContent.Children.Add(Text(title, 23, "#F4F6FF"));
        SectionContent.Children.Add(Text(description, 13, "#AEC3E5"));
    }

    private static Button ActionButton(string label) => new()
    {
        Content = label, Background = Brush("#29446F"), Foreground = Brush("#F4F8FF"),
        BorderBrush = Brush("#6B91CC"), BorderThickness = new Thickness(1),
        CornerRadius = new CornerRadius(9), Padding = new Thickness(14, 8, 14, 8),
        HorizontalAlignment = HorizontalAlignment.Left
    };

    private async Task BuildStudioAsync(int generation)
    {
        SectionHeader("Studio", "Create with Nova and revisit images and videos from your conversations.");
        var prompt = new TextBox { PlaceholderText = "Describe what you want to create…",
            TextWrapping = TextWrapping.Wrap, AcceptsReturn = true, MinHeight = 90, MaxHeight = 150 };
        SectionContent.Children.Add(prompt);
        var actions = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10 };
        var image = ActionButton("Create image");
        var video = ActionButton("Create video");
        image.Click += async (_, _) => await StudioPromptAsync(prompt.Text, "image");
        video.Click += async (_, _) => await StudioPromptAsync(prompt.Text, "video");
        actions.Children.Add(image); actions.Children.Add(video);
        SectionContent.Children.Add(actions);
        SectionContent.Children.Add(Text("RECENT CREATIONS", 11, "#8FDBF9"));

        var found = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var (type, url) in mediaEvents.AsEnumerable().Reverse())
        {
            if (found.Add(url)) SectionContent.Children.Add(MediaCard(type, url, true));
        }
        using var sessions = JsonDocument.Parse(await http.GetStringAsync("/sessions"));
        if (generation != sectionGeneration) return;
        foreach (var session in sessions.RootElement.EnumerateArray().Take(30))
        {
            var id = Get(session, "id");
            using var history = JsonDocument.Parse(await http.GetStringAsync("/sessions/" + id));
            if (generation != sectionGeneration) return;
            foreach (var message in history.RootElement.EnumerateArray())
                foreach (System.Text.RegularExpressions.Match match in MediaPattern.Matches(Get(message, "content")))
                {
                    var url = match.Groups[1].Value;
                    if (!found.Add(url)) continue;
                    var type = url.EndsWith(".mp4", StringComparison.OrdinalIgnoreCase) ? "video" : "image";
                    SectionContent.Children.Add(MediaCard(type, url, true));
                }
        }
        if (found.Count == 0)
            SectionContent.Children.Add(Card("Nothing here yet", "Create an image or video and it will appear here.", "#B5D6FF"));
    }

    private async Task StudioPromptAsync(string description, string kind)
    {
        description = description.Trim();
        if (description.Length == 0) { TurnStatus.Text = "Describe your " + kind + " first"; return; }
        ShowSection("Chat"); UpdateNavStyle("Chat"); WelcomeTitle.Text = welcomeGreeting;
        Composer.Text = "Create a " + kind + " of " + description;
        await SendAsync(false);
    }

    private async Task BuildConversationLibraryAsync(int generation)
    {
        var search = new TextBox { PlaceholderText = "Search conversations…" };
        var conversations = new StackPanel { Spacing = 9 };
        SectionContent.Children.Add(search);
        SectionContent.Children.Add(Text("CONVERSATIONS", 11, "#8FDBF9"));
        SectionContent.Children.Add(conversations);
        using var sessions = JsonDocument.Parse(await http.GetStringAsync("/sessions"));
        if (generation != sectionGeneration) return;
        var entries = sessions.RootElement.EnumerateArray()
            .Select(item => (id: item.GetProperty("id").GetInt32(), title: Get(item, "title", "New chat")))
            .ToArray();
        void RenderConversations()
        {
            conversations.Children.Clear();
            foreach (var (id, title) in entries.Where(entry => entry.title.Contains(search.Text, StringComparison.OrdinalIgnoreCase)).Take(80))
            {
                var open = ActionButton(title);
                open.HorizontalAlignment = HorizontalAlignment.Stretch;
                open.HorizontalContentAlignment = HorizontalAlignment.Left;
                open.Click += async (_, _) =>
                {
                    await LoadSessionAsync(id);
                    ShowSection("Chat"); UpdateNavStyle("Chat"); WelcomeTitle.Text = welcomeGreeting;
                };
                conversations.Children.Add(open);
            }
            if (conversations.Children.Count == 0) conversations.Children.Add(Text("No conversations match.", 13));
        }
        search.TextChanged += (_, _) => RenderConversations();
        RenderConversations();

    }

    private async Task BuildTasksAsync(int generation)
    {
        await EnsureTaskHostAsync();
        SectionHeader("Tasks", "Ongoing responsibilities, saved progress, and decisions for you.");
        using var control = JsonDocument.Parse(await http.GetStringAsync("/background-control"));
        if (generation != sectionGeneration) return;
        var backgroundEnabled = control.RootElement.GetProperty("enabled").GetBoolean();
        var workSwitch = ActionButton(backgroundEnabled ? "Pause all background work" : "Enable background work");
        workSwitch.Click += async (_, _) => await SetBackgroundEnabledAsync(!backgroundEnabled);
        SectionContent.Children.Add(workSwitch);
        SectionContent.Children.Add(Text(backgroundEnabled
            ? "Nova may run approved local tasks. Changes still require your approval."
            : "Background work is paused. Enable it, then resume individual tasks when ready.", 12, "#AEC3E5"));
        await BuildFixTasksAsync(generation);
        if (generation != sectionGeneration) return;
        await BuildResponsibilitiesAsync(generation);
        if (generation != sectionGeneration) return;
        SectionContent.Children.Add(Text("INBOX", 11, "#8FDBF9"));
        using var inbox = JsonDocument.Parse(await http.GetStringAsync("/inbox"));
        if (generation != sectionGeneration) return;
        var unread = inbox.RootElement.EnumerateArray().Where(item => Get(item, "read_at").Length == 0).Take(20).ToArray();
        if (unread.Length == 0)
            SectionContent.Children.Add(Card("No new results", "Completed work and decisions will appear here even after Nova restarts.", "#B5D6FF"));
        foreach (var notice in unread)
        {
            var noticeId = notice.GetProperty("id").GetInt32();
            SectionContent.Children.Add(Card(Get(notice, "title", "Nova update") + " · " + Get(notice, "kind"),
                Get(notice, "detail"), "#B4E5D7"));
            var markRead = ActionButton("Mark read");
            var reference = Get(notice, "ref_id");
            if (reference.StartsWith("responsibility:", StringComparison.Ordinal))
            {
                var inspect = ActionButton("Open responsibility");
                inspect.Click += async (_, _) => await InspectResponsibilityAsync(reference[15..]);
                SectionContent.Children.Add(inspect);
            }
            if (reference.StartsWith("fix:", StringComparison.Ordinal))
            {
                var inspect = ActionButton("Review candidate fix");
                inspect.Click += async (_, _) => await InspectFixTaskAsync(reference[4..]);
                SectionContent.Children.Add(inspect);
            }
            markRead.Click += async (_, _) => await MarkInboxReadAsync(noticeId);
            SectionContent.Children.Add(markRead);
        }
        SectionContent.Children.Add(Text("SUGGESTIONS FOR YOU", 11, "#8FDBF9"));
        using var proposals = JsonDocument.Parse(await http.GetStringAsync("/proposals"));
        if (generation != sectionGeneration) return;
        var pending = proposals.RootElement.EnumerateArray()
            .Where(item => Get(item, "status") == "pending").Take(30).ToArray();
        if (pending.Length == 0)
            SectionContent.Children.Add(Card("All caught up", "Nova will bring you a specific suggestion when a check needs attention.", "#B5D6FF"));
        foreach (var suggestion in pending)
        {
            var id = suggestion.GetProperty("id").GetInt32();
            SectionContent.Children.Add(Card(Get(suggestion, "title", "Suggestion"),
                Get(suggestion, "detail") + "\n\nProposed task: " + Get(suggestion, "prompt"), "#B4E5D7"));
            var decisionRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10 };
            var approve = ActionButton("Approve task");
            var dismiss = ActionButton("Dismiss");
            approve.Click += async (_, _) => await ProposalActionAsync(id, "approve");
            dismiss.Click += async (_, _) => await ProposalActionAsync(id, "dismiss");
            decisionRow.Children.Add(approve); decisionRow.Children.Add(dismiss);
            SectionContent.Children.Add(decisionRow);
        }
        SectionContent.Children.Add(Text("BACKGROUND WORK", 11, "#8FDBF9"));
        var refreshWork = ActionButton("Refresh progress");
        refreshWork.Click += async (_, _) => await RefreshSectionAsync("Tasks");
        SectionContent.Children.Add(refreshWork);
        using var jobs = JsonDocument.Parse(await http.GetStringAsync("/jobs"));
        if (generation != sectionGeneration) return;
        var approvedWork = jobs.RootElement.EnumerateArray().Take(30).ToArray();
        if (approvedWork.Length == 0)
            SectionContent.Children.Add(Card("No background work yet", "Approve a suggestion or create a schedule to begin.", "#B5D6FF"));
        foreach (var job in approvedWork)
        {
            var jobId = Get(job, "id");
            var status = Get(job, "status");
            var result = Get(job, "result");
            var error = Get(job, "error");
            var detail = Get(job, "stage", status) +
                (result.Length > 0 ? "\n\n" + result[..Math.Min(result.Length, status == "completed" ? 800 : 350)] : "") +
                (error.Length > 0 && (status is "failed" or "interrupted") ? "\n\n" + error : "");
            SectionContent.Children.Add(Card(Get(job, "name", "Nova task") + " · " + status,
                detail, status == "failed" ? "#F0A1A7" : "#B4E5D7"));
            if (status == "waiting_approval")
            {
                var review = ActionButton("Review requested action");
                review.Click += async (_, _) => await ReviewJobActionAsync(jobId);
                SectionContent.Children.Add(review);
            }
            var actions = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10 };
            if (status is "queued" or "running" or "waiting_approval")
            {
                var pause = ActionButton("Pause");
                var cancel = ActionButton("Cancel");
                pause.Click += async (_, _) => await JobActionAsync(jobId, "pause");
                cancel.Click += async (_, _) => await JobActionAsync(jobId, "cancel");
                actions.Children.Add(pause); actions.Children.Add(cancel);
            }
            else if (status == "paused")
            {
                var resume = ActionButton("Resume");
                var cancel = ActionButton("Cancel");
                resume.Click += async (_, _) => await JobActionAsync(jobId, "resume");
                cancel.Click += async (_, _) => await JobActionAsync(jobId, "cancel");
                actions.Children.Add(resume); actions.Children.Add(cancel);
            }
            else if (status is "failed" or "cancelled" or "interrupted")
            {
                var retry = ActionButton("Retry");
                retry.Click += async (_, _) => await JobActionAsync(jobId, "retry");
                actions.Children.Add(retry);
            }
            if (actions.Children.Count > 0) SectionContent.Children.Add(actions);
        }
        SectionContent.Children.Add(Text("YOUR SCHEDULED TASKS", 11, "#8FDBF9"));
        var name = new TextBox { PlaceholderText = "Task name" };
        var prompt = new TextBox { PlaceholderText = "What should Nova do?", AcceptsReturn = true,
            TextWrapping = TextWrapping.Wrap, MinHeight = 75 };
        var scheduleType = new ComboBox { MinWidth = 160 };
        scheduleType.Items.Add(new ComboBoxItem { Content = "Daily", Tag = "daily" });
        scheduleType.Items.Add(new ComboBoxItem { Content = "Every N minutes", Tag = "interval" });
        scheduleType.SelectedIndex = 0;
        var schedule = new TextBox { PlaceholderText = "09:00", Text = "09:00", Width = 120 };
        scheduleType.SelectionChanged += (_, _) =>
        {
            var daily = scheduleType.SelectedIndex == 0;
            schedule.PlaceholderText = daily ? "09:00" : "60";
            schedule.Text = daily ? "09:00" : "60";
        };
        var scheduleRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10 };
        scheduleRow.Children.Add(scheduleType); scheduleRow.Children.Add(schedule);
        var create = ActionButton("Create task");
        create.Click += async (_, _) =>
        {
            if (string.IsNullOrWhiteSpace(name.Text) || string.IsNullOrWhiteSpace(prompt.Text) || string.IsNullOrWhiteSpace(schedule.Text))
            { TurnStatus.Text = "Complete the task name, instruction, and schedule"; return; }
            try
            {
                using var response = await http.PostAsJsonAsync("/scheduled", new { name = name.Text.Trim(), prompt = prompt.Text.Trim(),
                    schedule_type = ((ComboBoxItem)scheduleType.SelectedItem).Tag, schedule_value = schedule.Text.Trim() });
                response.EnsureSuccessStatusCode();
                TurnStatus.Text = "Task created";
                await RefreshSectionAsync("Tasks");
            }
            catch (Exception error) { TurnStatus.Text = "Task could not be created: " + error.Message; }
        };
        SectionContent.Children.Add(name); SectionContent.Children.Add(prompt);
        SectionContent.Children.Add(scheduleRow); SectionContent.Children.Add(create);
        SectionContent.Children.Add(Text("SCHEDULED", 11, "#8FDBF9"));
        using var tasks = JsonDocument.Parse(await http.GetStringAsync("/scheduled"));
        if (generation != sectionGeneration) return;
        if (tasks.RootElement.GetArrayLength() == 0)
            SectionContent.Children.Add(Card("No scheduled tasks", "Create one above when you need a recurring check.", "#B5D6FF"));
        foreach (var task in tasks.RootElement.EnumerateArray().Take(80))
        {
            var id = task.GetProperty("id").GetInt32();
            var enabled = Get(task, "enabled") is "1" or "True";
            var detail = Get(task, "prompt") + "\n" + Get(task, "schedule_type") + " · " + Get(task, "schedule_value") +
                (enabled ? " · on" : " · paused");
            SectionContent.Children.Add(Card(Get(task, "name", "Task"), detail, enabled ? "#B4E5D7" : "#C3CEE8"));
            var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10 };
            var toggle = ActionButton(enabled ? "Pause" : "Resume");
            var run = ActionButton("Run now");
            toggle.Click += async (_, _) => await TaskActionAsync(id, "toggle");
            run.Click += async (_, _) => await TaskActionAsync(id, "run");
            row.Children.Add(toggle); row.Children.Add(run);
            SectionContent.Children.Add(row);
        }
    }

    private async Task ProposalActionAsync(int id, string decision)
    {
        try
        {
            using var response = await http.PostAsJsonAsync($"/proposals/{id}/decision", new { decision });
            response.EnsureSuccessStatusCode();
            TurnStatus.Text = decision == "approve" ? "Nova is working on your approved task" : "Suggestion dismissed";
            await RefreshSectionAsync("Tasks");
            await RefreshProposalBadgeAsync();
        }
        catch (Exception error) { TurnStatus.Text = "Could not update suggestion: " + error.Message; }
    }

    private async Task MarkInboxReadAsync(int id)
    {
        try
        {
            using var response = await http.PostAsJsonAsync($"/inbox/{id}/read", new { });
            response.EnsureSuccessStatusCode();
            await RefreshSectionAsync("Tasks");
            await RefreshProposalBadgeAsync();
        }
        catch (Exception error) { TurnStatus.Text = "Could not mark update read: " + error.Message; }
    }

    private async Task JobActionAsync(string id, string action)
    {
        try
        {
            using var response = await http.PostAsJsonAsync($"/jobs/{id}/{action}", new { });
            response.EnsureSuccessStatusCode();
            TurnStatus.Text = "Background task " + action + " requested";
            await RefreshSectionAsync("Tasks");
            await RefreshProposalBadgeAsync();
        }
        catch (Exception error) { TurnStatus.Text = "Task action failed: " + error.Message; }
    }

    private async Task SetBackgroundEnabledAsync(bool enabled)
    {
        try
        {
            using var response = await http.PostAsJsonAsync("/background-control", new { enabled });
            response.EnsureSuccessStatusCode();
            TurnStatus.Text = enabled ? "Background work enabled" : "Background work paused";
            await RefreshSectionAsync("Tasks");
            await RefreshProposalBadgeAsync();
        }
        catch (Exception error) { TurnStatus.Text = "Could not change background work: " + error.Message; }
    }

    private async Task ReviewJobActionAsync(string id)
    {
        try
        {
            using var detail = JsonDocument.Parse(await http.GetStringAsync($"/jobs/{id}"));
            var job = detail.RootElement;
            if (Get(job, "status") != "waiting_approval") return;
            var request = Get(job, "stage", "Nova requests an action");
            var eventDetail = job.GetProperty("events").EnumerateArray()
                .LastOrDefault(item => Get(item, "kind") == "approval");
            var explanation = eventDetail.ValueKind == JsonValueKind.Object ? Get(eventDetail, "detail") : "";
            var dialog = new ContentDialog { Title = "Approve this action?",
                Content = request + (explanation.Length > 0 ? "\n\n" + explanation : ""),
                PrimaryButtonText = "Allow", CloseButtonText = "Decline",
                DefaultButton = ContentDialogButton.Close, XamlRoot = XamlRoot };
            var approved = await dialog.ShowAsync() == ContentDialogResult.Primary;
            using var response = await http.PostAsJsonAsync($"/jobs/{id}/approval", new { approved });
            response.EnsureSuccessStatusCode();
            await RefreshSectionAsync("Tasks");
        }
        catch (Exception error) { TurnStatus.Text = "Action review failed: " + error.Message; }
    }

    private async Task TaskActionAsync(int id, string action)
    {
        try
        {
            using var response = await http.PostAsJsonAsync($"/scheduled/{id}/{action}", new { });
            response.EnsureSuccessStatusCode();
            TurnStatus.Text = action == "run" ? "Task started" : "Task updated";
            await RefreshSectionAsync("Tasks");
        }
        catch (Exception error) { TurnStatus.Text = "Task action failed: " + error.Message; }
    }

    private async Task BuildSettingsAsync(int generation)
    {
        SectionHeader("Settings", "Choose Nova's model, voice, permissions, and motion.");
        var assembly = typeof(MainPage).Assembly;
        var buildDate = assembly.GetCustomAttributes(typeof(System.Reflection.AssemblyMetadataAttribute), false)
            .Cast<System.Reflection.AssemblyMetadataAttribute>().FirstOrDefault(item => item.Key == "BuildDateUtc")?.Value ?? "Unknown";
        SectionContent.Children.Add(Card("Nova Native " + assembly.GetName().Version?.ToString(3), "Built " + buildDate, "#B5D6FF"));
        using var serviceDoc = JsonDocument.Parse(await http.GetStringAsync("/native-background"));
        if (generation != sectionGeneration) return;
        SectionContent.Children.Add(Text("BACKGROUND AVAILABILITY", 11, "#8FDBF9"));
        foreach (var (key, label) in new[] { ("enabled", "Recover Nova's engine automatically"),
            ("launch_at_login", "Start Nova's background service when I sign in"),
            ("notifications", "Show Windows notifications for new results and decisions") })
        {
            var option = new CheckBox { Content = label, IsChecked = serviceDoc.RootElement.GetProperty(key).GetBoolean() };
            option.Click += async (_, _) =>
            {
                try { await SendRecordAsync(HttpMethod.Post, "/native-background", new Dictionary<string, bool> { [key] = option.IsChecked == true }); }
                catch (Exception error) { option.IsChecked = !option.IsChecked; TurnStatus.Text = error.Message; }
            };
            SectionContent.Children.Add(option);
        }
        SectionContent.Children.Add(Text("Closing the window keeps the engine and iMessage available. Pause all background work in Tasks to stop approved work. Login startup and Windows notifications are optional; no work runs while the PC is asleep or off.", 12));
        if (serviceDoc.RootElement.TryGetProperty("last_notification", out var notification) && notification.ValueKind == JsonValueKind.Object && Get(notification, "status") == "failed")
            SectionContent.Children.Add(Text("Windows notification delivery failed. Your results are still saved in Tasks → Inbox.", 12, "#F0C7A7"));
        SectionContent.Children.Add(Text("PROACTIVE NOVA", 11, "#8FDBF9"));
        using var proactivityDoc = JsonDocument.Parse(await http.GetStringAsync("/proactivity"));
        if (generation != sectionGeneration) return;
        var proactive = new CheckBox { Content = "Prepare suggestions for my review", IsChecked = Get(proactivityDoc.RootElement, "enabled") == "True" };
        proactive.Checked += async (_, _) => await SetProactivityAsync(true);
        proactive.Unchecked += async (_, _) => await SetProactivityAsync(false);
        SectionContent.Children.Add(proactive);
        SectionContent.Children.Add(Text("Nova checks local services and reflects on saved goals once a day. She waits for your approval before starting a suggested task.", 12, "#AEC3E5"));
        SectionContent.Children.Add(Text("CURRENT MODEL", 11, "#8FDBF9"));
        var picker = new ComboBox { MinWidth = 250, IsEnabled = ModelPicker.IsEnabled };
        settingsModelPicker = picker;
        foreach (ComboBoxItem item in ModelPicker.Items)
            picker.Items.Add(new ComboBoxItem { Content = item.Content, Tag = item.Tag });
        picker.SelectedIndex = ModelPicker.SelectedIndex;
        picker.SelectionChanged += (_, _) =>
        {
            if (picker.SelectedItem is not ComboBoxItem selected) return;
            for (var index = 0; index < ModelPicker.Items.Count; index++)
                if ((string)((ComboBoxItem)ModelPicker.Items[index]).Tag == (string)selected.Tag)
                { ModelPicker.SelectedIndex = index; break; }
        };
        SectionContent.Children.Add(picker);
        SectionContent.Children.Add(Text("CAPABILITIES", 11, "#8FDBF9"));
        foreach (var (label, source) in new[] { ("Web tools", ToolsToggle), ("File and code actions", ActionToggle),
            ("Computer control", ComputerToggle) })
        {
            var option = new CheckBox { Content = label, IsChecked = source.IsChecked };
            option.Checked += (_, _) => source.IsChecked = true;
            option.Unchecked += (_, _) => source.IsChecked = false;
            SectionContent.Children.Add(option);
        }
        var motionOption = new CheckBox { Content = "Animate the voice orb", IsChecked = motion };
        motionOption.Checked += (_, _) => { motion = true; MotionButton.Content = "Motion: on"; };
        motionOption.Unchecked += (_, _) => { motion = false; MotionButton.Content = "Motion: off"; };
        SectionContent.Children.Add(motionOption);
        SectionContent.Children.Add(Text("VOICE", 11, "#8FDBF9"));
        SectionContent.Children.Add(Text("The composer microphone turns speech into an editable draft. Review it and press Send for a text reply. The separate Voice mode button starts a spoken conversation; End voice stops listening and playback.", 12, "#AEC3E5"));
        SectionContent.Children.Add(Text("Conversational voice uses Windows speaker echo cancellation. Stop clears pending recognition; End voice closes capture and playback. If echo cancellation cannot start, use dictation. Room and speaker behavior still needs local testing.", 12, "#AEC3E5"));
        SectionContent.Children.Add(Text("SYSTEM", 11, "#8FDBF9"));
        var health = await http.GetStringAsync("/health");
        var voice = await http.GetStringAsync("/voice_status");
        if (generation != sectionGeneration) return;
        using var healthDoc = JsonDocument.Parse(health);
        using var voiceDoc = JsonDocument.Parse(voice);
        SectionContent.Children.Add(Card("Local engine", Get(healthDoc.RootElement, "model_name", "Connected"), "#B4E5D7"));
        SectionContent.Children.Add(Card("Voice", Get(voiceDoc.RootElement, "tts") == "True" ? "Speech available" : "Speech unavailable", "#B4E5D7"));
        using var photonDoc = JsonDocument.Parse(await http.GetStringAsync("/photon/status"));
        if (generation != sectionGeneration) return;
        var photonState = photonDoc.RootElement;
        SectionContent.Children.Add(Card("Nova's iMessage line",
            Get(photonState, "connected") == "True"
                ? "Connected · " + Get(photonState, "line")
                : Get(photonState, "detail", "Not connected"),
            Get(photonState, "connected") == "True" ? "#B4E5D7" : "#F0C7A7"));
        var test = ActionButton("Test Nova's voice");
        test.Click += async (_, _) => await TestVoiceAsync();
        var refresh = ActionButton("Refresh status");
        refresh.Click += async (_, _) => { await LoadModelsAsync(); await RefreshSectionAsync("Settings"); };
        var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10 };
        row.Children.Add(test); row.Children.Add(refresh);
        SectionContent.Children.Add(row);
    }

    private async Task TestVoiceAsync()
    {
        if (busy || transcribing || recording) { TurnStatus.Text = "Finish the current turn before testing speech"; return; }
        busy = true;
        VoiceButton.IsEnabled = SendButton.IsEnabled = false;
        try
        {
            if (await SpeakAsync("Hello User. My voice is ready.", true))
                TurnStatus.Text = "Voice test complete";
        }
        finally { busy = false; UpdateControlAvailability(); }
    }

    private async Task SetProactivityAsync(bool enabled)
    {
        try
        {
            using var response = await http.PostAsJsonAsync("/proactivity", new { enabled });
            response.EnsureSuccessStatusCode();
            TurnStatus.Text = enabled ? "Proactive suggestions on" : "Proactive suggestions paused";
        }
        catch (Exception error) { TurnStatus.Text = "Could not update proactive settings: " + error.Message; }
    }
}
