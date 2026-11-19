using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Windows;
using WinAppsManager.Models;
using WinAppsManager.Services;

namespace WinAppsManager.Dialogs;

/// <summary>
/// Deep-clean wizard. It scans one or more applications (a batch uninstall passes every software
/// that was confirmed removed), merges their leftovers into one checkable tree, and deletes only
/// what the user selected. Files kept open by normal user processes are released by ending those
/// processes; anything still undeletable is offered for deletion on the next Windows restart.
/// </summary>
public partial class DeepCleanWindow : Window
{
    private readonly IReadOnlyList<InstalledApplication> _applications;
    private readonly List<PendingRebootItem> _pendingRebootItems = new();
    private CancellationTokenSource? _operation;
    private bool _started;
    private bool _isBusy;

    public ObservableCollection<LeftoverNode> Groups { get; } = new();

    /// <summary>Number of leftover items removed immediately during this session.</summary>
    public int DeletedCount { get; private set; }

    public int ScheduledForRebootCount { get; private set; }

    public DeepCleanWindow(InstalledApplication application)
        : this(new[] { application })
    {
    }

    public DeepCleanWindow(IReadOnlyList<InstalledApplication> applications)
    {
        ArgumentNullException.ThrowIfNull(applications);
        if (applications.Count == 0)
        {
            throw new ArgumentException("至少需要一个软件才能扫描残留。", nameof(applications));
        }

        _applications = applications.ToArray();
        InitializeComponent();
        DataContext = this;
        AppInfoPanel.DataContext = _applications[0];
        AppNameText.Text = BuildApplicationNames();
        AppDetailText.Text = _applications.Count == 1
            ? $"{_applications[0].PublisherText} · {_applications[0].SourceText}"
            : $"共 {_applications.Count} 个软件 · 残留已合并到同一列表";
        Title = _applications.Count == 1
            ? $"强制卸载 - {_applications[0].Name}"
            : $"强制卸载 - {_applications.Count} 个软件";
        SummaryText.Text = "正在读取注册表…";
    }

    private string ApplicationLogText => _applications.Count == 1
        ? $"{_applications[0].Name}（{_applications[0].Id}）"
        : $"{string.Join("、", _applications.Take(5).Select(application => application.Name))} 等 " +
          $"{_applications.Count} 个软件";

    private string BuildApplicationNames()
    {
        const int limit = 3;
        string names = string.Join("、", _applications.Take(limit).Select(application => application.Name));
        return _applications.Count > limit ? $"{names} 等 {_applications.Count} 个软件" : names;
    }

    private async void Window_OnLoaded(object sender, RoutedEventArgs args)
    {
        if (_started)
        {
            return;
        }

        _started = true;

        UiMotion.PlayDialogOpen(this);
        await ScanAsync();
    }

