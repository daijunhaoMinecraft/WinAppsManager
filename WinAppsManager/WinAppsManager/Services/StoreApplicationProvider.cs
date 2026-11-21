using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Xml;
using System.Xml.Linq;
using Microsoft.Win32;
using WinAppsManager.Models;

namespace WinAppsManager.Services;

/// <summary>
/// 枚举当前用户已注册的 Microsoft Store / MSIX 应用。
/// <para>
/// 单一 net6 构建没有 WinRT <c>PackageManager</c>，这里改为调用 Windows 自带的
/// Windows PowerShell（<c>powershell.exe</c>）执行 <c>Get-AppxPackage</c>，解析其 JSON 输出和
/// 每个包目录里的 <c>AppxManifest.xml</c>。这样同一份构建可以在 Windows 7–11 上运行：
/// Windows 10 / 11 上商店应用自动可用，Windows 7 / 8 / 8.1 上 <see cref="IsSupported"/>
/// 返回 <c>false</c>，界面只显示桌面软件并提示“当前系统不支持商店应用”。
/// </para>
/// <para>
/// 只保留安装位置在 <c>\WindowsApps\</c> 下、manifest 声明了
/// <c>Applications/Application</c> 的包；framework、resource、bundle 与 stub 包不是用户可卸载的
/// 应用，直接过滤。命令失败只记录日志并返回空集合，绝不把异常抛给界面。
/// </para>
/// </summary>
public static class StoreApplicationProvider
{
    private const string StoreSource = "商店应用";
    private const string WindowsAppsSegment = @"\WindowsApps\";
    private const string PackageFullNamePlaceholder = "__PACKAGE_FULL_NAME__";
    private const string PackageFamiliesPlaceholder = "__PACKAGE_FAMILIES__";

    /// <summary><c>Get-AppxPackage</c> 的默认超时；正常机器上不到 1 秒。</summary>
    private const int ScanTimeoutMilliseconds = 60_000;

    /// <summary>单个包查询的超时；卸载核对每条记录最多跑一次。</summary>
    private const int QueryTimeoutMilliseconds = 20_000;

    /// <summary><c>Get-StartApps</c> 的超时；自启动页解析商店包名称用。</summary>
    private const int SummaryTimeoutMilliseconds = 20_000;

    private static readonly StringComparer NameComparer =
        StringComparer.Create(CultureInfo.GetCultureInfo("zh-CN"), true);

    private static readonly UTF8Encoding Utf8NoBom = new(encoderShouldEmitUTF8Identifier: false);

    /// <summary>PackageFullName 只允许包标识里的字符，避免把任意文本拼进 PowerShell 脚本。</summary>
    private static readonly Regex SafePackageFullName =
        new(@"^[A-Za-z0-9][A-Za-z0-9._-]{0,511}$", RegexOptions.CultureInvariant);

    /// <summary>PackageFamilyName 形如 <c>OpenAI.Codex_2p2nqsd0c76g0</c>（名称 + 13 位发布者哈希）。</summary>
    private static readonly Regex SafePackageFamilyName =
        new(@"^[A-Za-z0-9][A-Za-z0-9.-]{0,127}_[a-z0-9]{13}$", RegexOptions.CultureInvariant);

    /// <summary>包族名 → 摘要的进程内缓存，见 <see cref="ResolvePackageSummaries"/>。</summary>
    private static readonly object SummaryCacheSync = new();
    private static readonly Dictionary<string, StorePackageSummary> SummaryCache =
        new(StringComparer.OrdinalIgnoreCase);

    private static readonly Regex ScaleSuffix = new(
        @"\.(?:scale-\d+|targetsize-\d+|contrast-[a-zA-Z]+)$",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);

    private static readonly int[] LogoSizeSuffixes = { 48, 44, 64, 32, 24, 256, 150, 100 };
    private static readonly int[] LogoScaleSuffixes = { 200, 150, 125, 400, 100 };

    /// <summary>
    /// <c>true</c>：当前系统可以用 <c>Get-AppxPackage</c> 枚举 MSIX / 商店应用。
    /// 编译期不需要 WinRT：Windows 10 / 11 的 Windows PowerShell 自带 Appx 模块。
    /// Windows 7 / 8 / 8.1 上返回 <c>false</c>，由界面提示“当前系统不支持商店应用”。
    /// </summary>
    public static bool IsSupported => OperatingSystem.IsWindowsVersionAtLeast(10);

    /// <summary>不支持时的原因说明；支持时为空字符串。</summary>
    public static string UnsupportedReason => IsSupported
        ? string.Empty
        : "当前系统不支持商店应用（需要 Windows 10 及以上）";

    public static Task<IReadOnlyList<InstalledApplication>> GetInstalledStoreApplicationsAsync(
        CancellationToken cancellationToken = default)
        => Task.Run<IReadOnlyList<InstalledApplication>>(() => Scan(cancellationToken), cancellationToken);

    public static IReadOnlyList<InstalledApplication> Scan(CancellationToken cancellationToken = default)
    {
        if (!IsSupported)
        {
            return Array.Empty<InstalledApplication>();
        }

        cancellationToken.ThrowIfCancellationRequested();
        try
        {
            PowerShellResult result = RunPowerShell(ScanScript, ScanTimeoutMilliseconds, cancellationToken);
            if (!result.Success)
            {
                AppLogService.Warn(
                    $"商店应用扫描失败：Get-AppxPackage 返回退出码 {result.ExitCode}" +
                    $"（{TrimForLog(result.StandardError)}），列表只显示桌面软件。");
                return Array.Empty<InstalledApplication>();
            }

            return ParseScanOutput(result.StandardOutput, cancellationToken);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception exception)
        {
            // 商店应用只是卸载列表的补充：任何意外都记日志并返回空，不影响桌面软件列表。
            AppLogService.Error(
                $"商店应用扫描失败：{exception.GetType().Name}：{exception.Message}，列表只显示桌面软件。",
                exception);
            return Array.Empty<InstalledApplication>();
        }
    }

