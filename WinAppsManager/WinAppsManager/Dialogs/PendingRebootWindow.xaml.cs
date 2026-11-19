using System.Windows;
using WinAppsManager.Services;

namespace WinAppsManager.Dialogs;

/// <summary>One leftover that could not be removed now, shown in the delayed-deletion prompt.</summary>
public sealed class PendingRebootItem
{
    public PendingRebootItem(string path, bool isScheduled, string message)
    {
        Path = path;
        IsScheduled = isScheduled;
        Message = message;
    }

    public string Path { get; }
    public bool IsScheduled { get; }
    public string StatusText => IsScheduled ? "重启后删除" : "无法删除";
    public string Message { get; }
}

/// <summary>
/// Lists the leftovers that need a restart, and offers the two actions the user needs:
/// restart now, or finish and reboot later.
/// </summary>
public partial class PendingRebootWindow : Window
{
    private readonly int _scheduledCount;
    private readonly int _failedCount;

    public IReadOnlyList<PendingRebootItem> Items { get; }

    public PendingRebootWindow(IReadOnlyList<PendingRebootItem> items)
    {
        ArgumentNullException.ThrowIfNull(items);
        Items = items;
        _scheduledCount = items.Count(item => item.IsScheduled);
        _failedCount = items.Count - _scheduledCount;
        InitializeComponent();
        DataContext = this;
        SummaryText.Text = _failedCount == 0
            ? $"有 {_scheduledCount} 项残留无法立即删除，已安排在下次 Windows 重启时自动删除。"
            : $"有 {_scheduledCount} 项已安排重启后删除；{_failedCount} 项仍然无法删除，重启后可重试。";
        Title = _failedCount == 0 ? "残留将在重启后删除" : "部分残留需要重启后删除";
    }

    private void Window_OnLoaded(object sender, RoutedEventArgs args) => UiMotion.PlayDialogOpen(this);

    private void RebootButton_OnClick(object sender, RoutedEventArgs args)
    {
        if (!SystemRestartService.TryRestart(out string error))
        {
            MessageBox.Show(
                $"无法启动重启：{error}\n\n请手动重启 Windows 以完成残留删除。",
                "无法重启",
                MessageBoxButton.OK,
                MessageBoxImage.Warning);
            return;
        }

        RebootButton.IsEnabled = false;
        DoneButton.IsEnabled = false;
        StatusText.Text = "计算机将在 5 秒后重启，重启后会自动删除已安排的残留。";
    }

    private void DoneButton_OnClick(object sender, RoutedEventArgs args) => Close();
}
