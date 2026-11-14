using WinAppsManager.Models;

namespace WinAppsManager.Services;

/// <summary>Outcome of deleting one selected leftover (registry key, registry value or file).</summary>
public sealed class LeftoverCleanupResult
{
    public LeftoverCleanupResult(LeftoverNode node, bool success, string message)
    {
        Node = node;
        Success = success;
        Message = message;
    }

    public LeftoverNode Node { get; }
    public bool Success { get; }
    public string Message { get; }
}

/// <summary>
/// Entry point used by the deep-clean window: splits the selection into registry items and
/// file-system items and lets each cleaner re-validate every path before touching it.
/// </summary>
public static class LeftoverCleaner
{
    public static IReadOnlyList<LeftoverCleanupResult> Delete(
        IEnumerable<LeftoverNode> nodes,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(nodes);
        var selected = nodes.Where(node => node is not null).ToArray();
        var results = new List<LeftoverCleanupResult>();

        var registryNodes = selected.Where(node => !node.IsFileSystemItem).ToArray();
        if (registryNodes.Length > 0)
        {
            results.AddRange(RegistryLeftoverCleaner.Delete(registryNodes, cancellationToken));
        }

        var fileNodes = selected.Where(node => node.IsFileSystemItem).ToArray();
        if (fileNodes.Length > 0)
        {
            results.AddRange(FileLeftoverCleaner.Delete(fileNodes, cancellationToken));
        }

        return results;
    }

    /// <summary>Validation used by tests and by the UI when it explains why an item is skipped.</summary>
    public static bool IsDeletable(LeftoverNode? node, out string reason)
        => node is { IsFileSystemItem: true }
            ? FileLeftoverCleaner.IsDeletable(node, out reason)
            : RegistryLeftoverCleaner.IsDeletable(node, out reason);
}