    /// <summary>
    /// 用 <c>Get-AppxPackage</c> 查询某个包全名是否仍在当前用户下注册，供卸载核对使用
    /// （单一 net6 构建没有 WinRT <c>PackageManager</c>）。
    /// </summary>
    /// <returns>
    /// <c>true</c> 仍安装、<c>false</c> 已移除、<c>null</c> 当前系统不支持商店应用或查询失败（无法判定）。
    /// </returns>
    public static bool? IsPackageInstalled(string? packageFullName)
    {
        string fullName = packageFullName?.Trim() ?? string.Empty;
        if (fullName.Length == 0 || !IsSupported)
        {
            return null;
        }

        if (!SafePackageFullName.IsMatch(fullName))
        {
            AppLogService.Warn("核对商店应用是否仍安装失败：包全名包含无效字符，结果按无法判定处理。");
            return null;
        }

        string script = QueryScript.Replace(
            PackageFullNamePlaceholder,
            fullName.Replace("'", "''", StringComparison.Ordinal),
            StringComparison.Ordinal);
        try
        {
            PowerShellResult result = RunPowerShell(script, QueryTimeoutMilliseconds, CancellationToken.None);
            if (!result.Success)
            {
                AppLogService.Warn(
                    $"核对商店应用是否仍安装失败：{fullName}，Get-AppxPackage 返回退出码 {result.ExitCode}" +
                    $"（{TrimForLog(result.StandardError)}），结果按无法判定处理。");
                return null;
            }

            string output = result.StandardOutput.Trim();
            if (output.Equals("true", StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }

            if (output.Equals("false", StringComparison.OrdinalIgnoreCase))
            {
                return false;
            }

            AppLogService.Warn(
                $"核对商店应用是否仍安装失败：{fullName}，Get-AppxPackage 输出无法识别" +
                $"（{TrimForLog(output)}），结果按无法判定处理。");
            return null;
        }
        catch (Exception exception)
        {
            AppLogService.Warn(
                $"核对商店应用是否仍安装失败：{fullName}，{exception.GetType().Name}：{exception.Message}，" +
                "结果按无法判定处理。",
                exception);
            return null;
        }
    }

    /// <summary>
    /// “自启动项”页里一个 UWP 启动任务所属包的展示信息：本地化名称、发布者、图标文件与安装目录。
    /// <see cref="LogoPath"/> / <see cref="InstallLocation"/> 为空表示读不到包目录（例如包被移到了别的盘）。
    /// </summary>
    public readonly record struct StorePackageSummary(
        string PackageFamilyName,
        string DisplayName,
        string Publisher,
        string LogoPath,
        string InstallLocation);

    /// <summary>
    /// 按包族名解析商店应用的展示信息，供“自启动项”页的 UWP 启动任务使用。
    /// <para>
    /// UWP 启动任务只给出包族名与任务 Id，直接显示任务 Id（<c>App</c>、<c>TeamsTfwStartupTask</c>）
    /// 用户根本认不出是哪个软件。这里沿用卸载页的同一套来源：开始菜单的本地化入口名
    /// （<c>Get-StartApps</c>，一次调用覆盖全部请求的包）+ <c>AppxManifest.xml</c> 的发布者与 Logo
    /// （含 <c>.targetsize-*</c> / <c>.scale-*</c> 变体）。<c>ms-resource:</c> 这类需要 PRI 资源索引的
    /// 名称会被跳过并回退到开始菜单名或包名。
    /// </para>
    /// <para>
    /// 结果按包族名缓存在进程内：商店包在一次运行期间几乎不会变，而自启动页每次刷新都要问一遍，
    /// 不缓存每次都要等一次 PowerShell。
    /// </para>
    /// </summary>
    public static IReadOnlyDictionary<string, StorePackageSummary> ResolvePackageSummaries(
        IEnumerable<string> packageFamilyNames,
        CancellationToken cancellationToken = default)
    {
        var result = new Dictionary<string, StorePackageSummary>(StringComparer.OrdinalIgnoreCase);
        string[] requested = packageFamilyNames
            .Where(name => !string.IsNullOrWhiteSpace(name))
            .Select(name => name.Trim())
            .Where(name => SafePackageFamilyName.IsMatch(name))
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .ToArray();
        if (requested.Length == 0 || !IsSupported)
        {
            return result;
        }

        string[] missing = Array.Empty<string>();
        lock (SummaryCacheSync)
        {
            var pending = new List<string>(requested.Length);
            foreach (string familyName in requested)
            {
                if (SummaryCache.TryGetValue(familyName, out StorePackageSummary cached))
                {
                    result[familyName] = cached;
                }
                else
                {
                    pending.Add(familyName);
                }
            }

            missing = pending.ToArray();
        }

        if (missing.Length == 0)
        {
            return result;
        }

        try
        {
            (Dictionary<string, string> byAumid, Dictionary<string, string> byFamily) =
                ReadStartMenuNames(missing, cancellationToken);
            foreach (string familyName in missing)
            {
                StorePackageSummary summary = ResolvePackageSummary(familyName, byAumid, byFamily);
                lock (SummaryCacheSync)
                {
                    SummaryCache[familyName] = summary;
                }

                result[familyName] = summary;
            }
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
                                              or System.Security.SecurityException or InvalidOperationException
                                              or JsonException or ArgumentException)
        {
            AppLogService.Warn(
                $"解析商店应用信息失败：{exception.GetType().Name}：{exception.Message}。",
                exception);
        }

        return result;
    }

    /// <summary>
    /// 取开始菜单的本地化入口名，返回 “包族名!入口 Id → 名称” 与 “包族名 → 第一个入口名称” 两张表。
    /// 失败（没有 Appx 模块、超时、JSON 损坏）只记日志，调用方回退到清单里的名字。
    /// </summary>
    private static (Dictionary<string, string> ByAumid, Dictionary<string, string> ByFamily)
        ReadStartMenuNames(string[] familyNames, CancellationToken cancellationToken)
    {
        var byAumid = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        var byFamily = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        string familyList = string.Join(", ", familyNames.Select(name => "'" + name + "'"));
        string script = SummaryScript.Replace(
            PackageFamiliesPlaceholder,
            familyList,
            StringComparison.Ordinal);

        PowerShellResult result = RunPowerShell(script, SummaryTimeoutMilliseconds, cancellationToken);
        if (!result.Success)
        {
            AppLogService.Warn(
                $"读取商店应用开始菜单名称失败：Get-StartApps 返回退出码 {result.ExitCode}" +
                $"（{TrimForLog(result.StandardError)}）。");
            return (byAumid, byFamily);
        }

        string json = result.StandardOutput.TrimStart('\uFEFF', '\r', '\n', ' ', '\t');
        if (json.Length == 0)
        {
            return (byAumid, byFamily);
        }

        try
        {
            using JsonDocument document = JsonDocument.Parse(json);
            JsonElement root = document.RootElement;
            if (root.ValueKind == JsonValueKind.Array)
            {
                foreach (JsonElement entry in root.EnumerateArray())
                {
                    AddEntry(entry);
                }
            }
            else if (root.ValueKind == JsonValueKind.Object)
            {
                AddEntry(root);
            }
        }
        catch (JsonException exception)
        {
            AppLogService.Warn($"读取商店应用开始菜单名称失败：JSON 无法解析（{exception.Message}）。", exception);
        }

        return (byAumid, byFamily);

        void AddEntry(JsonElement entry)
        {
            string family = ReadString(entry, "Family");
            string appId = ReadString(entry, "AppId");
            string name = ReadString(entry, "Name");
            if (family.Length == 0 || !IsUsableDisplayText(name))
            {
                return;
            }

            if (appId.Length > 0)
            {
                byAumid[family + "!" + appId] = name;
            }

            if (!byFamily.ContainsKey(family))
            {
                byFamily[family] = name;
            }
        }
    }

    private static StorePackageSummary ResolvePackageSummary(
        string familyName,
        IReadOnlyDictionary<string, string> startMenuNamesByAumid,
        IReadOnlyDictionary<string, string> startMenuNamesByFamily)
    {
        int separator = familyName.IndexOf('_', StringComparison.Ordinal);
        string packageName = separator > 0 ? familyName[..separator] : familyName;

        // 同一个族可能有主包和拆分包，逐个试到能读到 AppxManifest.xml 的那一个。
        string installPath = string.Empty;
        string fullName = string.Empty;
        foreach (string candidate in ReadPackageFullNames(familyName))
        {
            fullName = candidate;
            installPath = FindInstallPath(candidate);
            if (installPath.Length > 0)
            {
                break;
            }
        }

        string displayName = string.Empty;
        string publisher = string.Empty;
        string logoPath = string.Empty;

        if (installPath.Length > 0 && TryReadManifest(installPath, packageName, out PackageManifest manifest))
        {
            ManifestApplication? application = manifest.Applications.Count > 0 ? manifest.Applications[0] : null;

            // 名称优先级：开始菜单里该入口的本地化名称 → 该包任何一个开始菜单入口 → 清单名称。
            // 多入口包（Teams 的 MSTeamsRemoteModuleContainer 排在 MSTeams 前面）第一个入口往往不是
            // 用户认识的那个名字，所以整族的开始菜单名要排在清单里的应用名之前。
            if (application is { Id.Length: > 0 } first && startMenuNamesByAumid.TryGetValue(familyName + "!" + first.Id, out string? menuName)
                && IsUsableDisplayText(menuName))
            {
                displayName = menuName.Trim();
            }

            if (displayName.Length == 0
                && startMenuNamesByFamily.TryGetValue(familyName, out string? familyMenuName)
                && IsUsableDisplayText(familyMenuName))
            {
                displayName = familyMenuName.Trim();
            }

            if (displayName.Length == 0 && application is { } app && IsUsableDisplayText(app.DisplayName))
            {
                displayName = app.DisplayName!.Trim();
            }

            if (displayName.Length == 0 && IsUsableDisplayText(manifest.DisplayName))
            {
                displayName = manifest.DisplayName!.Trim();
            }

            if (IsUsableDisplayText(manifest.PublisherDisplayName))
            {
                publisher = manifest.PublisherDisplayName!.Trim();
            }

            logoPath = ResolveLogoPath(
                installPath,
                application is { } withLogo
                    ? FirstNonEmpty(withLogo.Logo, manifest.PackageLogo)
                    : manifest.PackageLogo);
        }
        else if (startMenuNamesByFamily.TryGetValue(familyName, out string? fallbackMenuName)
                 && IsUsableDisplayText(fallbackMenuName))
        {
            displayName = fallbackMenuName.Trim();
        }

        if (displayName.Length == 0)
        {
            displayName = packageName;
        }

        if (logoPath.Length == 0 && installPath.Length == 0)
        {
            AppLogService.Warn(
                $"找不到商店应用的包目录，名称与图标只能回退（应用可能不在系统盘或包目录不可读）：{familyName}");
        }

        return new StorePackageSummary(familyName, displayName, publisher, logoPath, installPath);
    }

    /// <summary>
    /// 包族名 → 当前用户下的全部包全名，主包优先、版本高的优先。
    /// 同一族可能同时注册主包与资源/语言/缩放拆分包（<c>…_neutral_split.scale-400_…</c>），
    /// 拆分包没有自己的 <c>AppxManifest.xml</c>，不能拿来解析名称和图标。
    /// </summary>
    private static IReadOnlyList<string> ReadPackageFullNames(string familyName)
    {
        var mains = new List<(string Name, Version Version)>(2);
        var splits = new List<string>(4);
        try
        {
            RegistryView view = Environment.Is64BitOperatingSystem ? RegistryView.Registry64 : RegistryView.Default;
            using RegistryKey baseKey = RegistryKey.OpenBaseKey(RegistryHive.ClassesRoot, view);
            using RegistryKey? families = baseKey.OpenSubKey(
                $@"Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\Repository\Families\{familyName}");
            if (families is null)
            {
                return Array.Empty<string>();
            }

            foreach (string candidate in families.GetSubKeyNames())
            {
                // 拆分包的段名是 <arch>_split.<qualifier>，所以标记是 "_split." 而不是 ".split."。
                if (candidate.Contains("_split.", StringComparison.OrdinalIgnoreCase)
                    || candidate.Contains(".split.", StringComparison.OrdinalIgnoreCase))
                {
                    splits.Add(candidate);
                    continue;
                }

                mains.Add((candidate, ReadPackageVersion(candidate)));
            }
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
                                              or System.Security.SecurityException or ArgumentException)
        {
            Debug.WriteLine(exception);
            return Array.Empty<string>();
        }

        return mains
            .OrderByDescending(entry => entry.Version)
            .Select(entry => entry.Name)
            .Concat(splits)
            .ToArray();
    }

    private static Version ReadPackageVersion(string packageFullName)
    {
        string[] parts = packageFullName.Split('_');
        return parts.Length > 1 && Version.TryParse(parts[1], out Version? version)
            ? version
            : new Version(0, 0);
    }

    /// <summary>
    /// 包目录：<c>%ProgramFiles%\WindowsApps\&lt;包全名&gt;</c>。没有单独的注册表项记录安装盘，
    /// “新应用保存到别的盘”时这里会读不到，调用方回退到包名与字母徽章。
    /// </summary>
    private static string FindInstallPath(string packageFullName)
    {
        try
        {
            string programFiles = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
            if (programFiles.Length == 0)
            {
                return string.Empty;
            }

            string path = Path.Combine(programFiles, "WindowsApps", packageFullName);
            return File.Exists(Path.Combine(path, "AppxManifest.xml")) ? path : string.Empty;
        }
        catch (Exception exception) when (exception is ArgumentException or IOException
                                              or UnauthorizedAccessException or NotSupportedException)
        {
            Debug.WriteLine(exception);
            return string.Empty;
        }
    }

    private static IReadOnlyList<InstalledApplication> ParseScanOutput(
        string json,
        CancellationToken cancellationToken)
    {
        string text = json.TrimStart('\uFEFF', '\r', '\n', ' ', '\t');
        if (text.Length == 0)
        {
            AppLogService.Warn("商店应用扫描失败：Get-AppxPackage 没有输出 JSON，列表只显示桌面软件。");
            return Array.Empty<InstalledApplication>();
        }

        using var document = JsonDocument.Parse(text);
        var applications = new List<InstalledApplication>();
        int packageCount;
        switch (document.RootElement.ValueKind)
        {
            case JsonValueKind.Array:
                packageCount = 0;
                foreach (JsonElement element in document.RootElement.EnumerateArray())
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    packageCount++;
                    AddApplications(element, applications);
                }

                break;

            case JsonValueKind.Object:
                // ConvertTo-Json 在只有一个包时会输出单个对象而不是数组，两种形状都要解析。
                packageCount = 1;
                AddApplications(document.RootElement, applications);
                break;

            default:
                AppLogService.Warn(
                    $"商店应用扫描失败：Get-AppxPackage 返回了非预期的 JSON 根节点 " +
                    $"（{document.RootElement.ValueKind}），列表只显示桌面软件。");
                return Array.Empty<InstalledApplication>();
        }

        applications.Sort((left, right) =>
        {
            int byName = NameComparer.Compare(left.Name, right.Name);
            return byName != 0 ? byName : StringComparer.Ordinal.Compare(left.Id, right.Id);
        });
        AppLogService.Info(
            $"商店应用扫描完成：Get-AppxPackage 返回 {packageCount} 个包，过滤后保留 {applications.Count} 个可卸载应用。");
        return applications;
    }

