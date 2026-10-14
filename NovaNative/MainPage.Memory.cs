using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System.Net.Http.Json;
using System.Text.Json;

namespace NovaNative;

public sealed partial class MainPage
{
    private string libraryTab = "Memory";
    private bool showRemovedRecords;
    private int? conversationProjectId;
    private string conversationProjectName = "";
    private string conversationProjectNextStep = "";

    private async Task BuildLibraryAsync(int generation)
    {
        SectionHeader("Library", "Your memory, projects, and conversations — saved on this PC.");
        var tabs = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        foreach (var name in new[] { "Memory", "Projects", "Conversations" })
        {
            var button = ActionButton(name);
            if (name == libraryTab) button.BorderBrush = Brush("#C3AAFF");
            button.Click += async (_, _) => { libraryTab = name; await RefreshSectionAsync("Library"); };
            tabs.Children.Add(button);
        }
        SectionContent.Children.Add(tabs);
        if (libraryTab == "Conversations") { await BuildConversationLibraryAsync(generation); return; }
        var removed = new CheckBox { Content = "Show removed records", IsChecked = showRemovedRecords };
        removed.Click += async (_, _) => { showRemovedRecords = removed.IsChecked == true; await RefreshSectionAsync("Library"); };
        SectionContent.Children.Add(removed);
        if (libraryTab == "Projects") await BuildProjectLibraryAsync(generation);
        else await BuildMemoryLibraryAsync(generation);
    }

    private async Task SendRecordAsync(HttpMethod method, string route, object? body = null)
    {
        await EnsureTaskHostAsync();
        using var request = new HttpRequestMessage(method, route);
        if (body != null) request.Content = JsonContent.Create(body);
        using var response = await http.SendAsync(request);
        if (!response.IsSuccessStatusCode)
        {
            var detail = await response.Content.ReadAsStringAsync();
            try { using var error = JsonDocument.Parse(detail); detail = Get(error.RootElement, "error", detail); }
            catch (JsonException) { }
            throw new InvalidOperationException(detail);
        }
    }

    private async Task ChangeRecordAsync(string route, HttpMethod method)
    {
        try
        {
            await SendRecordAsync(method, route);
            await RefreshSectionAsync("Library");
            if (conversationProjectId != null && sessionId != null) await LoadConversationProjectAsync(sessionId.Value);
            RenderCanvas();
            TurnStatus.Text = "Saved locally";
        }
        catch (Exception error) { TurnStatus.Text = error.Message; }
    }

