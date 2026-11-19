using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Runtime.CompilerServices;
using Microsoft.Win32;

namespace WinAppsManager.Models;

/// <summary>Which kind of leftover a deep-clean node represents.</summary>
public enum LeftoverKind
{
    UninstallEntry,
    ProductData,
    StartupEntry,
    ServiceEntry,
    AppPathEntry,
    InstallFolder,
    AppDataFolder,
    Shortcut,
    FolderItem,
    FileItem,
    MsiProductKey,
    MsiComponentKey,
    MsiInstallerFolder,
    StoreStateKey
}

public enum LeftoverCleanupDisposition
{
    Deleted,
    ScheduledForReboot,
    Failed
}

/// <summary>
/// One node of the deep-clean result tree. Group nodes are categories and are never deleted;
/// only leaf nodes carry a deletable registry path, registry value or file-system path.
/// </summary>
public sealed class LeftoverNode : INotifyPropertyChanged
{
    private bool? _isChecked = true;
    private bool _isExpanded;

    public required string Title { get; init; }
    public string Subtitle { get; init; } = string.Empty;
    public string Badge { get; init; } = string.Empty;

    /// <summary>Path relative to the hive root, for example <c>SOFTWARE\Vendor\App</c>.</summary>
    public string RegistryPath { get; init; } = string.Empty;

    /// <summary>Absolute file-system path for folder and shortcut leftovers.</summary>
    public string FilePath { get; init; } = string.Empty;

    /// <summary>
    /// Scan root a file-system item belongs to (the install folder, the AppData folder or the
    /// shortcut itself). The cleaner only allows deleting paths below this root.
    /// </summary>
    public string RootPath { get; init; } = string.Empty;

    /// <summary>File size in bytes for file items.</summary>
    public long? SizeBytes { get; init; }

    /// <summary>Set when the node deletes a single value instead of a whole key.</summary>
    public string? ValueName { get; init; }

    public RegistryHive Hive { get; init; } = RegistryHive.CurrentUser;
    public RegistryView View { get; init; } = RegistryView.Default;
    public LeftoverKind Kind { get; init; }
    public bool IsGroup { get; init; }

    /// <summary>True for items that can destroy files or MSI database rows; unchecked by default.</summary>
    public bool IsRisky { get; init; }

    /// <summary>Set after a deletion attempt so the UI can distinguish reboot scheduling from removal.</summary>
    public LeftoverCleanupDisposition? CleanupDisposition { get; private set; }

    public ObservableCollection<LeftoverNode> Children { get; } = new();
    public LeftoverNode? Parent { get; private set; }

    public event PropertyChangedEventHandler? PropertyChanged;

    /// <summary>
    /// Group nodes select or clear the whole subtree; leaf nodes report their state up so
    /// the parent category stays in sync. A mixed group shows as unchecked.
    /// </summary>
    public bool? IsChecked
    {
        get => _isChecked;
        set
        {
            bool target = value == true;
            if (IsGroup)
            {
                ApplyToSubtree(target);
                Parent?.RefreshFromChildren();
                return;
            }

            if (_isChecked == target)
            {
                return;
            }

            _isChecked = target;
            OnPropertyChanged();
            Parent?.RefreshFromChildren();
        }
    }

    /// <summary>
    /// True only for a selectable item that has no children below it; folder items only act as
    /// containers, so that unchecking one file cannot be overridden by its parent folder.
    /// </summary>
    public bool IsDeletable => !IsGroup &&
                               CleanupDisposition != LeftoverCleanupDisposition.ScheduledForReboot &&
                               Children.Count == 0 &&
                               (RegistryPath.Length > 0 || FilePath.Length > 0);

    public bool IsSelectable => IsGroup || CleanupDisposition != LeftoverCleanupDisposition.ScheduledForReboot;

    public string CleanupStatusText => CleanupDisposition switch
    {
        LeftoverCleanupDisposition.ScheduledForReboot => "已安排重启后删除",
        _ => string.Empty
    };

