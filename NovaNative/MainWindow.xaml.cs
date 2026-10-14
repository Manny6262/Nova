using Microsoft.UI.Xaml;
using Microsoft.UI.Windowing;
using Windows.Graphics;

namespace NovaNative;

public sealed partial class MainWindow : Window
{
    public MainWindow()
    {
        InitializeComponent();

        ExtendsContentIntoTitleBar = true;
        SetTitleBar(AppTitleBar);
        var title = AppWindow.TitleBar;
        title.BackgroundColor = Windows.UI.Color.FromArgb(255, 9, 24, 47);
        title.ForegroundColor = Windows.UI.Color.FromArgb(255, 221, 234, 255);
        title.ButtonBackgroundColor = Windows.UI.Color.FromArgb(255, 9, 24, 47);
        title.ButtonForegroundColor = Windows.UI.Color.FromArgb(255, 221, 234, 255);
        title.ButtonHoverBackgroundColor = Windows.UI.Color.FromArgb(255, 38, 58, 93);
        var compactPreview = Environment.GetCommandLineArgs().Any(arg =>
            arg.Equals("--compact-preview", StringComparison.OrdinalIgnoreCase));
        var targetWidth = compactPreview ? 760 : 1536;
        var targetHeight = compactPreview ? 640 : 860;
        AppWindow.Resize(new SizeInt32(targetWidth, targetHeight));
        var area = DisplayArea.GetFromWindowId(AppWindow.Id, DisplayAreaFallback.Primary);
        if (area != null)
            AppWindow.Move(new PointInt32(area.WorkArea.X + Math.Max(0, (area.WorkArea.Width - targetWidth) / 2),
                                           area.WorkArea.Y + Math.Max(0, (area.WorkArea.Height - targetHeight) / 2)));
        AppWindow.SetIcon("Assets/AppIcon.ico");
        RootFrame.Navigate(typeof(MainPage));
    }
}
