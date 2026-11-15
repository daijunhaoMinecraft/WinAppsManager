using WinAppsManager.Models;

namespace WinAppsManager.Services;

/// <summary>
/// 卸载成功后自动执行的“安全残留清理”：扫描与手动深度清理相同的残留树，但只删除
/// 默认勾选且非危险项的叶子节点（通常是注册表卸载项、启动项等）；安装目录、AppData、
/// 快捷方式和 MSI 记录等危险项不在自动清理范围内，仍由“强制卸载”手动确认。
/// </summary>
public static class AutoLeftoverCleaner
{
    /// <summary>扫描并删除安全的默认残留；任何异常由调用方记录，不阻塞卸载结果。</summary>
    public static async Task<AutoCleanSummary> CleanAsync(
        InstalledApplication application,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(application);

        var groups = await LeftoverScanner.ScanAsync(application, cancellationToken).ConfigureAwait(false);
        var selected = groups
            .SelectMany(group => group.SelectedLeaves())
            .Where(node => !node.IsRisky)
            .ToArray();
        if (selected.Length == 0)
        {
            return new AutoCleanSummary(0, 0);
        }

        var results = await Task.Run(
                () => LeftoverCleaner.Delete(selected, cancellationToken),
                cancellationToken)
            .ConfigureAwait(false);
        return new AutoCleanSummary(
            results.Count(result => result.Success),
            results.Count(result => !result.Success));
    }
}

/// <summary>自动残留清理结果：成功删除数与删除失败数。</summary>
public readonly record struct AutoCleanSummary(int Deleted, int Failed);
