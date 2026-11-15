using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Xml;
using System.Xml.Linq;
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

    /// <summary><c>Get-AppxPackage</c> 的默认超时；正常机器上不到 1 秒。</summary>
    private const int ScanTimeoutMilliseconds = 60_000;

    /// <summary>单个包查询的超时；卸载核对每条记录最多跑一次。</summary>
    private const int QueryTimeoutMilliseconds = 20_000;

    private static readonly StringComparer NameComparer =
        StringComparer.Create(CultureInfo.GetCultureInfo("zh-CN"), true);

    private static readonly UTF8Encoding Utf8NoBom = new(encoderShouldEmitUTF8Identifier: false);

    /// <summary>PackageFullName 只允许包标识里的字符，避免把任意文本拼进 PowerShell 脚本。</summary>
    private static readonly Regex SafePackageFullName =
        new(@"^[A-Za-z0-9][A-Za-z0-9._-]{0,511}$", RegexOptions.CultureInvariant);

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
                    AddApplication(element, applications);
                }

                break;

            case JsonValueKind.Object:
                // ConvertTo-Json 在只有一个包时会输出单个对象而不是数组，两种形状都要解析。
                packageCount = 1;
                AddApplication(document.RootElement, applications);
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

    private static void AddApplication(JsonElement package, List<InstalledApplication> applications)
    {
        try
        {
            InstalledApplication? application = CreateApplication(package);
            if (application is not null)
            {
                applications.Add(application);
            }
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
            or System.Security.SecurityException or ArgumentException or NotSupportedException
            or XmlException or InvalidOperationException)
        {
            // A broken package must not hide the rest of the Store app list.
            Debug.WriteLine(exception);
        }
    }

    private static InstalledApplication? CreateApplication(JsonElement package)
    {
        string packageName = ReadString(package, "Name");
        string fullName = ReadString(package, "PackageFullName");
        string familyName = ReadString(package, "PackageFamilyName");
        if (packageName.Length == 0 || fullName.Length == 0)
        {
            return null;
        }

        if (ReadBoolean(package, "IsFramework") || ReadBoolean(package, "IsResourcePackage")
            || ReadBoolean(package, "IsBundle") || ReadBoolean(package, "IsStub"))
        {
            return null;
        }

        string installPath = ReadString(package, "InstallLocation");
        if (installPath.Length == 0 ||
            !installPath.Contains(WindowsAppsSegment, StringComparison.OrdinalIgnoreCase))
        {
            return null;
        }

        if (!TryReadManifest(installPath, out ManifestMetadata manifest))
        {
            return null;
        }

        string name = IsUsableDisplayText(manifest.DisplayName) ? manifest.DisplayName!.Trim() : packageName;
        string publisher = IsUsableDisplayText(manifest.PublisherDisplayName)
            ? manifest.PublisherDisplayName!.Trim()
            : ReadString(package, "Publisher");

        string uninstallCommand;
        try
        {
            uninstallCommand = StoreApplicationUninstallCommand.Build(packageName);
        }
        catch (ArgumentException exception)
        {
            Debug.WriteLine(exception);
            return null;
        }

        return new InstalledApplication
        {
            Id = "store:" + fullName,
            Name = name,
            Version = ReadString(package, "Version"),
            Publisher = publisher,
            SourceText = StoreSource,
            Category = ApplicationCategory.StoreApplication,
            UninstallString = uninstallCommand,
            DisplayIcon = ResolveLogoPath(installPath, manifest.Logo),
            InstallLocation = installPath,
            InstallDate = ReadInstallDate(installPath),
            PackageFullName = fullName,
            PackageName = packageName,
            PackageFamilyName = familyName
        };
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

    /// <summary>AppxManifest.xml 里卸载列表需要的元数据。</summary>
    /// <remarks>
    /// 读不到 manifest（文件缺失、被 ACL 拒绝、XML 损坏）时保留该包并使用空元数据，
    /// 名称与发布者回退到 <c>Get-AppxPackage</c> 的身份字段；没有
    /// <c>Applications/Application</c> 的包（框架、资源辅助包）返回 <c>false</c> 直接过滤。
    /// </remarks>
    private static bool TryReadManifest(string installPath, out ManifestMetadata manifest)
    {
        manifest = default;
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

        List<XElement> applicationEntries = FindChild(root, "Applications")
            ?.Elements()
            .Where(element => element.Name.LocalName == "Application")
            .ToList() ?? new List<XElement>();
        if (applicationEntries.Count == 0)
        {
            return false;
        }

        XElement? properties = FindChild(root, "Properties");
        string displayName = FindChild(properties, "DisplayName")?.Value.Trim() ?? string.Empty;
        string publisher = FindChild(properties, "PublisherDisplayName")?.Value.Trim() ?? string.Empty;
        string logo = FindChild(properties, "Logo")?.Value.Trim() ?? string.Empty;
        if (logo.Length == 0)
        {
            // 少数 manifest 只在 Application/VisualElements 上声明图标。
            foreach (XElement entry in applicationEntries)
            {
                XElement? visualElements = FindChild(entry, "VisualElements");
                logo = GetAttributeValue(visualElements, "Square44x44Logo");
                if (logo.Length == 0)
                {
                    logo = GetAttributeValue(visualElements, "Logo");
                }

                if (logo.Length > 0)
                {
                    break;
                }
            }
        }

        manifest = new ManifestMetadata(displayName, publisher, logo);
        return true;
    }

    /// <summary>按 LocalName 找子元素，兼容不同版本的 Appx manifest 命名空间（uap/uap10/uap5…）。</summary>
    private static XElement? FindChild(XElement? parent, string localName)
        => parent?.Elements().FirstOrDefault(element => element.Name.LocalName == localName);

    private static string GetAttributeValue(XElement? element, string attributeName)
        => element?.Attributes()
               .FirstOrDefault(attribute => attribute.Name.LocalName == attributeName)
               ?.Value.Trim() ?? string.Empty;

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

            string best = ResolveBestLogo(candidate);
            return File.Exists(best) ? best : string.Empty;
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
    private static string ResolveBestLogo(string logoPath)
    {
        string? directory = Path.GetDirectoryName(logoPath);
        if (directory is null)
        {
            return logoPath;
        }

        string stem = ScaleSuffix.Replace(Path.GetFileNameWithoutExtension(logoPath), string.Empty);
        foreach (int size in LogoSizeSuffixes)
        {
            string plain = Path.Combine(directory, $"{stem}.targetsize-{size}.png");
            if (File.Exists(plain))
            {
                return plain;
            }

            string unplatedVariant = Path.Combine(directory, $"{stem}.targetsize-{size}_altform-unplated.png");
            if (File.Exists(unplatedVariant))
            {
                return unplatedVariant;
            }
        }

        foreach (int scale in LogoScaleSuffixes)
        {
            string scaled = Path.Combine(directory, $"{stem}.scale-{scale}.png");
            if (File.Exists(scaled))
            {
                return scaled;
            }
        }

        string unplated = Path.Combine(directory, $"{stem}_altform-unplated.png");
        return File.Exists(unplated) ? unplated : logoPath;
    }

    /// <summary>安装日期找不到包安装时间时退回包目录的创建时间；两者都拿不到就留空。</summary>
    private static DateOnly? ReadInstallDate(string installPath)
    {
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
        $items = @(
            foreach ($package in $packages) {
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

    private readonly record struct ManifestMetadata(
        string? DisplayName,
        string? PublisherDisplayName,
        string? Logo);

    private readonly record struct PowerShellResult(string StandardOutput, string StandardError, int ExitCode)
    {
        public bool Success => ExitCode == 0;
    }
}
