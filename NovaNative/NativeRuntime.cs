using System.Diagnostics;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace NovaNative;

/// <summary>
/// Starts the private Python engine from the native, windowless executable.
/// The engine is intentionally allowed to remain available after the window
/// closes so Photon and approved background work can continue.
/// </summary>
internal sealed class NativeRuntime
{
    private const string EngineUrl = "http://127.0.0.1:5100";
    internal static string InstallRoot { get; } = ResolveInstallRoot();
    private readonly string root = InstallRoot;
    private readonly HttpClient client = new() { BaseAddress = new Uri(EngineUrl), Timeout = TimeSpan.FromSeconds(2) };
    private readonly SemaphoreSlim startupLock = new(1, 1);
    private Process? engineProcess;
    private Process? photonProcess;

    public string LastError { get; private set; } = "";

    [DllImport("kernel32.dll", EntryPoint = "GetFinalPathNameByHandleW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern uint GetFinalPathNameByHandle(SafeFileHandle file, StringBuilder path, uint length, uint flags);

    private static string ResolveInstallRoot()
    {
        var directory = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, ".."));
        try
        {
            using var file = File.OpenHandle(Path.Combine(directory, "backend_host.py"),
                FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            var resolved = new StringBuilder(32768);
            var length = GetFinalPathNameByHandle(file, resolved, (uint)resolved.Capacity, 0);
            if (length > 0 && length < resolved.Capacity)
                return Path.GetDirectoryName(resolved.ToString().Replace(@"\\?\", ""))!;
        }
        catch { }
        return directory;
    }

    public async Task<bool> EnsureStartedAsync()
    {
        await startupLock.WaitAsync();
        try
        {
            LastError = "";
            if (!await IsEngineReadyAsync())
            {
                var host = Path.Combine(root, "backend_host.py");
                var supervisor = Path.Combine(root, "background_service.py");
                var python = FindExecutable("python.exe", @"C:\Python314\python.exe");
                if (!File.Exists(host) || !File.Exists(Path.Combine(root, "engine", "app.py")))
                    throw new FileNotFoundException("Nova Native's private engine files are missing.");
                if (python is null)
                    throw new FileNotFoundException("Python is unavailable.");

                var start = new ProcessStartInfo(python)
                {
                    WorkingDirectory = root,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                };
                var useRecovery = File.Exists(supervisor) && RecoveryEnabled();
                start.ArgumentList.Add(useRecovery ? supervisor : host);
                if (!useRecovery)
                {
                    start.ArgumentList.Add("--port");
                    start.ArgumentList.Add("5100");
                }
                engineProcess = StartLogged(start, "engine");
                Log("Started engine process " + engineProcess.Id);

                for (var attempt = 0; attempt < 60; attempt++)
                {
                    await Task.Delay(500);
                    if (await IsEngineReadyAsync()) break;
                    if (engineProcess.HasExited && engineProcess.ExitCode != 0)
                        throw new InvalidOperationException("Nova's engine exited during startup. See native-runtime.log.");
                }
                if (!await IsEngineReadyAsync())
                    throw new TimeoutException("Nova's engine did not become ready within 30 seconds.");
            }

            // Also attach recovery to an engine that was already running.
            var recovery = Path.Combine(root, "background_service.py");
            var recoveryPython = FindExecutable("python.exe", @"C:\Python314\python.exe");
            if (File.Exists(recovery) && RecoveryEnabled() && recoveryPython is not null && (engineProcess is null || engineProcess.HasExited))
            {
                var start = new ProcessStartInfo(recoveryPython) { WorkingDirectory = root, UseShellExecute = false,
                    CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
                start.ArgumentList.Add(recovery);
                engineProcess = StartLogged(start, "recovery");
            }

            StartPhotonIfConfigured();
            return true;
        }
        catch (Exception error)
        {
            LastError = error.Message;
            Log("Startup failed: " + error);
            return false;
        }
        finally
        {
            startupLock.Release();
        }
    }

    private async Task<bool> IsEngineReadyAsync()
    {
        try
        {
            using var identity = await client.GetAsync("/native-host");
            if (!identity.IsSuccessStatusCode) return false;
            using var json = JsonDocument.Parse(await identity.Content.ReadAsStringAsync());
            var body = json.RootElement;
            if (!body.TryGetProperty("native", out var native) || native.ValueKind != JsonValueKind.True ||
                !body.TryGetProperty("engine_path", out var enginePath)) return false;
            var expected = Path.GetFullPath(Path.Combine(root, "engine")).TrimEnd(Path.DirectorySeparatorChar);
            var actual = Path.GetFullPath(enginePath.GetString() ?? "").TrimEnd(Path.DirectorySeparatorChar);
            if (!string.Equals(expected, actual, StringComparison.OrdinalIgnoreCase)) return false;
            using var health = await client.GetAsync("/health");
            return health.IsSuccessStatusCode;
        }
        catch (Exception error) when (error is HttpRequestException or TaskCanceledException or IOException or JsonException or ArgumentException)
        {
            return false;
        }
    }

    private bool RecoveryEnabled()
    {
        try
        {
            using var config = JsonDocument.Parse(File.ReadAllText(Path.Combine(root, "background-service.json")));
            return !config.RootElement.TryGetProperty("enabled", out var enabled) || enabled.ValueKind == JsonValueKind.True;
        }
        catch (FileNotFoundException) { return true; }
        catch { return false; }
    }

    private void StartPhotonIfConfigured()
    {
        var config = Path.Combine(root, "photon.json");
        var bridge = Path.Combine(root, "photon", "bridge.mjs");
        if (!File.Exists(config) || !File.Exists(bridge)) return;
        if (photonProcess is { HasExited: false }) return;
        var node = FindExecutable("node.exe", @"C:\Program Files\nodejs\node.exe");
        if (node is null)
        {
            Log("Photon bridge not started: Node.js is unavailable.");
            return;
        }
        try
        {
            var start = new ProcessStartInfo(node)
            {
                WorkingDirectory = Path.GetDirectoryName(bridge)!,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            start.ArgumentList.Add(bridge);
            start.Environment["NOVA_DATA_DIR"] = root;
            photonProcess = StartLogged(start, "photon");
            Log("Started Photon bridge process " + photonProcess.Id);
        }
        catch (Exception error)
        {
            Log("Photon bridge failed to start: " + error);
        }
    }

    private static string? FindExecutable(string name, string preferred)
    {
        var configured = Environment.GetEnvironmentVariable(name.StartsWith("python", StringComparison.OrdinalIgnoreCase) ? "NOVA_PYTHON" : "NOVA_NODE");
        if (!string.IsNullOrWhiteSpace(configured) && File.Exists(configured)) return configured;
        if (File.Exists(preferred)) return preferred;
        foreach (var directory in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator))
        {
            if (string.IsNullOrWhiteSpace(directory)) continue;
            var candidate = Path.Combine(directory.Trim('"'), name);
            if (File.Exists(candidate)) return candidate;
        }
        return null;
    }

    private Process StartLogged(ProcessStartInfo start, string label)
    {
        var process = Process.Start(start) ?? throw new InvalidOperationException("Could not launch " + label + ".");
        process.OutputDataReceived += (_, data) => { if (data.Data is not null) Log(label + ": " + data.Data); };
        process.ErrorDataReceived += (_, data) => { if (data.Data is not null) Log(label + ": " + data.Data); };
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();
        return process;
    }

    private void Log(string message)
    {
        try
        {
            lock (this)
                File.AppendAllText(Path.Combine(root, "native-runtime.log"),
                    DateTimeOffset.Now.ToString("o") + " " + message + Environment.NewLine);
        }
        catch { /* Logging must never prevent Nova from opening. */ }
    }
}
