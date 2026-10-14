using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System.Net.Http.Json;
using System.Text.Json;

namespace NovaNative;

public sealed partial class MainPage
{
    private async Task BuildResponsibilitiesAsync(int generation)
    {
        SectionContent.Children.Add(Text("ONGOING RESPONSIBILITIES", 11, "#8FDBF9"));
        SectionContent.Children.Add(Text("Give Nova a continuing goal and approve exactly what she may read. Reviews use a downloaded local model and continue while this PC is awake. She saves evidence and proposed next steps; she cannot edit files or send messages from a responsibility.", 13));
        var create = ActionButton("New responsibility");
        create.Click += async (_, _) => await CreateResponsibilityAsync();
        var refresh = ActionButton("Refresh progress");
        refresh.Click += async (_, _) => await RefreshSectionAsync("Tasks");
        var controls = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        controls.Children.Add(create); controls.Children.Add(refresh);
        SectionContent.Children.Add(controls);
        using var doc = JsonDocument.Parse(await http.GetStringAsync("/responsibilities"));
        if (generation != sectionGeneration) return;
        if (doc.RootElement.GetArrayLength() == 0)
            SectionContent.Children.Add(Card("Ready when you are", "Start with a small project and a few specific text files. Nothing starts until you review and approve the responsibility.", "#B5D6FF"));
        foreach (var item in doc.RootElement.EnumerateArray().Take(60))
        {
            var id = Get(item, "id");
            var state = Get(item, "state");
            var panel = RecordPanel(Get(item, "name") + " · " + state, Get(item, "goal"));
            panel.Children.Add(Text("Success criteria: " + Get(item, "success_criteria"), 12));
            panel.Children.Add(Text("Every " + Get(item, "interval_minutes") + " minutes · local " + Get(item, "model"), 12));
            if (item.TryGetProperty("latest_run", out var run) && run.ValueKind == JsonValueKind.Object)
                panel.Children.Add(Text(Get(run, "stage") + (Get(run, "error").Length > 0 ? "\n" + Get(run, "error") : ""), 12, "#B8DFEF"));
            var actions = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
            var inspect = ActionButton("Inspect scope & progress");
            inspect.Click += async (_, _) => await InspectResponsibilityAsync(id);
            actions.Children.Add(inspect);
            foreach (var action in state == "draft" ? new[] { "approve", "cancel" } :
                state == "active" ? new[] { "run", "pause", "cancel" } :
                state == "paused" ? new[] { "resume", "cancel" } : Array.Empty<string>())
            {
                var label = action switch { "approve" => "Review & approve", "run" => "Check now", "cancel" => "Cancel responsibility", "pause" => "Pause", _ => "Resume saved steps" };
                var button = ActionButton(label);
                button.Click += async (_, _) => await ResponsibilityActionAsync(id, action);
                actions.Children.Add(button);
            }
            panel.Children.Add(new ScrollViewer { Content = actions, HorizontalScrollBarVisibility = ScrollBarVisibility.Auto, VerticalScrollBarVisibility = ScrollBarVisibility.Disabled });
            SectionContent.Children.Add(RecordBorder(panel));
        }
    }