    public bool HasCleanupStatus => CleanupStatusText.Length > 0;

    public bool IsFileSystemItem => FilePath.Length > 0;

    public bool IsExpanded
    {
        get => _isExpanded;
        set
        {
            if (_isExpanded == value)
            {
                return;
            }

            _isExpanded = value;
            OnPropertyChanged();
        }
    }

    public string DisplayPath => FilePath.Length > 0
        ? FilePath
        : RegistryPath.Length == 0
            ? Title
            : $"{HivePrefix(Hive)}\\{RegistryPath}";

    public void AddChild(LeftoverNode child)
    {
        child.Parent = this;
        Children.Add(child);
        RefreshFromChildren();
    }

    public void SetCleanupDisposition(LeftoverCleanupDisposition disposition)
    {
        if (CleanupDisposition == disposition)
        {
            return;
        }

        CleanupDisposition = disposition;
        if (disposition == LeftoverCleanupDisposition.ScheduledForReboot)
        {
            _isChecked = false;
            OnPropertyChanged(nameof(IsChecked));
        }

        OnPropertyChanged(nameof(CleanupDisposition));
        OnPropertyChanged(nameof(CleanupStatusText));
        OnPropertyChanged(nameof(HasCleanupStatus));
        OnPropertyChanged(nameof(IsSelectable));
        Parent?.RefreshFromChildren();
    }

    public void MarkScheduledForReboot() => SetCleanupDisposition(LeftoverCleanupDisposition.ScheduledForReboot);

    /// <summary>Recomputes the checkbox state from the current children after a tree edit.</summary>
    public void RefreshSelection() => RefreshFromChildren();

    /// <summary>Collects every selected leaf below this node.</summary>
    public IEnumerable<LeftoverNode> SelectedLeaves()
    {
        foreach (var child in Children)
        {
            if (child.Children.Count > 0)
            {
                foreach (var leaf in child.SelectedLeaves())
                {
                    yield return leaf;
                }
            }
            else if (child._isChecked == true && child.IsDeletable)
            {
                yield return child;
            }
        }
    }

    /// <summary>Number of files in this subtree (a shortcut counts as one file).</summary>
    public int FileCount()
    {
        if (Children.Count == 0)
        {
            return Kind is LeftoverKind.FileItem or LeftoverKind.Shortcut ? 1 : 0;
        }

        return Children.Sum(child => child.FileCount());
    }

    /// <summary>Total size of the files in this subtree.</summary>
    public long TotalSizeBytes()
    {
        if (Children.Count == 0)
        {
            return SizeBytes ?? 0;
        }

        return Children.Sum(child => child.TotalSizeBytes());
    }

    public static string HivePrefix(RegistryHive hive) => hive switch
    {
        RegistryHive.LocalMachine => "HKEY_LOCAL_MACHINE",
        RegistryHive.CurrentUser => "HKEY_CURRENT_USER",
        RegistryHive.Users => "HKEY_USERS",
        _ => hive.ToString().ToUpperInvariant()
    };

    private void ApplyToSubtree(bool value)
    {
        value = value && CleanupDisposition != LeftoverCleanupDisposition.ScheduledForReboot;
        foreach (var child in Children)
        {
            child.ApplyToSubtree(value);
        }

        if (_isChecked != value)
        {
            _isChecked = value;
            OnPropertyChanged(nameof(IsChecked));
        }
    }

    private void RefreshFromChildren()
    {
        if (Children.Count == 0)
        {
            return;
        }

        bool all = Children.All(child => child._isChecked == true);
        bool any = Children.Any(child => child._isChecked != false);
        bool? state = all ? true : any ? null : false;
        if (_isChecked != state)
        {
            _isChecked = state;
            OnPropertyChanged(nameof(IsChecked));
        }

        Parent?.RefreshFromChildren();
    }

    private void OnPropertyChanged([CallerMemberName] string? propertyName = null)
        => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propertyName));
}
