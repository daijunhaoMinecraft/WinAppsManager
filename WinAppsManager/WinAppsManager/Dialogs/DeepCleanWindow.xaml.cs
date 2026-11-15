using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Windows;
using WinAppsManager.Models;
using WinAppsManager.Services;

namespace WinAppsManager.Dialogs;

/// <summary>
/// Deep-clean wizard: scans the registry for leftovers of one desktop application, shows them
/// as a checkable tree grouped by kind, and deletes only what the user selected.
/// </summary>
public partial class DeepCleanWindow : Window
{
    private readonly InstalledApplication _application;
    private CancellationTokenSource? _operation;
    private bool _started;
    private bool _isBusy;

    public ObservableCollection<LeftoverNode> Groups { get; } = new();

    /// <summary>Number of registry items deleted in this session.</summary>
    public int DeletedCount { get; private set; }

    public DeepCleanWindow(InstalledApplication application)
    {
        ArgumentNullException.ThrowIfNull(application);
        _application = application;
        InitializeComponent();
        DataContext = this;
        AppInfoPanel.DataContext = application;
        AppNameText.Text = application.Name;
        AppDetailText.Text = $"{application.PublisherText} · {application.SourceText}";
        Title = $"强制卸载 - {application.Name}";
        SummaryText.Text = "正在读取注册表…";
    }

    private async void Window_OnLoaded(object sender, RoutedEventArgs args)
    {
        if (_started)
        {
            return;
        }

        _started = true;

        await ScanAsync();
    }

    private async Task ScanAsync()
    {
        var stopwatch = Stopwatch.StartNew();
        SetBusy(true, "正在扫描注册表…");
        AppLogService.Info($"开始扫描残留：{_application.Name}（{_application.Id}），来源={_application.SourceText}。");
        try
        {
            var operation = new CancellationTokenSource();
            _operation = operation;
            var nodes = await LeftoverScanner.ScanAsync(_application, operation.Token);
            Groups.Clear();
            foreach (var node in nodes)
            {
                node.PropertyChanged += OnNodePropertyChanged;
                Groups.Add(node);
            }

            if (Groups.Count == 0)
            {
                ShowEmptyState("没有发现注册表残留",
                    "没有发现该软件留下的注册表项或文件夹残留。");
                SummaryText.Text = "扫描完成：没有发现注册表残留。";
                AppLogService.Info(
                    $"残留扫描完成：{_application.Name}（{_application.Id}），没有发现残留，耗时 {stopwatch.ElapsedMilliseconds} ms。");
            }
            else
            {
                EmptyPanel.Visibility = Visibility.Collapsed;
                LeftoverTree.Visibility = Visibility.Visible;
                int count = CountLeaves();
                SummaryText.Text = $"扫描完成：发现 {count} 项可能的残留。注册表项已默认勾选，文件夹、快捷方式和 MSI 记录默认不勾选。";
                AppLogService.Info(
                    $"残留扫描完成：{_application.Name}（{_application.Id}），{Groups.Count} 组、{count} 项可删除残留，" +
                    $"耗时 {stopwatch.ElapsedMilliseconds} ms。");
            }
        }
        catch (OperationCanceledException)
        {
            // The window is closing.
            AppLogService.Verbose(
                $"残留扫描已取消：{_application.Name}（{_application.Id}），耗时 {stopwatch.ElapsedMilliseconds} ms。");
        }
        catch (Exception exception)
        {
            Debug.WriteLine(exception);
            SummaryText.Text = "扫描注册表时发生错误，请稍后重试。";
            AppLogService.Error(
                $"扫描残留失败：{_application.Name}（{_application.Id}），{exception.GetType().Name}：{exception.Message}，" +
                $"耗时 {stopwatch.ElapsedMilliseconds} ms。",
                exception);
        }
        finally
        {
            SetBusy(false);
            UpdateSelectionState();
        }
    }

    private void OnNodePropertyChanged(object? sender, PropertyChangedEventArgs args)
    {
        if (args.PropertyName == nameof(LeftoverNode.IsChecked))
        {
            UpdateSelectionState();
        }
    }

    private async void RescanButton_OnClick(object sender, RoutedEventArgs args) => await ScanAsync();

    private void SelectAllButton_OnClick(object sender, RoutedEventArgs args)
    {
        foreach (var group in Groups)
        {
            group.IsChecked = true;
        }

        UpdateSelectionState();
    }

    private void SelectNoneButton_OnClick(object sender, RoutedEventArgs args)
    {
        foreach (var group in Groups)
        {
            group.IsChecked = false;
        }

        UpdateSelectionState();
    }

    private void DeleteButton_OnClick(object sender, RoutedEventArgs args)
    {
        int selected = CountSelected();
        if (selected == 0 || _isBusy)
        {
            return;
        }

        ConfirmText.Text = $"将删除 {selected} 项注册表残留，删除后无法恢复。确认继续？";
        ConfirmBar.Visibility = Visibility.Visible;
        DeleteButton.IsEnabled = false;
    }

    private void CancelConfirmButton_OnClick(object sender, RoutedEventArgs args)
    {
        ConfirmBar.Visibility = Visibility.Collapsed;
        UpdateSelectionState();
    }