    private static void AddApplications(JsonElement package, List<InstalledApplication> applications)
    {
        try
        {
            applications.AddRange(CreateApplications(package));
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
            or System.Security.SecurityException or ArgumentException or NotSupportedException
            or XmlException or InvalidOperationException)
        {
            // A broken package must not hide the rest of the Store app list.
            Debug.WriteLine(exception);
        }
    }

    private static IReadOnlyList<InstalledApplication> CreateApplications(JsonElement package)
    {
        string packageName = ReadString(package, "Name");
        string fullName = ReadString(package, "PackageFullName");
        string familyName = ReadString(package, "PackageFamilyName");
        if (packageName.Length == 0 || fullName.Length == 0)
        {
            return Array.Empty<InstalledApplication>();
        }

        if (ReadBoolean(package, "IsFramework") || ReadBoolean(package, "IsResourcePackage")
            || ReadBoolean(package, "IsBundle") || ReadBoolean(package, "IsStub"))
        {
            return Array.Empty<InstalledApplication>();
        }

        string installPath = ReadString(package, "InstallLocation");
        if (installPath.Length == 0 ||
            !installPath.Contains(WindowsAppsSegment, StringComparison.OrdinalIgnoreCase))
        {
            return Array.Empty<InstalledApplication>();
        }

        if (!TryReadManifest(installPath, packageName, out PackageManifest manifest))
        {
            return Array.Empty<InstalledApplication>();
        }

        string publisher = IsUsableDisplayText(manifest.PublisherDisplayName)
            ? manifest.PublisherDisplayName!.Trim()
            : ReadString(package, "Publisher");

        string uninstallCommand;
        try
        {
            uninstallCommand = StoreApplicationUninstallCommand.Build(packageName, fullName);
        }
        catch (ArgumentException exception)
        {
            Debug.WriteLine(exception);
            return Array.Empty<InstalledApplication>();
        }

        string version = ReadString(package, "Version");
        DateOnly? installDate = ReadInstallDate(familyName, fullName, installPath);
        RelatedPackageApplication[] packageApplications = manifest.Applications.Select(entry =>
        {
            string startMenuName = ReadStartMenuName(package, entry.Id);
            if (IsUsableDisplayText(startMenuName))
            {
                return new RelatedPackageApplication(entry.Id, startMenuName);
            }

            if (IsUsableDisplayText(entry.DisplayName))
            {
                return new RelatedPackageApplication(entry.Id, entry.DisplayName!.Trim());
            }

            if (manifest.Applications.Count == 1 && IsUsableDisplayText(manifest.DisplayName))
            {
                return new RelatedPackageApplication(entry.Id, manifest.DisplayName!.Trim());
            }

            return new RelatedPackageApplication(
                entry.Id,
                manifest.Applications.Count == 1 ? packageName : $"{packageName} ({entry.Id})");
        }).ToArray();
        var results = new List<InstalledApplication>(manifest.Applications.Count);
        for (int index = 0; index < manifest.Applications.Count; index++)
        {
            ManifestApplication entry = manifest.Applications[index];
            string name = packageApplications[index].Name;
            results.Add(new InstalledApplication
            {
                Id = "store:" + fullName + "!" + entry.Id,
                Name = name,
                Version = version,
                Publisher = publisher,
                SourceText = StoreSource,
                Category = ApplicationCategory.StoreApplication,
                UninstallString = uninstallCommand,
                DisplayIcon = ResolveLogoPath(installPath, FirstNonEmpty(entry.Logo, manifest.PackageLogo)),
                InstallLocation = installPath,
                InstallDate = installDate,
                PackageFullName = fullName,
                PackageName = packageName,
                PackageFamilyName = familyName,
                PackageApplicationId = entry.Id,
                RelatedPackageApplications = packageApplications
                    .Where((_, relatedIndex) => relatedIndex != index)
                    .ToArray()
            });
        }

        return results;
    }