    private async Task ScanAsync()
    {
        var stopwatch = Stopwatch.StartNew();
        SetBusy(true, "正在读取注册表…");
        AppLogService.Info($"开始扫描残留：{ApplicationLogText}。");
        int failedApplications = 0;
        try
        {
            var operation = new CancellationTokenSource();
            _operation = operation;
            foreach (var oldGroup in Groups)
            {
                oldGroup.PropertyChanged -= OnNodePropertyChanged;
            }

            Groups.Clear();
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            int applicationIndex = 0;
            int found = 0;

            foreach (var application in _applications)
            {
                operation.Token.ThrowIfCancellationRequested();
                applicationIndex++;
                BusyText.Text =
                    $"正在扫描：{application.Name}（{applicationIndex}/{_applications.Count}）…";
                SummaryText.Text = BusyText.Text;
                AppLogService.Info(
                    $"开始扫描残留：{application.Name}（{application.Id}），来源={application.SourceText}。");

                var applicationStopwatch = Stopwatch.StartNew();
                IReadOnlyList<LeftoverNode> nodes;
                try
                {
                    nodes = await LeftoverScanner.ScanAsync(application, operation.Token);
                }
                catch (OperationCanceledException)
                {
                    throw;
                }
                catch (Exception exception)
                {
                    failedApplications++;
                    Debug.WriteLine(exception);
                    AppLogService.Error(
                        $"扫描残留失败：{application.Name}（{application.Id}），" +
                        $"{exception.GetType().Name}：{exception.Message}，" +
                        $"耗时 {applicationStopwatch.ElapsedMilliseconds} ms。",
                        exception);
                    continue;
                }

                int applicationCount = 0;
                foreach (var group in nodes)
                {
                    var merged = CreateMergedGroup(
                        application,
                        group,
                        seen,
                        prefixWithApplication: _applications.Count > 1);
                    if (merged is null)
                    {
                        continue;
                    }

                    merged.PropertyChanged += OnNodePropertyChanged;
                    Groups.Add(merged);
                    int count = CountDeletable(merged);
                    applicationCount += count;
                    found += count;
                }

                AppLogService.Info(
                    $"残留扫描完成：{application.Name}（{application.Id}），" +
                    $"{nodes.Count} 组、{applicationCount} 项可删除残留，" +
                    $"耗时 {applicationStopwatch.ElapsedMilliseconds} ms。");
            }

            if (Groups.Count == 0)
            {
                ShowEmptyState("没有发现软件残留",
                    failedApplications > 0
                        ? "扫描失败的软件未计入；其余软件没有留下可识别的注册表项、文件夹或快捷方式残留。"
                        : "没有发现这些软件留下的注册表项、文件夹或快捷方式残留。");
                SummaryText.Text = failedApplications > 0
                    ? $"扫描完成：没有发现残留，{failedApplications} 个软件扫描失败。"
                    : "扫描完成：没有发现软件残留。";
                AppLogService.Info(
                    $"残留扫描完成：{ApplicationLogText}，没有发现残留" +
                    (failedApplications > 0 ? $"，{failedApplications} 个软件扫描失败" : string.Empty) +
                    $"，耗时 {stopwatch.ElapsedMilliseconds} ms。");
            }
            else
            {
                EmptyPanel.Visibility = Visibility.Collapsed;
                LeftoverTree.Visibility = Visibility.Visible;
                int count = CountLeaves();
                SummaryText.Text =
                    $"扫描完成：{BuildApplicationNames()}，发现 {count} 项可能的残留。" +
                    "注册表项已默认勾选，文件夹、快捷方式和 MSI 记录默认不勾选；" +
                    "占用文件会先尝试结束占用进程，仍无法删除的会安排重启后删除。" +
                    (failedApplications > 0 ? $"（{failedApplications} 个软件扫描失败）" : string.Empty);
                AppLogService.Info(
                    $"残留扫描完成：{ApplicationLogText}，{Groups.Count} 组、{count} 项可删除残留" +
                    (failedApplications > 0 ? $"，{failedApplications} 个软件扫描失败" : string.Empty) +
                    $"，耗时 {stopwatch.ElapsedMilliseconds} ms。");
            }
        }
        catch (OperationCanceledException)
        {
            // The window is closing.
            AppLogService.Verbose(
                $"残留扫描已取消：{ApplicationLogText}，耗时 {stopwatch.ElapsedMilliseconds} ms。");
        }
        catch (Exception exception)
        {
            Debug.WriteLine(exception);
            SummaryText.Text = "扫描注册表时发生错误，请稍后重试。";
            AppLogService.Error(
                $"扫描残留失败：{ApplicationLogText}，{exception.GetType().Name}：{exception.Message}，" +
                $"耗时 {stopwatch.ElapsedMilliseconds} ms。",
                exception);
        }
        finally
        {
            SetBusy(false);
            UpdateSelectionState();
        }
    }

    private static LeftoverNode? CreateMergedGroup(
        InstalledApplication application,
        LeftoverNode source,
        HashSet<string> seen,
        bool prefixWithApplication)
    {
        var group = new LeftoverNode
        {
            Title = prefixWithApplication ? $"{application.Name} · {source.Title}" : source.Title,
            Subtitle = source.Subtitle,
            Badge = source.Badge,
            Kind = source.Kind,
            IsGroup = true,
            IsRisky = source.IsRisky,
            IsExpanded = source.IsExpanded
        };

        foreach (var child in source.Children)
        {
            // The same shared registry key or shortcut may be reported for several applications.
            // Deleting it once is enough, and keeping both would show duplicate rows.
            if (!seen.Add(NodeKey(child)))
            {
                continue;
            }

            group.AddChild(child);
        }

        return group.Children.Count == 0 ? null : group;
    }

    private static string NodeKey(LeftoverNode node)
    {
        if (node.FilePath.Length > 0)
        {
            return $"{node.Kind}|{node.FilePath}";
        }

        return $"{node.Kind}|{node.Hive}|{node.View}|{node.RegistryPath}|{node.ValueName}";
    }

