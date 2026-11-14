using System.Windows;
using WinAppsManager.Models;
using WinAppsManager.Services;

namespace WinAppsManager.Dialogs;

public partial class UninstallConfirmationWindow : Window
{
    private readonly int _count;

    public UninstallConfirmationWindow(IReadOnlyList<InstalledApplication> applications)
    {
        ArgumentNullException.ThrowIfNull(applications);
        _count = applications.Count;
        InitializeComponent();
        ApplicationsList.ItemsSource = applications;
        SummaryText.Text = $"即将卸载 {applications.Count} 个软件。";
        StoreNoticePanel.Visibility = applications.Any(application => application.IsStoreApplication)
            ? Visibility.Visible
            : Visibility.Collapsed;
    }

    private void ConfirmButton_OnClick(object sender, RoutedEventArgs args)
    {
        AppLogService.Verbose($"用户确认卸载 {_count} 个软件。");
        DialogResult = true;
    }

    private void CancelButton_OnClick(object sender, RoutedEventArgs args)
    {
        AppLogService.Verbose($"用户取消卸载确认：{_count} 个软件未执行卸载。");
        DialogResult = false;
    }
}