    private async Task BuildMemoryLibraryAsync(int generation)
    {
        SectionContent.Children.Add(Text("Save preferences, instructions, or facts explicitly. Removing a record stops saved recall; original conversation text stays in Recents.", 13));
        var add = ActionButton("Add memory");
        add.Click += async (_, _) => await EditMemoryAsync(null);
        SectionContent.Children.Add(add);
        var search = new TextBox { PlaceholderText = "Search memory…" };
        var records = new StackPanel { Spacing = 10 };
        SectionContent.Children.Add(search); SectionContent.Children.Add(records);
        using var doc = JsonDocument.Parse(await http.GetStringAsync("/facts" + (showRemovedRecords ? "?include_removed=1" : "")));
        if (generation != sectionGeneration) return;
        var entries = doc.RootElement.EnumerateArray().Select(item => item.Clone()).ToArray();
        void Render()
        {
            records.Children.Clear();
            foreach (var item in entries.Where(item => (Get(item, "text") + Get(item, "project_name"))
                .Contains(search.Text, StringComparison.OrdinalIgnoreCase)).Take(100))
            {
                var id = item.GetProperty("id").GetInt32();
                var removed = Get(item, "deleted_at").Length > 0;
                var projectRemoved = Get(item, "project_deleted_at").Length > 0;
                var scope = Get(item, "project_name", "Personal");
                var heading = Get(item, "kind", "fact") + " · " + scope + (removed ? " · removed" : projectRemoved ? " · project removed" : "");
                var panel = RecordPanel(heading, Get(item, "text"));
                var source = Get(item, "source") == "chat" ? "Explicit request in conversation" : "Added in Library";
                var provenance = source + " · " + RecordDate(item);
                if (int.TryParse(Get(item, "source_session_id"), out var sourceSession)) provenance += " · chat " + sourceSession;
                panel.Children.Add(Text(provenance, 11, "#9AB4DB"));
                if (Get(item, "pinned") == "1") panel.Children.Add(Text("Prioritized when this scope is active", 11, "#BBA9F0"));
                var actions = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
                if (removed)
                {
                    var restore = ActionButton("Restore memory");
                    restore.Click += async (_, _) => await ChangeRecordAsync($"/facts/{id}/restore", HttpMethod.Post);
                    actions.Children.Add(restore);
                }
                else if (!projectRemoved)
                {
                    var edit = ActionButton("Edit memory");
                    edit.Click += async (_, _) => await EditMemoryAsync(item);
                    var remove = ActionButton("Remove memory");
                    remove.Click += async (_, _) => await ChangeRecordAsync($"/facts/{id}", HttpMethod.Delete);
                    actions.Children.Add(edit); actions.Children.Add(remove);
                }
                else panel.Children.Add(Text("Restore its project to use this memory again.", 12));
                panel.Children.Add(actions); records.Children.Add(RecordBorder(panel));
            }
            if (records.Children.Count == 0) records.Children.Add(Card("No memories here", "Add something you want Nova to remember, or say ‘remember that…’ in a conversation.", "#B4E5D7"));
        }
        search.TextChanged += (_, _) => Render(); Render();
    }

    private async Task EditMemoryAsync(JsonElement? record)
    {
        try
        {
            using var projects = JsonDocument.Parse(await http.GetStringAsync("/projects"));
            var text = Field("Memory", record is { } saved ? Get(saved, "text") : "", "Something Nova should remember…", 4000);
            var kind = Choice("Type", new[] { "preference", "instruction", "fact" }, record is { } r ? Get(r, "kind", "preference") : "preference");
            var scope = new ComboBox { Header = "Scope", HorizontalAlignment = HorizontalAlignment.Stretch };
            scope.Items.Add(new ComboBoxItem { Content = "Personal — all conversations", Tag = 0 });
            foreach (var project in projects.RootElement.EnumerateArray())
                scope.Items.Add(new ComboBoxItem { Content = Get(project, "name"), Tag = project.GetProperty("id").GetInt32() });
            var selectedId = record is { } rec && int.TryParse(Get(rec, "project_id"), out var pid) ? pid : 0;
            scope.SelectedItem = scope.Items.OfType<ComboBoxItem>().FirstOrDefault(item => (int)item.Tag == selectedId) ?? scope.Items[0];
            var pinned = new CheckBox { Content = "Prioritize in this scope", IsChecked = record is not { } fact || Get(fact, "pinned") == "1" };
            var content = new StackPanel { Spacing = 12 };
            content.Children.Add(text); content.Children.Add(kind); content.Children.Add(scope); content.Children.Add(pinned);
            content.Children.Add(Text("Saved instructions guide Nova's replies. They do not grant permission to change files, use accounts, or send messages.", 12));
            var dialog = EditorDialog(record == null ? "Add memory" : "Edit memory", content);
            if (!await SaveEditorAsync(dialog, content, async () =>
            {
                var projectId = (int)((ComboBoxItem)scope.SelectedItem).Tag;
                var body = new { text = text.Text, kind = (string)kind.SelectedItem, project_id = projectId == 0 ? (int?)null : projectId, pinned = pinned.IsChecked == true };
                await SendRecordAsync(record == null ? HttpMethod.Post : HttpMethod.Patch,
                    record is { } previous ? "/facts/" + Get(previous, "id") : "/facts", body);
            })) return;
            await RefreshSectionAsync("Library"); TurnStatus.Text = "Memory saved locally";
        }
        catch (Exception error) { TurnStatus.Text = "Memory: " + error.Message; }
    }