    private async void ConfirmDeleteButton_OnClick(object sender, RoutedEventArgs args)
    {
        var selected = Groups.SelectMany(group => group.SelectedLeaves()).ToArray();
        ConfirmBar.Visibility = Visibility.Collapsed;
        if (selected.Length == 0)
        {
            UpdateSelectionState();
            return;
        }

        SetBusy(true, $"正在删除 {selected.Length} 项注册表残留…");
        var stopwatch = Stopwatch.StartNew();
        AppLogService.Info(
            $"开始删除残留：{_application.Name}（{_application.Id}），勾选 {selected.Length} 项。");
        try
        {
            var operation = new CancellationTokenSource();
            _operation = operation;
            var results = await Task.Run(
                () => LeftoverCleaner.Delete(selected, operation.Token),
                operation.Token);
            ApplyDeletionResults(results);
            AppLogService.Info(
                $"残留删除完成：{_application.Name}（{_application.Id}），耗时 {stopwatch.ElapsedMilliseconds} ms。");
        }
        catch (OperationCanceledException)
        {
            SummaryText.Text = "删除已取消，未删除的残留仍保留在列表中。";
            AppLogService.Info(
                $"残留删除已取消：{_application.Name}（{_application.Id}），耗时 {stopwatch.ElapsedMilliseconds} ms。");
        }
        catch (Exception exception)
        {
            Debug.WriteLine(exception);
            SummaryText.Text = "删除过程中发生错误，请稍后重试。";
            AppLogService.Error(
                $"删除残留失败：{_application.Name}（{_application.Id}），{exception.GetType().Name}：{exception.Message}，耗时 {stopwatch.ElapsedMilliseconds} ms。",
                exception);
        }
        finally
        {
            SetBusy(false);
            UpdateSelectionState();
        }
    }

    private void ApplyDeletionResults(IReadOnlyList<LeftoverCleanupResult> results)
    {
        var deleted = new HashSet<LeftoverNode>(
            results.Where(result => result.Success).Select(result => result.Node));
        foreach (var group in Groups.ToArray())
        {
            PruneDeleted(group, deleted);
            group.RefreshSelection();
            if (group.Children.Count == 0)
            {
                group.PropertyChanged -= OnNodePropertyChanged;
                Groups.Remove(group);
            }
        }

        int succeeded = results.Count(result => result.Success);
        int failed = results.Count - succeeded;
        DeletedCount += succeeded;

        if (Groups.Count == 0)
        {
            ShowEmptyState("残留已清理", "本次勾选的注册表残留已全部删除。");
        }

        SummaryText.Text = failed == 0
            ? $"已删除 {succeeded} 项注册表残留。"
            : $"已删除 {succeeded} 项，{failed} 项删除失败。失败项多为权限不足，可尝试以管理员身份运行本应用后重试。";
        if (failed == 0)
        {
            AppLogService.Info($"残留删除结果：成功 {succeeded} 项，失败 0 项。");
        }
        else
        {
            // 失败项可能很多，只把前几条原因写进日志，其余只记数量。
            string details = string.Join("；", results
                .Where(result => !result.Success)
                .Take(5)
                .Select(result => result.Message));
            AppLogService.Warn(
                $"残留删除结果：成功 {succeeded} 项，失败 {failed} 项。失败原因示例：{details}");
        }
    }

    /// <summary>Drops deleted nodes and folders that disappeared from disk after their files went away.</summary>
    private static void PruneDeleted(LeftoverNode node, HashSet<LeftoverNode> deleted)
    {
        foreach (var child in node.Children.ToArray())
        {
            if (deleted.Contains(child))
            {
                node.Children.Remove(child);
                continue;
            }

            PruneDeleted(child, deleted);
            if (child.Children.Count == 0 && IsFolderKind(child.Kind) && !Directory.Exists(child.FilePath))
            {
                node.Children.Remove(child);
            }
        }
    }

    private static bool IsFolderKind(LeftoverKind kind) => kind is LeftoverKind.InstallFolder
        or LeftoverKind.AppDataFolder or LeftoverKind.FolderItem;

    private int CountLeaves() => Groups.Sum(CountDeletable);

    private int CountSelected() => Groups.Sum(group => group.SelectedLeaves().Count());

    private static int CountDeletable(LeftoverNode node)
    {
        if (node.Children.Count == 0)
        {
            return node.IsDeletable ? 1 : 0;
        }

        return node.Children.Sum(CountDeletable);
    }

    private void UpdateSelectionState()
    {
        int selected = CountSelected();
        int total = CountLeaves();
        SelectionText.Text = $"已选择 {selected} 项 / 共 {total} 项";
        DeleteButton.Content = selected > 0 ? $"删除选中 ({selected})" : "删除选中";
        bool hasItems = total > 0;
        DeleteButton.IsEnabled = !_isBusy && selected > 0;
        SelectAllButton.IsEnabled = !_isBusy && hasItems;
        SelectNoneButton.IsEnabled = !_isBusy && hasItems;
    }

    private void SetBusy(bool busy, string? message = null)
    {
        _isBusy = busy;
        BusyPanel.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
        if (message is not null)
        {
            BusyText.Text = message;
        }

        RescanButton.Visibility = busy ? Visibility.Collapsed : Visibility.Visible;
        RescanButton.IsEnabled = !busy;
        if (busy)
        {
            LeftoverTree.Visibility = Visibility.Collapsed;
            DeleteButton.IsEnabled = false;
            SelectAllButton.IsEnabled = false;
            SelectNoneButton.IsEnabled = false;
            return;
        }

        bool hasItems = Groups.Count > 0;
        LeftoverTree.Visibility = hasItems ? Visibility.Visible : Visibility.Collapsed;
        SelectAllButton.IsEnabled = hasItems;
        SelectNoneButton.IsEnabled = hasItems;
        UpdateSelectionState();
    }

    private void ShowEmptyState(string title, string description)
    {
        EmptyText.Text = title;
        EmptyDescriptionText.Text = description;
        LeftoverTree.Visibility = Visibility.Collapsed;
        EmptyPanel.Visibility = Visibility.Visible;
    }

    private void CloseButton_OnClick(object sender, RoutedEventArgs args) => Close();

    private void Window_OnClosing(object? sender, CancelEventArgs args)
    {
        _operation?.Cancel();
    }
}