    private static bool IsUsableDisplayText(string? value)
    {
        string text = value?.Trim() ?? string.Empty;
        return text.Length > 0
               && !text.StartsWith("ms-resource:", StringComparison.OrdinalIgnoreCase)
               && !text.StartsWith("@{", StringComparison.Ordinal);
    }

    private static string ReadString(JsonElement element, string propertyName)
    {
        if (element.ValueKind != JsonValueKind.Object ||
            !element.TryGetProperty(propertyName, out JsonElement value))
        {
            return string.Empty;
        }

        return value.ValueKind switch
        {
            JsonValueKind.String => value.GetString()?.Trim() ?? string.Empty,
            JsonValueKind.Number => value.GetRawText(),
            JsonValueKind.True => bool.TrueString,
            JsonValueKind.False => bool.FalseString,
            _ => string.Empty
        };
    }

    private static string ReadStartMenuName(JsonElement package, string applicationId)
    {
        if (!package.TryGetProperty("StartMenuEntries", out JsonElement entries))
        {
            return string.Empty;
        }

        IEnumerable<JsonElement> candidates = entries.ValueKind switch
        {
            JsonValueKind.Array => entries.EnumerateArray().ToArray(),
            JsonValueKind.Object => new[] { entries },
            _ => Array.Empty<JsonElement>()
        };
        foreach (JsonElement entry in candidates)
        {
            if (ReadString(entry, "ApplicationId").Equals(applicationId, StringComparison.OrdinalIgnoreCase))
            {
                string name = ReadString(entry, "Name");
                if (IsUsableDisplayText(name))
                {
                    return name;
                }
            }
        }

        return string.Empty;
    }