    private async Task BuildProjectLibraryAsync(int generation)
    {
        SectionContent.Children.Add(Text("Keep a goal, decisions, files, and next step together. Start a project chat to give Nova that context.", 13));
        var add = ActionButton("Add project");
        add.Click += async (_, _) => await EditProjectAsync(null);
        SectionContent.Children.Add(add);
        var search = new TextBox { PlaceholderText = "Search projects…" };
        var records = new StackPanel { Spacing = 10 };
        SectionContent.Children.Add(search); SectionContent.Children.Add(records);
        using var doc = JsonDocument.Parse(await http.GetStringAsync("/projects" + (showRemovedRecords ? "?include_removed=1" : "")));
        if (generation != sectionGeneration) return;
        var entries = doc.RootElement.EnumerateArray().Select(item => item.Clone()).ToArray();
        void Render()
        {
            records.Children.Clear();
            foreach (var item in entries.Where(item => (Get(item, "name") + Get(item, "goal"))
                .Contains(search.Text, StringComparison.OrdinalIgnoreCase)).Take(100))
            {
                var id = item.GetProperty("id").GetInt32();
                var removed = Get(item, "deleted_at").Length > 0;
                var panel = RecordPanel(Get(item, "name") + " · " + (removed ? "removed" : Get(item, "state")), Get(item, "goal"));
                foreach (var (key, label) in new[] { ("description", "Context"), ("decisions", "Decisions"), ("next_step", "Next step"), ("path", "Folder") })
                    if (Get(item, key).Length > 0) panel.Children.Add(Text(label + ": " + Get(item, key), 12));
                if (item.TryGetProperty("files", out var files) && files.GetArrayLength() > 0)
                    panel.Children.Add(Text("Files and references: " + string.Join("\n", files.EnumerateArray().Select(file => file.ToString())), 12));
                panel.Children.Add(Text("Updated " + RecordDate(item), 11, "#9AB4DB"));
                var actions = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
                if (removed)
                {
                    var restore = ActionButton("Restore project");
                    restore.Click += async (_, _) => await ChangeRecordAsync($"/projects/{id}/restore", HttpMethod.Post);
                    actions.Children.Add(restore);
                }
                else
                {
                    var chat = ActionButton("Start project chat");
                    chat.Click += async (_, _) => await StartProjectConversationAsync(id);
                    var edit = ActionButton("Edit project");
                    edit.Click += async (_, _) => await EditProjectAsync(item);
                    var remove = ActionButton("Remove project");
                    remove.Click += async (_, _) => await ChangeRecordAsync($"/projects/{id}", HttpMethod.Delete);
                    actions.Children.Add(chat); actions.Children.Add(edit); actions.Children.Add(remove);
                }
                panel.Children.Insert(1, actions); records.Children.Add(RecordBorder(panel));
            }
            if (records.Children.Count == 0) records.Children.Add(Card("No projects here", "Add a goal you want Nova to help you carry forward.", "#B4E5D7"));
        }
        search.TextChanged += (_, _) => Render(); Render();
    }