    private async Task CreateResponsibilityAsync()
    {
        try
        {
            using var projects = JsonDocument.Parse(await http.GetStringAsync("/projects"));
            var name = Field("Name", "", "For example: Garden project review", 120, false);
            var goal = Field("Ongoing goal", "", "What should Nova help keep moving?", 4000);
            var success = Field("Success criteria", "", "What evidence would show progress?", 2000);
            var files = Field("Exact local files — one absolute path per line", "", "C:\\Users\\…\\project-plan.txt", 20000);
            var interval = Field("Check every N minutes (5–10080)", "60", "60", 5, false);
            var project = new ComboBox { Header = "Project", HorizontalAlignment = HorizontalAlignment.Stretch };
            project.Items.Add(new ComboBoxItem { Content = "No project", Tag = 0 });
            foreach (var item in projects.RootElement.EnumerateArray().Where(item => Get(item, "state") == "active"))
                project.Items.Add(new ComboBoxItem { Content = Get(item, "name"), Tag = item.GetProperty("id").GetInt32() });
            project.SelectedIndex = 0;
            // This list excludes the OAuth entry. Background work never inherits it.
            var locals = ModelPicker.Items.OfType<ComboBoxItem>().Select(item => (string)item.Tag)
                .Where(tag => tag.StartsWith("local|", StringComparison.Ordinal)).Select(tag => tag[6..]).ToArray();
            if (locals.Length == 0) throw new InvalidOperationException("No downloaded local model is available. Refresh models in Settings first.");
            var model = Choice("Local model", locals, locals.Contains("qwen3.5:35b") ? "qwen3.5:35b" : locals[0]);
            var content = new StackPanel { Spacing = 12 };
            foreach (var field in new FrameworkElement[] { name, goal, success, files, interval, project, model }) content.Children.Add(field);
            content.Children.Add(Text("This creates a draft. Review and approve its scope before any file is read. Up to 20 files, 5 MB each; text excerpts are bounded. Changed captures produce an inbox update. Unchanged checks stay quiet.", 12));
            var dialog = EditorDialog("New ongoing responsibility", content);
            dialog.PrimaryButtonText = "Save draft";
            if (!await SaveEditorAsync(dialog, content, async () =>
            {
                if (!int.TryParse(interval.Text, out var minutes)) throw new InvalidOperationException("Enter a whole number of minutes.");
                var pid = (int)((ComboBoxItem)project.SelectedItem).Tag;
                await SendRecordAsync(HttpMethod.Post, "/responsibilities", new { name = name.Text, goal = goal.Text,
                    success_criteria = success.Text, files = files.Text.Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries).Select(line => line.Trim()).Where(line => line.Length > 0).ToArray(),
                    interval_minutes = minutes, project_id = pid == 0 ? (int?)null : pid, model = (string)model.SelectedItem });
            })) return;
            await RefreshSectionAsync("Tasks");
            TurnStatus.Text = "Draft saved — review its scope to start";
        }
        catch (Exception error) { TurnStatus.Text = "Responsibility: " + error.Message; }
    }

    private static string ResponsibilityScope(JsonElement item) =>
        Get(item, "goal") + "\n\nSuccess criteria: " + Get(item, "success_criteria") +
        "\n\n" + Get(item, "permissions") + "\n\nApproved files:\n" +
        string.Join("\n", item.GetProperty("files").EnumerateArray().Select(file => file.ToString())) +
        "\n\nEvery " + Get(item, "interval_minutes") + " minutes while this PC is awake. Local model: " + Get(item, "model");

    private async Task ResponsibilityActionAsync(string id, string action)
    {
        try
        {
            if (action == "approve")
            {
                using var doc = JsonDocument.Parse(await http.GetStringAsync("/responsibilities/" + id));
                var content = new StackPanel { Spacing = 12 };
                content.Children.Add(Text(ResponsibilityScope(doc.RootElement), 13));
                var dialog = EditorDialog("Approve ongoing read-only work?", content);
                dialog.PrimaryButtonText = "Approve this scope";
                dialog.DefaultButton = ContentDialogButton.Close;
                if (await dialog.ShowAsync() != ContentDialogResult.Primary) return;
            }
            await SendRecordAsync(HttpMethod.Post, $"/responsibilities/{id}/{action}", new { approved = action == "approve" });
            await RefreshSectionAsync("Tasks");
            await RefreshProposalBadgeAsync();
            TurnStatus.Text = action == "approve" ? "Approved responsibility started locally" : "Responsibility updated";
        }
        catch (Exception error) { TurnStatus.Text = error.Message; }
    }

    private async Task InspectResponsibilityAsync(string id)
    {
        try
        {
            using var doc = JsonDocument.Parse(await http.GetStringAsync("/responsibilities/" + id));
            var item = doc.RootElement;
            var content = new StackPanel { Spacing = 12 };
            content.Children.Add(Text(ResponsibilityScope(item), 13));
            content.Children.Add(Text("Saved steps confirm file captures and that a model response finished. Recommendations and overall goal completion still need review.", 12));
            foreach (var run in item.GetProperty("runs").EnumerateArray().Take(10))
            {
                content.Children.Add(Text("Run: " + Get(run, "status") + " · " + Get(run, "stage"), 14, "#B8DFEF"));
                foreach (var step in run.GetProperty("steps").EnumerateArray())
                {
                    content.Children.Add(Text(Get(step, "name") + " · " + Get(step, "status"), 12));
                    if (Get(step, "ordinal") == "1" && Get(step, "receipt").Length > 0)
                    {
                        using var receipt = JsonDocument.Parse(Get(step, "receipt"));
                        foreach (var file in receipt.RootElement.EnumerateArray())
                            content.Children.Add(Text(Get(file, "path") + " · " + Get(file, "status") +
                                (Get(file, "bytes").Length > 0 ? " · " + Get(file, "bytes") + " bytes\nFingerprint: " + Get(file, "sha256") : ""), 11, "#9AB4DB"));
                    }
                }
                if (Get(run, "result").Length > 0) content.Children.Add(Text(Get(run, "result"), 13));
                if (Get(run, "error").Length > 0) content.Children.Add(Text(Get(run, "error"), 12, "#F0A1A7"));
            }
            var dialog = EditorDialog(Get(item, "name"), content);
            dialog.PrimaryButtonText = ""; dialog.CloseButtonText = "Close";
            await dialog.ShowAsync();
        }
        catch (Exception error) { TurnStatus.Text = error.Message; }
    }
}
