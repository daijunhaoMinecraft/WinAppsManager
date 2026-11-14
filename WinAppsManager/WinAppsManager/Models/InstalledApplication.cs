using System.ComponentModel;
using System.Globalization;
using System.Runtime.CompilerServices;
using System.Windows.Media;
using Microsoft.Win32;

namespace WinAppsManager.Models;

/// <summary>
/// Categorises an uninstall entry:
/// normal applications, Windows system components and updates it hides, and Store packages.
/// </summary>
public enum ApplicationCategory
{
    Application,
    SystemComponent,
    HiddenComponent,
    StoreApplication
}

/// <summary>
/// A desktop application discovered from the Windows uninstall registry keys.
/// Only values needed for the first direct-uninstall implementation are kept.
/// </summary>
public sealed class InstalledApplication : INotifyPropertyChanged
{
    private static readonly string[] AvatarPalette =
    [
        "#0067C0", "#0F6CBD", "#107C10", "#8764B8", "#C239B3", "#CA5010",
        "#986F0B", "#7A7574", "#005B70", "#4F6BED", "#C50F1F", "#038387"
    ];

    private static readonly HashSet<string> HiddenReleaseTypes = new(StringComparer.OrdinalIgnoreCase)
    {
        "Update",
        "Hotfix",
        "Security Update",
        "Update Rollup",
        "ServicePack"
    };

    private bool _isSelected;
    private ImageSource? _iconSource;

    public event PropertyChangedEventHandler? PropertyChanged;

    public required string Id { get; init; }
    public required string Name { get; init; }
    public string Version { get; init; } = string.Empty;
    public string Publisher { get; init; } = string.Empty;
    public string SourceText { get; init; } = string.Empty;
    public ApplicationCategory Category { get; init; } = ApplicationCategory.Application;
    public bool IsStoreApplication => Category == ApplicationCategory.StoreApplication;
    public bool IsSystemComponent => Category == ApplicationCategory.SystemComponent;
    public bool IsHiddenComponent => Category == ApplicationCategory.HiddenComponent;
    public bool IsApplication => Category == ApplicationCategory.Application;
    public string UninstallString { get; init; } = string.Empty;
    public string DisplayIcon { get; init; } = string.Empty;
    public string InstallLocation { get; init; } = string.Empty;
    public DateOnly? InstallDate { get; internal set; }
    private long? _estimatedSizeBytes;
    private string? _sizeText;
    private string? _installDateText;

    public long? EstimatedSizeBytes
    {
        get => _estimatedSizeBytes;
        private set
        {
            _estimatedSizeBytes = value;
            _sizeText = null;
        }
    }

    /// <summary>Website from <c>URLInfoAbout</c> (or <c>HelpLink</c>) in the uninstall key.</summary>
    public string UrlInfoAbout { get; init; } = string.Empty;

    public bool HasUrl => UrlInfoAbout.Length > 0;

    /// <summary>
    /// True when the size or the install date still has to be read from the file system or from the
    /// uninstall key's own last write time; the date probe works without an <c>InstallLocation</c>.
    /// </summary>
    public bool NeedsFilesystemProbe =>
        !IsStoreApplication && (EstimatedSizeBytes is not > 0 || InstallDate is null);

    /// <summary>Hive that owns the uninstall registry key; unused for Store packages.</summary>
    public RegistryHive RegistryHive { get; init; } = RegistryHive.CurrentUser;

    /// <summary>Registry view the uninstall key was read from (32-bit entries need Registry32).</summary>
    public RegistryView RegistryView { get; init; } = RegistryView.Default;

    /// <summary>Path of the uninstall key relative to <see cref="RegistryHive"/>.</summary>
    public string RegistryPath { get; init; } = string.Empty;

    /// <summary>Package full name of a Microsoft Store / MSIX package.</summary>
    public string PackageFullName { get; init; } = string.Empty;

    /// <summary>Package identity name (<c>Package.Id.Name</c>), used by Get-AppxPackage.</summary>
    public string PackageName { get; init; } = string.Empty;

    /// <summary>Package family name (<c>Name_PublisherId</c>), used for AppModel state keys.</summary>
    public string PackageFamilyName { get; init; } = string.Empty;

