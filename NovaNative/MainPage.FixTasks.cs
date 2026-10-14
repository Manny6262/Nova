using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System.Net.Http.Json;
using System.Text.Json;

namespace NovaNative;

public sealed partial class MainPage
{
    private async Task EnsureTaskHostAsync()
    {
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(3));
        using var response = await http.GetAsync("/native-host", timeout.Token);
        response.EnsureSuccessStatusCode();
        using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync(timeout.Token));
        var item = doc.RootElement;
        var expected = Path.GetFullPath(Path.Combine(NativeRuntime.InstallRoot, "engine")).TrimEnd(Path.DirectorySeparatorChar);
        var actual = Path.GetFullPath(Get(item, "engine_path")).TrimEnd(Path.DirectorySeparatorChar);
        if (!item.TryGetProperty("native", out var native) || native.ValueKind != JsonValueKind.True || !string.Equals(actual, expected, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("This window is not connected to its own Nova Native engine. Retry the engine connection before managing tasks.");
    }

    private async Task BuildFixTasksAsync(int generation)
    {
        SectionContent.Children.Add(Text("TASKS & CANDIDATE FIXES", 11, "#8FDBF9"));
        SectionContent.Children.Add(Text("Approve a small source task. Nova prepares changes in a separate working copy, saves checks and progress, then brings you the exact revision to review. Your originals stay untouched until you approve applying it.", 13));
        var controls = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        var create = ActionButton("New task");
        create.Click += async (_, _) => await CreateFixTaskAsync();
        var refresh = ActionButton("Refresh tasks");
        refresh.Click += async (_, _) => await RefreshSectionAsync("Tasks");
        controls.Children.Add(create); controls.Children.Add(refresh);
        SectionContent.Children.Add(controls);
        using var doc = JsonDocument.Parse(await http.GetStringAsync("/fix-tasks"));
        if (generation != sectionGeneration) return;
        foreach (var task in doc.RootElement.EnumerateArray().Take(40))
        {
            var id = Get(task, "id"); var state = Get(task, "state");
            var goal = Get(task, "goal");
            var panel = RecordPanel(Get(task, "name") + " · " + state.Replace('_', ' '), goal.Length > 240 ? goal[..240] + "… (full goal in scope)" : goal);
            panel.Children.Add(Text(Get(task, "stage") + " · Revision " + Get(task, "revision"), 12, "#B8DFEF"));
            if (Get(task, "error").Length > 0) panel.Children.Add(Text(Get(task, "error"), 12, "#F0A1A7"));
            var actions = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
            var inspect = ActionButton(state == "ready" ? "Review candidate fix" : "Inspect scope & progress");
            inspect.Click += async (_, _) => await InspectFixTaskAsync(id);
            actions.Children.Add(inspect);
            var names = state switch
            {
                "draft" => new[] { "approve", "cancel" },
                "queued" or "running" => new[] { "pause", "cancel" },
                "paused" or "interrupted" or "failed" => new[] { "resume", "cancel" },
                "ready" or "needs_changes" => new[] { "revise", "reject" },
                _ => Array.Empty<string>()
            };
            foreach (var action in names)
            {
                var button = ActionButton(action switch { "approve" => "Review & approve scope", "resume" => "Resume saved work", "revise" => "Request changes", "reject" => "Reject candidate", "cancel" => "Cancel task", _ => "Pause" });
                button.Click += async (_, _) => await FixTaskActionAsync(id, action);
                actions.Children.Add(button);
            }
            panel.Children.Add(new ScrollViewer { Content = actions, HorizontalScrollBarVisibility = ScrollBarVisibility.Auto, VerticalScrollBarVisibility = ScrollBarVisibility.Disabled });
            SectionContent.Children.Add(RecordBorder(panel));
        }
    }

    private async Task ChooseTaskFilesAsync(TextBox root, TextBox files)
    {
        var picker = new Windows.Storage.Pickers.FileOpenPicker();
        picker.FileTypeFilter.Add("*");
        WinRT.Interop.InitializeWithWindow.Initialize(picker, WinRT.Interop.WindowNative.GetWindowHandle(((App)Application.Current).MainWindow!));
        var selected = await picker.PickMultipleFilesAsync();
        if (selected.Count == 0) return;
        if (string.IsNullOrWhiteSpace(root.Text)) root.Text = System.IO.Path.GetDirectoryName(selected[0].Path)!;
        var relative = selected.Select(file => System.IO.Path.GetRelativePath(root.Text, file.Path).Replace('\\', '/'));
        files.Text = string.Join("\n", files.Text.Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries).Concat(relative).Distinct(StringComparer.OrdinalIgnoreCase));
    }

    private async Task CreateFixTaskAsync()
    {
        try
        {
            await EnsureTaskHostAsync();
            using var projects = JsonDocument.Parse(await http.GetStringAsync("/projects"));
            var name = Field("Task name", "", "A small, specific improvement", 120, false);
            var goal = Field("What should Nova prepare?", "", "Describe the change and behavior to preserve", 4000);
            var criteria = Field("How will you judge success?", "", "What should work when this is finished?", 2000);
            var root = Field("Project source folder", "", "C:\\Projects\\MyProject", 1000, false);
            var files = Field("Editable files — relative paths, one per line", "", "example.py", 6000);
            var references = Field("Read-only references — relative paths, one per line", "", "test_example.py", 6000);
            var test = Field("Optional trusted Python test — one of the reference files", "", "test_example.py", 250, false);
            var testMode = Choice("Check permissions", new[] { "Guarded Python checks" }, "Guarded Python checks");
            var project = new ComboBox { Header = "Project memory", HorizontalAlignment = HorizontalAlignment.Stretch };
            project.Items.Add(new ComboBoxItem { Content = "No project", Tag = 0 });
            foreach (var item in projects.RootElement.EnumerateArray().Where(item => Get(item, "state") == "active"))
                project.Items.Add(new ComboBoxItem { Content = Get(item, "name"), Tag = item.GetProperty("id").GetInt32() });
            project.SelectedIndex = 0;
            var locals = ModelPicker.Items.OfType<ComboBoxItem>().Select(item => (string)item.Tag)
                .Where(tag => tag.StartsWith("local|", StringComparison.Ordinal)).Select(tag => tag[6..]).ToArray();
            if (locals.Length == 0) throw new InvalidOperationException("Refresh your downloaded local models in Settings first.");
            var model = Choice("Local model", locals, locals.Contains("qwen3.5:35b") ? "qwen3.5:35b" : locals[0]);
            var choose = ActionButton("Choose editable files");
            choose.Click += async (_, _) => { try { await ChooseTaskFilesAsync(root, files); } catch (Exception error) { TurnStatus.Text = error.Message; } };
            var content = new StackPanel { Spacing = 12 };
            foreach (var element in new FrameworkElement[] { name, goal, criteria, root, files, choose, references, test, testMode, project, model }) content.Children.Add(element);
            content.Children.Add(Text("Drafts do not start work. Approve scope first. Up to 20 editable and 20 reference files, 100 KB each, 45,000 characters combined. Read-only ZIP test dependencies may be up to 20 MB. New files are allowed. Optional tests run as your Windows user, outside an OS sandbox: approve only trusted scripts. Applying source changes is separate from deployment.", 12));
            var dialog = EditorDialog("New local task", content);
            dialog.PrimaryButtonText = "Save draft";
            if (!await SaveEditorAsync(dialog, content, async () =>
            {
                string[] Paths(TextBox box) => box.Text.Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries).Select(line => line.Trim()).Where(line => line.Length > 0).ToArray();
                var pid = (int)((ComboBoxItem)project.SelectedItem).Tag;
                await SendRecordAsync(HttpMethod.Post, "/fix-tasks", new { name = name.Text, goal = goal.Text, criteria = criteria.Text,
                    root = root.Text, files = Paths(files), references = Paths(references), test_file = test.Text.Trim(), test_mode = (string)testMode.SelectedItem == "Guarded Python checks" ? "python_guarded" : "native_build", project_id = pid == 0 ? (int?)null : pid, model = (string)model.SelectedItem });
            })) return;
            await RefreshSectionAsync("Tasks");
            TurnStatus.Text = "Task draft saved — approve its scope to start";
        }
        catch (Exception error) { TurnStatus.Text = error.Message; }
    }

    private static string FixScope(JsonElement task)
    {
        var scope = task.GetProperty("scope");
        return Get(task, "goal") + "\n\nSuccess criteria: " + Get(task, "criteria") + "\n\n" + Get(task, "permissions") +
            "\n\nProject: " + Get(scope, "root") + "\nEditable files:\n" + string.Join("\n", scope.GetProperty("files").EnumerateArray().Select(file => file.ToString())) +
            "\n\nRead-only references:\n" + string.Join("\n", scope.GetProperty("references").EnumerateArray().Select(file => file.ToString())) +
            "\n\nApproved text checks:\n" + scope.GetProperty("rules").ToString() +
            "\n\nLocal model: " + Get(task, "model");
    }

    private async Task FixTaskActionAsync(string id, string action)
    {
        try
        {
            await EnsureTaskHostAsync();
            object body = new { };
            if (action is "approve" or "resume" or "revise")
            {
                using var doc = JsonDocument.Parse(await http.GetStringAsync("/fix-tasks/" + id));
                var content = new StackPanel { Spacing = 12 };
                content.Children.Add(Text(FixScope(doc.RootElement), 13));
                TextBox? feedback = null;
                if (action == "revise")
                {
                    feedback = Field("What should Nova change?", "", "Keep the same file scope", 2000);
                    content.Children.Add(feedback);
                }
                if (action == "resume") content.Children.Add(Text("Saved captures and candidate edits will be reused. If a test was interrupted, this also authorizes retrying the previously approved test. Review its possible side effects first.", 13));
                var dialog = EditorDialog(action == "approve" ? "Approve preparation scope?" : action == "resume" ? "Resume this task?" : "Request a new revision", content);
                dialog.PrimaryButtonText = action == "approve" ? "Approve preparation" : action == "resume" ? "Resume & allow test retry" : "Request changes";
                dialog.DefaultButton = ContentDialogButton.Close;
                if (await dialog.ShowAsync() != ContentDialogResult.Primary) return;
                body = action == "approve" ? new { approved = true } : action == "resume" ? new { retry_checks = true } : new { feedback = feedback!.Text };
            }
            await SendRecordAsync(HttpMethod.Post, $"/fix-tasks/{id}/{action}", body);
            await RefreshSectionAsync("Tasks");
            TurnStatus.Text = "Task updated";
        }
        catch (Exception error) { TurnStatus.Text = error.Message; }
    }

    private async Task InspectFixTaskAsync(string id)
    {
        try
        {
            await EnsureTaskHostAsync();
            using var doc = JsonDocument.Parse(await http.GetStringAsync("/fix-tasks/" + id));
            var task = doc.RootElement; var state = Get(task, "state");
            var content = new StackPanel { Spacing = 12 };
            content.Children.Add(Text(FixScope(task), 13));
            content.Children.Add(Text(Get(task, "stage") + " · revision " + Get(task, "revision"), 14, "#B8DFEF"));
            if (Get(task, "summary").Length > 0) content.Children.Add(Text(Get(task, "summary"), 13));
            foreach (var step in task.GetProperty("plan").EnumerateArray()) content.Children.Add(Text("• " + step.ToString(), 12));
            foreach (var capture in task.GetProperty("captures").EnumerateArray()) content.Children.Add(Text("Saved capture: " + Get(capture, "file") + "\nFingerprint: " + Get(capture, "sha256"), 11, "#9AB4DB"));
            content.Children.Add(Text("Check receipts report what actually ran. Passing syntax or text checks does not prove the goal is achieved. Review behavior and risks before applying. Keep this project idle while applying source changes.", 12));
            foreach (var change in task.GetProperty("changes").EnumerateArray())
            {
                content.Children.Add(Text(Get(change, "file"), 14, "#8FDBF9"));
                content.Children.Add(new TextBox { Text = Get(change, "diff"), IsReadOnly = true, AcceptsReturn = true,
                    TextWrapping = TextWrapping.NoWrap, MaxHeight = 300, FontFamily = new Microsoft.UI.Xaml.Media.FontFamily("Consolas") });
            }
            foreach (var check in task.GetProperty("checks").EnumerateArray())
                content.Children.Add(Text(Get(check, "name") + " · " + Get(check, "status") + "\n" + Get(check, "output"), 12));
            foreach (var item in task.GetProperty("events").EnumerateArray().TakeLast(15))
                content.Children.Add(Text(Get(item, "kind") + ": " + Get(item, "detail"), 12));
            if (Get(task, "error").Length > 0) content.Children.Add(Text(Get(task, "error"), 13, "#F0A1A7"));
            var dialog = EditorDialog("Review " + Get(task, "name"), content);
            dialog.PrimaryButtonText = state is "ready" or "apply_interrupted" ? "Approve source changes" : "";
            dialog.CloseButtonText = "Close"; dialog.DefaultButton = ContentDialogButton.Close;
            if (await dialog.ShowAsync() != ContentDialogResult.Primary) return;
            await EnsureTaskHostAsync();
            await SendRecordAsync(HttpMethod.Post, "/fix-tasks/" + id + "/apply", new { approved = true, review_token = Get(task, "review_token") });
            await RefreshSectionAsync("Tasks");
            TurnStatus.Text = "Approved source changes applied — deployment is separate";
        }
        catch (Exception error) { TurnStatus.Text = error.Message; }
    }
}