    private void OnNodePropertyChanged(object? sender, PropertyChangedEventArgs args)
    {
        if (args.PropertyName != nameof(LeftoverNode.IsChecked))
        {
            return;
        }

        if (!Dispatcher.CheckAccess())
        {
            // Deletion runs on the thread pool; never touch WPF controls from there.
            Dispatcher.BeginInvoke(new Action(() =>
            {
                if (!_isBusy)
                {
                    UpdateSelectionState();
                }
            }));
            return;
        }

        UpdateSelectionState();
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

        ConfirmText.Text = $"将处理 {selected} 项勾选的残留。占用文件会先尝试结束占用进程；" +
                           "仍无法删除的会安排到下次 Windows 重启时删除。确认继续？";
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

        _pendingRebootItems.Clear();
        SetBusy(true, $"正在处理 {selected.Length} 项残留…");
        var stopwatch = Stopwatch.StartNew();
        AppLogService.Info($"开始删除残留：{ApplicationLogText}，勾选 {selected.Length} 项。");
        try
        {
            var operation = new CancellationTokenSource();
            _operation = operation;
            var results = await Task.Run(
                () => LeftoverCleaner.Delete(selected, operation.Token),
                operation.Token);
            ApplyDeletionResults(results);
            AppLogService.Info(
                $"残留清理完成：{ApplicationLogText}，耗时 {stopwatch.ElapsedMilliseconds} ms。");
        }
        catch (OperationCanceledException)
        {
            SummaryText.Text = "删除已取消，未删除的残留仍保留在列表中。";
            AppLogService.Info(
                $"残留删除已取消：{ApplicationLogText}，耗时 {stopwatch.ElapsedMilliseconds} ms。");
        }
        catch (Exception exception)
        {
            Debug.WriteLine(exception);
            SummaryText.Text = "删除过程中发生错误，请稍后重试。";
            AppLogService.Error(
                $"删除残留失败：{ApplicationLogText}，{exception.GetType().Name}：{exception.Message}，" +
                $"耗时 {stopwatch.ElapsedMilliseconds} ms。",
                exception);
        }
        finally
        {
            SetBusy(false);
            UpdateSelectionState();
            ShowPendingRebootDialog();
        }
    }

    private void ApplyDeletionResults(IReadOnlyList<LeftoverCleanupResult> results)
    {
        var completed = results.Where(result => result.Success).ToArray();
        foreach (var result in completed.Where(result =>
                     result.Disposition == LeftoverCleanupDisposition.ScheduledForReboot))
        {
            result.Node.SetCleanupDisposition(LeftoverCleanupDisposition.ScheduledForReboot);
        }

        var deleted = new HashSet<LeftoverNode>(completed
            .Where(result => result.Disposition == LeftoverCleanupDisposition.Deleted)
            .Select(result => result.Node));
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

        int scheduled = completed.Count(result =>
            result.Disposition == LeftoverCleanupDisposition.ScheduledForReboot);
        int removed = completed.Length - scheduled;
        int failed = results.Count(result =>
            result.Disposition == LeftoverCleanupDisposition.Failed);
        DeletedCount += completed.Count(result =>
            result.Disposition == LeftoverCleanupDisposition.Deleted);
        ScheduledForRebootCount += scheduled;

        foreach (var result in results.Where(result =>
                     result.Disposition is LeftoverCleanupDisposition.ScheduledForReboot or
                         LeftoverCleanupDisposition.Failed))
        {
            string path = result.Node.DisplayPath;
            if (_pendingRebootItems.Any(item =>
                    string.Equals(item.Path, path, StringComparison.OrdinalIgnoreCase)))
            {
                continue;
            }

            _pendingRebootItems.Add(new PendingRebootItem(
                path,
                result.Disposition == LeftoverCleanupDisposition.ScheduledForReboot,
                result.Message));
        }

        if (CountLeaves() == 0)
        {
            ShowEmptyState("残留已处理",
                "可立即删除的项目已清理；已安排重启删除的项目会在 Windows 重启后处理。");
        }

        SummaryText.Text = BuildCleanupSummary(removed, failed);
        if (failed == 0 && ScheduledForRebootCount == 0)
        {
            AppLogService.Info($"残留删除结果：立即删除 {removed} 项，失败 0 项。");
        }
        else
        {
            // 失败项可能很多，只把前几条原因写进日志，其余只记数量。
            string details = string.Join("；", results
                .Where(result => result.Disposition == LeftoverCleanupDisposition.Failed)
                .Take(5)
                .Select(result => result.Message));
            AppLogService.Warn(
                $"残留清理结果：立即删除 {removed} 项、重启后删除 {scheduled} 项、失败 {failed} 项。" +
                $"失败原因示例：{details}");
        }
    }

    private void ShowPendingRebootDialog()
    {
        if (_pendingRebootItems.Count == 0)
        {
            return;
        }

        var items = _pendingRebootItems.ToArray();
        _pendingRebootItems.Clear();
        var dialog = new PendingRebootWindow(items) { Owner = this };
        dialog.ShowDialog();
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

    private string BuildCleanupSummary(int succeeded, int failed)
    {
        string summary = $"已删除 {succeeded} 项残留";
        if (ScheduledForRebootCount > 0)
        {
            summary += $"；已安排 {ScheduledForRebootCount} 项在下次 Windows 重启时删除";
        }

        if (failed > 0)
        {
            summary += $"；{failed} 项无法删除，请重启 Windows 后重试";
        }

        return summary + "。";
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

        bool hasItems = CountLeaves() > 0;
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