    public bool HasRegistryEntry => RegistryPath.Length > 0;

    public ImageSource? IconSource
    {
        get => _iconSource;
        set
        {
            if (ReferenceEquals(_iconSource, value))
            {
                return;
            }

            _iconSource = value;
            OnPropertyChanged();
            OnPropertyChanged(nameof(HasIcon));
        }
    }

    public bool HasIcon => _iconSource is not null;

    public bool IsSelected
    {
        get => _isSelected;
        set
        {
            if (_isSelected == value)
            {
                return;
            }

            _isSelected = value;
            OnPropertyChanged();
        }
    }

    public bool HasVersion => Version.Length > 0;
    public string VersionText => Version.Length > 0 ? $"版本 {Version}" : "版本未知";    public string PublisherText => Publisher.Length > 0 ? $"发布者：{Publisher}" : "发布者未知";

    public string CategoryText => Category switch
    {
        ApplicationCategory.SystemComponent => "系统组件",
        ApplicationCategory.HiddenComponent => "隐藏组件",
        ApplicationCategory.StoreApplication => "商店应用",
        _ => "应用程序"
    };

    /// <summary>Short badge shown on the row; empty for a normal application.</summary>
    public string CategoryBadge => Category switch
    {
        ApplicationCategory.SystemComponent => "系统",
        ApplicationCategory.HiddenComponent => "隐藏",
        ApplicationCategory.StoreApplication => "商店",
        _ => string.Empty
    };

    public bool HasCategoryBadge => Category != ApplicationCategory.Application;
    // Cached: WPF re-reads these on every layout pass, and formatting them each time showed up
    // in profiles as repeated work while scrolling.
    public string SizeText => _sizeText ??= EstimatedSizeBytes is > 0
        ? FormatSize(EstimatedSizeBytes.Value)
        : "大小未知";

    /// <summary>
    /// Install date formatted as ("8月 15, 2026"). Unknown dates stay empty
    /// instead of saying "安装日期未知", so the column keeps its alignment.
    /// </summary>
    public string InstallDateText => _installDateText ??=
        InstallDate is { } date ? date.ToString("M月 d, yyyy") : string.Empty;

    /// <summary>Compact date used by the list and report layouts.</summary>
    public string ShortInstallDateText => InstallDateText;

    public string SummaryText => $"{PublisherText} · {SourceText}";

    public string DetailsText =>
        InstallDateText.Length > 0 ? $"{SizeText} · {InstallDateText}" : SizeText;

    public string LocationText => InstallLocation.Length > 0
        ? $"安装位置：{InstallLocation}"
        : "安装位置未知";

    public string Initial
    {
        get
        {
            if (Name.Length == 0)
            {
                return "?";
            }

            var element = StringInfo.GetNextTextElement(Name);
            return element.ToUpperInvariant();
        }
    }

    public string AvatarBackground => AvatarPalette[GetStableHash(Name) % AvatarPalette.Length];

    /// <summary>
    /// Filters the raw registry entry the same way the Programs and Features list hides
    /// updates, system components and entries that cannot be removed.
    /// </summary>
    public static InstalledApplication? TryCreate(UninstallRegistryEntry entry)
    {
        ArgumentNullException.ThrowIfNull(entry);

        string name = entry.DisplayName?.Trim() ?? string.Empty;
        if (name.Length == 0)
        {
            return null;
        }

        var category = Classify(entry);
        string uninstallString = entry.UninstallString?.Trim() ?? string.Empty;
        if (uninstallString.Length == 0 && category == ApplicationCategory.Application)
        {
            // A regular application without a command cannot be uninstalled or cleaned up.
            return null;
        }

        return new InstalledApplication
        {
            Id = entry.Id,
            Name = name,
            Version = entry.DisplayVersion?.Trim() ?? string.Empty,
            Publisher = entry.Publisher?.Trim() ?? string.Empty,
            SourceText = entry.HiveLabel,
            Category = category,
            UninstallString = uninstallString,
            DisplayIcon = entry.DisplayIcon?.Trim() ?? string.Empty,
            InstallLocation = TrimQuotes(entry.InstallLocation),
            InstallDate = ParseInstallDate(entry.InstallDate),
            EstimatedSizeBytes = entry.EstimatedSizeBytes is > 0 ? entry.EstimatedSizeBytes : null,
            UrlInfoAbout = (entry.UrlInfoAbout ?? string.Empty).Trim(),
            RegistryHive = entry.Hive,
            RegistryView = entry.View,
            RegistryPath = entry.RegistryPath
        };
    }