    private async Task EditProjectAsync(JsonElement? record)
    {
        try
        {
            string Value(string key) => record is { } item ? Get(item, key) : "";
            var name = Field("Project name", Value("name"), "Name your project", 120, false);
            var goal = Field("Goal", Value("goal"), "What are you trying to achieve?", 4000);
            var description = Field("Context", Value("description"), "Useful background", 4000);
            var decisions = Field("Decisions", Value("decisions"), "Choices you have made", 6000);
            var next = Field("Next step", Value("next_step"), "Where should the next conversation pick up?", 2000);
            var path = Field("Project folder (optional)", Value("path"), "Existing folder path", 1000, false);
            var fileText = record is { } r && r.TryGetProperty("files", out var files) ? string.Join("\n", files.EnumerateArray().Select(file => file.ToString())) : "";
            var references = Field("Files and references (one per line)", fileText, "Paths or links; adding a reference does not read it", 30000);
            var state = Choice("Status", new[] { "active", "paused", "completed" }, record is { } saved ? Get(saved, "state", "active") : "active");
            var content = new StackPanel { Spacing = 12 };
            foreach (var control in new FrameworkElement[] { name, goal, description, decisions, next, path, references, state }) content.Children.Add(control);
            content.Children.Add(Text("Project context is saved locally. File access and actions still use Nova's existing permissions and approvals.", 12));
            var dialog = EditorDialog(record == null ? "Add project" : "Edit project", content);
            if (!await SaveEditorAsync(dialog, content, async () =>
            {
                var body = new { name = name.Text, goal = goal.Text, description = description.Text, decisions = decisions.Text,
                    next_step = next.Text, path = path.Text, files = references.Text.Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries).Select(line => line.Trim()).Where(line => line.Length > 0).ToArray(), state = (string)state.SelectedItem };
                await SendRecordAsync(record == null ? HttpMethod.Post : HttpMethod.Patch,
                    record is { } previous ? "/projects/" + Get(previous, "id") : "/projects", body);
            })) return;
            await RefreshSectionAsync("Library");
            if (sessionId != null) await LoadConversationProjectAsync(sessionId.Value);
            RenderCanvas(); TurnStatus.Text = "Project saved locally";
        }
        catch (Exception error) { TurnStatus.Text = "Project: " + error.Message; }
    }

    private ContentDialog EditorDialog(string title, StackPanel content) => new()
    {
        Title = title, XamlRoot = XamlRoot, PrimaryButtonText = "Save", CloseButtonText = "Cancel",
        RequestedTheme = ElementTheme.Dark, Background = Brush("#142440"), Foreground = Brush("#EEF2FC"),
        DefaultButton = ContentDialogButton.Primary,
        Content = new ScrollViewer { Content = content, MaxHeight = 480, VerticalScrollBarVisibility = ScrollBarVisibility.Auto }
    };

    private async Task<bool> SaveEditorAsync(ContentDialog dialog, StackPanel content, Func<Task> save)
    {
        var errorText = Text("", 12, "#F0A1A7");
        // Put validation above the fields so a long project form never hides it.
        content.Children.Insert(0, errorText);
        dialog.PrimaryButtonClick += async (_, args) =>
        {
            var deferral = args.GetDeferral();
            dialog.IsPrimaryButtonEnabled = false; content.IsHitTestVisible = false; errorText.Text = "";
            try { await save(); }
            catch (Exception error) { args.Cancel = true; errorText.Text = error.Message; }
            finally { dialog.IsPrimaryButtonEnabled = true; content.IsHitTestVisible = true; deferral.Complete(); }
        };
        return await dialog.ShowAsync() == ContentDialogResult.Primary;
    }

    private static TextBox Field(string label, string value, string placeholder, int limit, bool multiline = true) => new()
    {
        Header = label, Text = value, PlaceholderText = placeholder, MaxLength = limit,
        AcceptsReturn = multiline, TextWrapping = multiline ? TextWrapping.Wrap : TextWrapping.NoWrap,
        MinHeight = multiline ? 75 : 0, MaxHeight = multiline ? 150 : 60
    };

    private static ComboBox Choice(string header, string[] options, string selected)
    {
        var box = new ComboBox { Header = header, HorizontalAlignment = HorizontalAlignment.Stretch };
        foreach (var option in options) box.Items.Add(option);
        box.SelectedItem = options.Contains(selected) ? selected : options[0];
        return box;
    }

    private static StackPanel RecordPanel(string title, string detail)
    {
        var panel = new StackPanel { Spacing = 8 };
        panel.Children.Add(Text(title, 15, "#B8DFEF"));
        if (detail.Length > 0) panel.Children.Add(Text(detail, 13));
        return panel;
    }

    private static Border RecordBorder(StackPanel panel) => new()
    {
        Child = panel, Background = Brush("#B81A2D4E"), BorderBrush = Brush("#5275A4"),
        BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(10), Padding = new Thickness(15)
    };

    private static string RecordDate(JsonElement item) => double.TryParse(Get(item, "updated_at"),
        System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var timestamp)
        ? DateTimeOffset.FromUnixTimeSeconds((long)timestamp).ToLocalTime().ToString("MMM d, yyyy · h:mm tt") : "Date unavailable";

    private async Task LoadConversationProjectAsync(int id)
    {
        using var doc = JsonDocument.Parse(await http.GetStringAsync($"/sessions/{id}/project"));
        ClearConversationProject();
        if (doc.RootElement.TryGetProperty("project", out var project) && project.ValueKind == JsonValueKind.Object)
        {
            conversationProjectId = project.GetProperty("id").GetInt32();
            conversationProjectName = Get(project, "name");
            conversationProjectNextStep = Get(project, "next_step");
        }
    }

    private void ClearConversationProject()
    {
        conversationProjectId = null; conversationProjectName = conversationProjectNextStep = "";
    }

    private async Task StartProjectConversationAsync(int projectId)
    {
        if (busy || transcribing) { TurnStatus.Text = "Finish the current reply first"; return; }
        try
        {
            EndVoiceMode();
            using var response = await http.PostAsJsonAsync("/sessions", new { });
            response.EnsureSuccessStatusCode();
            using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
            var id = doc.RootElement.GetProperty("id").GetInt32();
            await SendRecordAsync(HttpMethod.Post, $"/sessions/{id}/project", new { project_id = projectId });
            await LoadSessionAsync(id); await LoadSessionsAsync();
            AddMessage("Nova", "This conversation is linked to " + conversationProjectName + ". What would you like to work on?", false);
            ShowSection("Chat"); UpdateNavStyle("Chat"); WelcomeTitle.Text = welcomeGreeting;
            Composer.Focus(FocusState.Programmatic);
        }
        catch (Exception error) { TurnStatus.Text = "Could not start project chat: " + error.Message; }
    }

    private async Task ChooseConversationProjectAsync()
    {
        if (busy || transcribing) { TurnStatus.Text = "Finish the current reply first"; return; }
        try
        {
            using var doc = JsonDocument.Parse(await http.GetStringAsync("/projects"));
            var choices = new ComboBox { Header = "Project for this conversation", HorizontalAlignment = HorizontalAlignment.Stretch };
            choices.Items.Add(new ComboBoxItem { Content = "Personal conversation", Tag = 0 });
            foreach (var project in doc.RootElement.EnumerateArray())
                choices.Items.Add(new ComboBoxItem { Content = Get(project, "name"), Tag = project.GetProperty("id").GetInt32() });
            choices.SelectedItem = choices.Items.OfType<ComboBoxItem>().FirstOrDefault(item => (int)item.Tag == conversationProjectId) ?? choices.Items[0];
            var content = new StackPanel { Spacing = 12 };
            content.Children.Add(choices); content.Children.Add(Text("Nova uses this project's saved context in future replies. This does not grant new tool permissions.", 12));
            if (await EditorDialog("Conversation context", content).ShowAsync() != ContentDialogResult.Primary) return;
            if (sessionId == null)
            {
                using var response = await http.PostAsJsonAsync("/sessions", new { });
                response.EnsureSuccessStatusCode();
                using var session = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
                sessionId = session.RootElement.GetProperty("id").GetInt32();
            }
            var pid = (int)((ComboBoxItem)choices.SelectedItem).Tag;
            await SendRecordAsync(HttpMethod.Post, $"/sessions/{sessionId}/project", new { project_id = pid == 0 ? (int?)null : pid });
            await LoadConversationProjectAsync(sessionId.Value); RenderCanvas();
            TurnStatus.Text = pid == 0 ? "Personal conversation" : "Project context linked";
        }
        catch (Exception error) { TurnStatus.Text = "Project: " + error.Message; }
    }
}