    private static bool ReadBoolean(JsonElement element, string propertyName)
    {
        if (element.ValueKind != JsonValueKind.Object ||
            !element.TryGetProperty(propertyName, out JsonElement value))
        {
            return false;
        }

        return value.ValueKind switch
        {
            JsonValueKind.True => true,
            JsonValueKind.String => bool.TryParse(value.GetString(), out bool parsed) && parsed,
            _ => false
        };
    }

    /// <summary>Package-level and per-application metadata from AppxManifest.xml.</summary>
    /// <remarks>
    /// 读不到 manifest（文件缺失、被 ACL 拒绝、XML 损坏）时保留该包并使用空元数据，
    /// 名称与发布者回退到 <c>Get-AppxPackage</c> 的身份字段；没有
    /// <c>Applications/Application</c> 的包（框架、资源辅助包）返回 <c>false</c> 直接过滤。
    /// </remarks>
    private static bool TryReadManifest(string installPath, string packageName, out PackageManifest manifest)
    {
        manifest = CreateFallbackManifest(packageName);
        string manifestPath;
        try
        {
            manifestPath = Path.Combine(installPath, "AppxManifest.xml");
        }
        catch (ArgumentException)
        {
            return true;
        }

        if (!File.Exists(manifestPath))
        {
            return true;
        }

        XDocument document;
        try
        {
            document = XDocument.Load(manifestPath);
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
            or System.Security.SecurityException or XmlException)
        {
            Debug.WriteLine(exception);
            return true;
        }

        XElement? root = document.Root;
        if (root is null)
        {
            return false;
        }

        List<ManifestApplication> applications = FindChild(root, "Applications")
            ?.Elements()
            .Where(element => element.Name.LocalName == "Application")
            .Select(element =>
            {
                XElement? visualElements = FindChild(element, "VisualElements");
                string logo = FirstNonEmpty(
                    GetAttributeValue(visualElements, "Square44x44Logo"),
                    GetAttributeValue(visualElements, "Square30x30Logo"),
                    GetAttributeValue(visualElements, "Square70x70Logo"),
                    GetAttributeValue(visualElements, "Square71x71Logo"),
                    GetAttributeValue(visualElements, "Square150x150Logo"),
                    GetAttributeValue(visualElements, "SmallLogo"),
                    GetAttributeValue(visualElements, "Logo"));
                return new ManifestApplication(
                    GetAttributeValue(element, "Id"),
                    GetAttributeValue(visualElements, "DisplayName"),
                    logo);
            })
            .Where(application => application.Id.Length > 0)
            .ToList() ?? new List<ManifestApplication>();
        if (applications.Count == 0)
        {
            return false;
        }

        XElement? properties = FindChild(root, "Properties");
        string displayName = FindChild(properties, "DisplayName")?.Value.Trim() ?? string.Empty;
        string publisher = FindChild(properties, "PublisherDisplayName")?.Value.Trim() ?? string.Empty;
        string packageLogo = FindChild(properties, "Logo")?.Value.Trim() ?? string.Empty;
        manifest = new PackageManifest(displayName, publisher, packageLogo, applications);
        return true;
    }