    /// <summary>
    /// Mirrors the flags read from the uninstall key: <c>SystemComponent</c> marks a
    /// Windows component, while <c>ParentKeyName</c>, <c>ReleaseType</c> and <c>NoRemove</c> mark the
    /// entries Programs and Features hides instead of listing.
    /// </summary>
    private static ApplicationCategory Classify(UninstallRegistryEntry entry)
    {
        if (entry.IsSystemComponent)
        {
            return ApplicationCategory.SystemComponent;
        }

        if (!string.IsNullOrWhiteSpace(entry.ParentKeyName) ||
            entry.NoRemove ||
            (!string.IsNullOrWhiteSpace(entry.ReleaseType) &&
             HiddenReleaseTypes.Contains(entry.ReleaseType.Trim())))
        {
            return ApplicationCategory.HiddenComponent;
        }

        return ApplicationCategory.Application;
    }

    /// <summary>
    /// Fills in a missing size or install date from the file system: the install folder is measured
    /// recursively, and the earlier of the uninstall key's and folder's last write times is used.
    /// </summary>
    public void ApplyFilesystemProbe(long? sizeBytes, DateOnly? installDate)
    {
        bool changed = false;
        if (sizeBytes is > 0 && EstimatedSizeBytes is not > 0)
        {
            EstimatedSizeBytes = sizeBytes;
            changed = true;
        }

        if (installDate is not null && InstallDate is null)
        {
            InstallDate = installDate;
            _installDateText = null;
            changed = true;
        }

        if (!changed)
        {
            return;
        }

        // One "everything changed" notification instead of five: the row is re-read once, which
        // keeps the list from re-measuring its text layout several times per probe result.
        OnPropertyChanged(string.Empty);
    }

    private static string TrimQuotes(string? value)
    {
        string text = value?.Trim() ?? string.Empty;
        return text.Length >= 2 && text[0] == '"' && text[^1] == '"'
            ? text[1..^1].Trim()
            : text;
    }

    private static DateOnly? ParseInstallDate(string? value)
    {
        string text = value?.Trim() ?? string.Empty;
        return text.Length == 8 && DateOnly.TryParseExact(
            text,
            "yyyyMMdd",
            CultureInfo.InvariantCulture,
            DateTimeStyles.None,
            out var date)
            ? date
            : null;
    }

    private static string FormatSize(long bytes)
    {
        const double kilobyte = 1024;
        const double megabyte = kilobyte * 1024;
        const double gigabyte = megabyte * 1024;
        return bytes >= gigabyte
            ? $"{bytes / gigabyte:0.##} GB"
            : bytes >= megabyte
                ? $"{bytes / megabyte:0.#} MB"
                : bytes >= kilobyte
                    ? $"{bytes / kilobyte:0} KB"
                    : $"{Math.Max(1, bytes)} B";
    }

    private static int GetStableHash(string text)
    {
        unchecked
        {
            int hash = 17;
            foreach (char character in text)
            {
                hash = (hash * 31) + character;
            }

            return hash & int.MaxValue;
        }
    }

    private void OnPropertyChanged([CallerMemberName] string? propertyName = null)
        => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propertyName));
}

/// <summary>
/// Raw values read from one uninstall registry key. Keeping this separate from the
/// scanner makes entry filtering testable without touching the real registry.
/// </summary>
public sealed record UninstallRegistryEntry(
    string Id,
    string HiveLabel,
    string RegistryPath,
    string? DisplayName,
    string? DisplayVersion,
    string? Publisher,
    string? UninstallString,
    string? InstallLocation,
    string? InstallDate,
    long? EstimatedSizeBytes,
    bool IsSystemComponent,
    bool NoRemove,
    string? ReleaseType,
    string? ParentKeyName,
    string? DisplayIcon = null,
    RegistryHive Hive = RegistryHive.CurrentUser,
    RegistryView View = RegistryView.Default,
    string? UrlInfoAbout = null);
