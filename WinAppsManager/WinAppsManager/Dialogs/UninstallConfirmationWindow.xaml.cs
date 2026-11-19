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
        RelatedStoreNotice.Text = applications
            .Where(application => application.IsStoreApplication && application.HasRelatedPackageApplications)
            .SelectMany(application => application.RelatedPackageApplications.Select(related => related.Name))
            .Distinct(StringComparer.CurrentCultureIgnoreCase)
            .Take(4)
            .ToArray() is { Length: > 0 } relatedNames
            ? $"所选商店应用所在的软件包还包含：{string.Join("、", relatedNames)}。Windows 会一起卸载该包内的所有应用入口。"
            : string.Empty;
        RelatedStoreNotice.Visibility = RelatedStoreNotice.Text.Length > 0
            ? Visibility.Visible
            : Visibility.Collapsed;
        // 无边框弹窗的入场动画统一走 UiMotion；动画在窗口首次渲染前启动，不会闪一下完整界面。
        Loaded += (_, _) => UiMotion.PlayDialogOpen(this);
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