    private static PackageManifest CreateFallbackManifest(string packageName)
        => new(
            null,
            null,
            string.Empty,
            new[] { new ManifestApplication(packageName, null, string.Empty) });

    /// <summary>按 LocalName 找子元素，兼容不同版本的 Appx manifest 命名空间（uap/uap10/uap5…）。</summary>
    private static XElement? FindChild(XElement? parent, string localName)
        => parent?.Elements().FirstOrDefault(element => element.Name.LocalName == localName);

    private static string GetAttributeValue(XElement? element, string attributeName)
        => element?.Attributes()
               .FirstOrDefault(attribute => attribute.Name.LocalName == attributeName)
               ?.Value.Trim() ?? string.Empty;

    private static string FirstNonEmpty(params string[] values)
        => values.FirstOrDefault(value => value.Length > 0) ?? string.Empty;

    private static string ResolveLogoPath(string installPath, string? logo)
    {
        string relative = logo?.Trim() ?? string.Empty;
        if (relative.Length == 0)
        {
            return string.Empty;
        }

        const string appxPrefix = "ms-appx:///";
        if (relative.StartsWith(appxPrefix, StringComparison.OrdinalIgnoreCase))
        {
            relative = relative[appxPrefix.Length..];
        }

        try
        {
            relative = Uri.UnescapeDataString(relative);
            string root = Path.GetFullPath(installPath);
            string candidate = Path.GetFullPath(Path.Combine(
                root,
                relative.Replace('/', Path.DirectorySeparatorChar)));
            if (!candidate.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
            {
                return string.Empty;
            }

            foreach (string logoCandidate in ResolveLogoCandidates(candidate))
            {
                if (File.Exists(logoCandidate))
                {
                    return logoCandidate;
                }
            }

            return string.Empty;
        }
        catch (Exception exception) when (exception is ArgumentException or IOException
            or UnauthorizedAccessException or System.Security.SecurityException
            or NotSupportedException or UriFormatException)
        {
            Debug.WriteLine(exception);
            return string.Empty;
        }
    }

    /// <summary>
    /// Picks the best scaled UWP asset for the manifest logo: looks for <c>.targetsize-N.png</c> /
    /// <c>.scale-N.png</c> siblings (and <c>_altform-unplated</c>), because the manifest usually
    /// points at the 100% asset.
    /// 打包应用经常只有带缩放后缀的文件而没有同名基准文件，因此这里不要求 <paramref name="logoPath"/> 本身存在。
    /// </summary>
    private static IEnumerable<string> ResolveLogoCandidates(string logoPath)
    {
        string? directory = Path.GetDirectoryName(logoPath);
        if (directory is null)
        {
            yield return logoPath;
            yield break;
        }

        string stem = ScaleSuffix.Replace(Path.GetFileNameWithoutExtension(logoPath), string.Empty);
        foreach (int size in LogoSizeSuffixes)
        {
            string plain = Path.Combine(directory, $"{stem}.targetsize-{size}.png");
            yield return plain;

            string unplatedVariant = Path.Combine(directory, $"{stem}.targetsize-{size}_altform-unplated.png");
            yield return unplatedVariant;
        }

        foreach (int scale in LogoScaleSuffixes)
        {
            string scaled = Path.Combine(directory, $"{stem}.scale-{scale}.png");
            yield return scaled;
        }

        string unplated = Path.Combine(directory, $"{stem}_altform-unplated.png");
        yield return unplated;
        yield return logoPath;
    }

    /// <summary>安装日期找不到包安装时间时退回包目录的创建时间；两者都拿不到就留空。</summary>
    private static DateOnly? ReadInstallDate(string familyName, string fullName, string installPath)
    {
        if (familyName.Length > 0 && fullName.Length > 0)
        {
            try
            {
                RegistryView view = Environment.Is64BitOperatingSystem
                    ? RegistryView.Registry64
                    : RegistryView.Default;
                using RegistryKey baseKey = RegistryKey.OpenBaseKey(RegistryHive.ClassesRoot, view);
                using RegistryKey? key = baseKey.OpenSubKey(
                    $@"Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\Repository\Families\{familyName}\{fullName}");
                object? value = key?.GetValue("InstallTime");
                if (value is long fileTime && fileTime > 0)
                {
                    return DateOnly.FromDateTime(DateTime.FromFileTimeUtc(fileTime).ToLocalTime());
                }

                if (value is ulong unsignedFileTime && unsignedFileTime > 0 && unsignedFileTime <= long.MaxValue)
                {
                    return DateOnly.FromDateTime(DateTime.FromFileTimeUtc((long)unsignedFileTime).ToLocalTime());
                }
            }
            catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
                or System.Security.SecurityException or ArgumentException)
            {
                Debug.WriteLine(exception);
            }
        }

        try
        {
            DateTime created = Directory.GetCreationTime(installPath);
            if (created.Year is >= 1990 and <= 2200)
            {
                return DateOnly.FromDateTime(created);
            }
        }
        catch (Exception exception) when (exception is ArgumentException or IOException
            or UnauthorizedAccessException or System.Security.SecurityException)
        {
            Debug.WriteLine(exception);
        }

        return null;
    }

    private static PowerShellResult RunPowerShell(
        string script,
        int timeoutMilliseconds,
        CancellationToken cancellationToken)
    {
        string encodedCommand = Convert.ToBase64String(Encoding.Unicode.GetBytes(script));
        var startInfo = new ProcessStartInfo
        {
            FileName = ResolvePowerShellPath(),
            Arguments = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand "
                        + encodedCommand,
            CreateNoWindow = true,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Utf8NoBom,
            StandardErrorEncoding = Utf8NoBom,
            WindowStyle = ProcessWindowStyle.Hidden
        };

        using var process = new Process { StartInfo = startInfo };
        try
        {
            if (!process.Start())
            {
                return new PowerShellResult(string.Empty, "powershell.exe 未能启动。", -1);
            }
        }
        catch (Exception exception) when (exception is InvalidOperationException or Win32Exception
            or PlatformNotSupportedException or NotSupportedException)
        {
            return new PowerShellResult(string.Empty, $"{exception.GetType().Name}：{exception.Message}", -1);
        }

        // 必须先开始异步读取：Get-AppxPackage 的 JSON 会超过管道缓冲区，读完才可能退出。
        Task<string> outputTask = process.StandardOutput.ReadToEndAsync();
        Task<string> errorTask = process.StandardError.ReadToEndAsync();

        bool timedOut = false;
        using (cancellationToken.Register(static state => TerminateProcess((Process)state!), process))
        {
            if (!process.WaitForExit(timeoutMilliseconds))
            {
                timedOut = true;
                TerminateProcess(process);
                process.WaitForExit(5000);
            }
        }

        string standardOutput = ReadCompleted(outputTask);
        string standardError = ReadCompleted(errorTask);
        if (cancellationToken.IsCancellationRequested)
        {
            throw new OperationCanceledException(cancellationToken);
        }

        if (timedOut)
        {
            return new PowerShellResult(
                string.Empty,
                $"powershell.exe 执行超过 {timeoutMilliseconds} 毫秒，已终止。",
                -1);
        }

        int exitCode;
        try
        {
            exitCode = process.ExitCode;
        }
        catch (InvalidOperationException)
        {
            exitCode = -1;
        }

        return new PowerShellResult(standardOutput, standardError, exitCode);
    }

    /// <summary>
    /// 32 位进程在 64 位 Windows 上默认会启动 SysWOW64 里的 32 位 PowerShell，
    /// 通过 <c>Sysnative</c> 改为启动原生 64 位 PowerShell，保证 Appx 模块行为与系统一致。
    /// </summary>
    private static string ResolvePowerShellPath()
    {
        if (!Environment.Is64BitOperatingSystem || Environment.Is64BitProcess)
        {
            return "powershell.exe";
        }

        try
        {
            string windows = Environment.GetFolderPath(Environment.SpecialFolder.Windows);
            if (windows.Length > 0)
            {
                string sysnative = Path.Combine(
                    windows,
                    "Sysnative",
                    "WindowsPowerShell",
                    "v1.0",
                    "powershell.exe");
                if (File.Exists(sysnative))
                {
                    return sysnative;
                }
            }
        }
        catch (Exception exception) when (exception is ArgumentException or IOException
            or UnauthorizedAccessException)
        {
            Debug.WriteLine(exception);
        }

        return "powershell.exe";
    }

    private static void TerminateProcess(Process process)
    {
        try
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
            }
        }
        catch (Exception exception) when (exception is InvalidOperationException or Win32Exception
            or NotSupportedException or AggregateException)
        {
            Debug.WriteLine(exception);
        }
    }

    private static string ReadCompleted(Task<string> readTask)
    {
        try
        {
            if (!readTask.Wait(5000))
            {
                return string.Empty;
            }

            return readTask.GetAwaiter().GetResult();
        }
        catch (Exception exception) when (exception is AggregateException or InvalidOperationException
            or IOException or ObjectDisposedException)
        {
            Debug.WriteLine(exception);
            return string.Empty;
        }
    }

    private static string TrimForLog(string text, int maxLength = 500)
    {
        string trimmed = text.Trim();
        return trimmed.Length <= maxLength ? trimmed : trimmed[..maxLength] + "…";
    }

    /// <summary>
    /// 只取指定包族名的开始菜单入口名，供自启动页解析 UWP 启动任务的显示名。
    /// stdout 固定为 UTF-8；<c>__PACKAGE_FAMILIES__</c> 由调用方替换成已校验的包族名列表。
    /// </summary>
    private const string SummaryScript = """
        $ProgressPreference = 'SilentlyContinue'
        try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }
        $families = @(__PACKAGE_FAMILIES__)
        try { $startApps = @(Get-StartApps -ErrorAction Stop) } catch { $startApps = @() }
        $items = @(
            foreach ($entry in $startApps) {
                $appId = [string]$entry.AppID
                $separator = $appId.IndexOf('!')
                if ($separator -le 0) { continue }
                $family = $appId.Substring(0, $separator)
                if ($families -notcontains $family) { continue }
                [pscustomobject]@{
                    Family = $family
                    AppId  = $appId.Substring($separator + 1)
                    Name   = [string]$entry.Name
                }
            }
        )
        [Console]::Out.Write((ConvertTo-Json -InputObject $items -Depth 3 -Compress))
        """;

    /// <summary>
    /// 枚举当前用户全部包。字段与 <c>Get-AppxPackage</c> 的输出一一对应；
    /// <c>IsStub</c> 同时覆盖旧版本 Windows 上表示“未完成安装”的 <c>IsPartiallyStaged</c>。
    /// stdout 固定为 UTF-8，输出单个 JSON 数组（零个包时是空数组）。
    /// </summary>
    private const string ScanScript = """
        $ProgressPreference = 'SilentlyContinue'
        $ErrorActionPreference = 'Stop'
        try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }
        try {
            $packages = Get-AppxPackage -ErrorAction Stop
        } catch {
            [Console]::Error.WriteLine($_.Exception.Message)
            exit 1
        }
        try { $startApps = @(Get-StartApps -ErrorAction Stop) } catch { $startApps = @() }
        $startAppsByFamily = @{}
        foreach ($entry in $startApps) {
            $appId = [string]$entry.AppID
            $separator = $appId.IndexOf('!')
            if ($separator -gt 0) {
                $familyName = $appId.Substring(0, $separator)
                if (-not $startAppsByFamily.ContainsKey($familyName)) {
                    $startAppsByFamily[$familyName] = @()
                }
                $startAppsByFamily[$familyName] += [pscustomobject]@{
                    ApplicationId = $appId.Substring($separator + 1)
                    Name = [string]$entry.Name
                }
            }
        }
        $items = @(
            foreach ($package in $packages) {
                $familyName = [string]$package.PackageFamilyName
                $startMenuEntries = @($startAppsByFamily[$familyName])
                [pscustomobject]@{
                    Name              = [string]$package.Name
                    PackageFullName   = [string]$package.PackageFullName
                    PackageFamilyName = [string]$package.PackageFamilyName
                    Version           = [string]$package.Version
                    Publisher         = [string]$package.Publisher
                    InstallLocation   = [string]$package.InstallLocation
                    IsFramework       = [bool]$package.IsFramework
                    IsResourcePackage = [bool]$package.IsResourcePackage
                    IsBundle          = [bool]$package.IsBundle
                    IsStub            = [bool]($package.IsStub -or $package.IsPartiallyStaged)
                    StartMenuEntries  = $startMenuEntries
                }
            }
        )
        $json = ConvertTo-Json -InputObject $items -Depth 4 -Compress
        [Console]::Out.Write($json)
        """;

    /// <summary>按包全名查询是否仍安装：只输出 <c>true</c>/<c>false</c>，查询失败以退出码 1 结束。</summary>
    private const string QueryScript = """
        $ProgressPreference = 'SilentlyContinue'
        $ErrorActionPreference = 'Stop'
        $fullName = '__PACKAGE_FULL_NAME__'
        $name = ($fullName -split '_', 2)[0]
        try {
            $found = Get-AppxPackage -Name $name -ErrorAction Stop | Where-Object { $_.PackageFullName -ieq $fullName }
            if ($found) { [Console]::Out.Write('true') } else { [Console]::Out.Write('false') }
        } catch {
            [Console]::Error.WriteLine($_.Exception.Message)
            exit 1
        }
        """;

    private sealed record PackageManifest(
        string? DisplayName,
        string? PublisherDisplayName,
        string PackageLogo,
        IReadOnlyList<ManifestApplication> Applications);

    private readonly record struct ManifestApplication(string Id, string? DisplayName, string Logo);

    private readonly record struct PowerShellResult(string StandardOutput, string StandardError, int ExitCode)
    {
        public bool Success => ExitCode == 0;
    }
}
