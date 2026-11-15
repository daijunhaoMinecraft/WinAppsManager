using System.Collections.Concurrent;
using System.Diagnostics;
using System.Diagnostics.CodeAnalysis;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Text.Json;
using WinAppsManager.Models;

namespace WinAppsManager.Services;

/// <summary>
/// 软件目录客户端：只调用服务端统一软件源接口（<c>docs/source-api.md</c>），
/// 不包含任何软件源 ID、名称、接口路径或字段分支。
/// <para>
/// 来源列表、默认来源、名称、搜索模式与能力全部来自 <c>GET /api/v1/sources</c>；目录/分类/搜索/详情/下载
/// 统一走 <c>/api/v1/sources/{source_id}/...</c>，视频播放地址直接使用详情返回的 <c>video_proxy_url</c>。
/// 新增一个软件源只需要服务端增加适配器，客户端自动显示、切换、搜索、详情与下载。
/// </para>
/// <para>
/// 缓存按 <c>source_id</c> 隔离：切换来源会重建 <see cref="ClientCacheStore"/>（目录缓存 key 含来源 ID），
/// 内存目录、详情与已知应用表同时清空。切换服务端地址会清空来源表并按新服务端重新发现来源。
/// </para>
/// </summary>
public static class AppCatalogService
{
    /// <summary>搜索模式（服务端 <c>/sources</c> 的 <c>search_mode</c>）：目录完整，客户端本地搜索即可。</summary>
    public const string SearchModeCatalog = "catalog";

    /// <summary>搜索模式（服务端 <c>/sources</c> 的 <c>search_mode</c>）：目录只是子集，搜索必须调用服务端 API。</summary>
    public const string SearchModeApi = "api";

    private const int SearchResultLimit = 30;
    private const int MaxSourceIdLength = 64;

    /// <summary>当前后端服务地址（设置中可自定义，始终以 / 结尾）。</summary>
    public static string BackendBaseUrl
    {
        get
        {
            AppSettingsService.Initialize();
            return AppSettingsService.Current.BackendBaseUrl;
        }
    }

    /// <summary>当前后端根 Uri；请求路径都是相对它拼接，切换服务端后自动生效。</summary>
    public static Uri BackendUri => new(BackendBaseUrl);

    private static HttpClient CreateClient() => new()
    {
        // 逐请求的超时在 GetAsync 里用 CancellationTokenSource 控制：大目录首次构建可能较慢。
        // 不使用 BaseAddress：服务端地址可在运行时修改，每次请求都用绝对地址。
        Timeout = Timeout.InfiniteTimeSpan
    };

    private static readonly HttpClient Client = CreateClient();
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = BclCompat.SnakeCaseLower,
        PropertyNameCaseInsensitive = true
    };
    private static readonly TimeSpan DefaultRequestTimeout = TimeSpan.FromSeconds(12);
    private static readonly TimeSpan SourcesRequestTimeout = TimeSpan.FromSeconds(10);
    private static readonly TimeSpan CatalogRequestTimeout = TimeSpan.FromSeconds(40);
    private static readonly TimeSpan StoreRequestTimeout = TimeSpan.FromSeconds(20);
    private static readonly TimeSpan RefreshRequestTimeout = TimeSpan.FromSeconds(180);

    /// <summary>来源列表在内存中的复用窗口：窗口内不重复请求 <c>/sources</c>。</summary>
    private static readonly TimeSpan SourcesReuseWindow = TimeSpan.FromMinutes(5);

    /// <summary><see cref="_catalogServerUpdatedAtTicks"/> 的“未知”哨兵值。</summary>
    private const long UnknownCatalogTimestamp = long.MinValue;
    private static readonly string CacheRootPath = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "WinAppsManager", "Cache");

    private static readonly object InitializationGate = new();
    private static readonly object SourcesTaskGate = new();
    private static readonly SemaphoreSlim CatalogGate = new(1, 1);
    private static readonly SemaphoreSlim IconDownloadGate = new(6, 6);
    private static readonly Dictionary<string, (AppItem App, DateTimeOffset Updated)> Details = new(StringComparer.Ordinal);
    private static readonly ConcurrentDictionary<string, AppItem> KnownApps = new(StringComparer.Ordinal);

    private static bool _initialized;
    private static List<SourceInfo> _sources = new();
    private static Task<IReadOnlyList<SourceInfo>>? _sourcesTask;
    private static DateTimeOffset _sourcesLoadedAt;
    private static SourceInfo? _currentSource;
    private static string _currentSourceId = string.Empty;
    private static string? _sourceFallbackNote;
    private static ClientCacheStore? _store;
    private static IReadOnlyList<AppItem> _apps = Array.Empty<AppItem>();
    private static int _hiddenIncompatible;
    private static bool _hasLoaded;
    private static DateTimeOffset? _catalogUpdatedAt;
    // 服务端目录快照的 updated_at（UTC ticks）。普通请求可能返回几分钟/几小时前的缓存，
    // 因此它和本地加载时间 _catalogUpdatedAt 分开保存；未知时为 long.MinValue（避免 Nullable<T> 撕裂读）。
    private static long _catalogServerUpdatedAtTicks = UnknownCatalogTimestamp;
    private static string? _lastNetworkError;
    private static CatalogCacheState _cacheState = new(CatalogCacheSource.None, null);

    public static IReadOnlyList<AppItem> Apps => Volatile.Read(ref _apps);
    public static bool HasLoaded => Volatile.Read(ref _hasLoaded);
    public static CatalogCacheState CacheState => Volatile.Read(ref _cacheState);

    /// <summary>最近一次从服务端获取的软件源列表（服务端未返回前为空）。</summary>
    public static IReadOnlyList<SourceInfo> Sources => _sources;

    /// <summary>当前软件源 ID（服务端 <c>/sources</c> 的 <c>id</c>）；尚未解析时为空字符串。</summary>
    public static string CurrentSourceId
    {
        get
        {
            EnsureInitialized();
            return _currentSourceId;
        }
    }

    /// <summary>当前软件源显示名；服务端未返回名称时回退为来源 ID。</summary>
    public static string SourceDisplayName
    {
        get
        {
            EnsureInitialized();
            return _currentSource is not null
                ? DisplayNameOf(_currentSource)
                : _currentSourceId.Length > 0 ? _currentSourceId : "软件源";
        }
    }

    /// <summary>当前软件源的能力集合；尚未解析来源时为契约默认能力（catalog/details/download 可用）。</summary>
    public static SourceCapabilities CurrentCapabilities =>
        _currentSource?.CapabilitiesOrDefault ?? SourceCapabilities.Default;

    /// <summary>
    /// 服务端下发的搜索模式：<c>catalog</c>（目录完整，客户端本地搜索）或 <c>api</c>（调用服务端搜索）。
    /// 尚未解析来源时按 <c>catalog</c> 处理，避免搜索框在加载期间发起多余请求。
    /// </summary>
    public static string CurrentSearchMode
    {
        get
        {
            string? mode = _currentSource?.SearchMode?.Trim();
            return string.Equals(mode, SearchModeApi, StringComparison.OrdinalIgnoreCase)
                ? SearchModeApi
                : SearchModeCatalog;
        }
    }

    /// <summary>当前软件源是否必须调用服务端搜索 API。</summary>
    public static bool UsesApiSearch => CurrentSearchMode == SearchModeApi;

    /// <summary>来源列表解析失败/保存的来源不可用时的说明（供设置页展示），正常时为 <see langword="null"/>。</summary>
    public static string? SourceFallbackNote => _sourceFallbackNote;

    /// <summary>
    /// 最近一次目录响应里的 <c>updated_at</c>（服务端目录快照的生成时间，UTC）。
    /// 普通请求只读服务端缓存，该时间可能早于本地加载时间；尚未成功拉取过目录时为 <see langword="null"/>。
    /// </summary>
    public static DateTimeOffset? CatalogUpdatedAtUtc
    {
        get
        {
            long ticks = Volatile.Read(ref _catalogServerUpdatedAtTicks);
            return ticks == UnknownCatalogTimestamp ? null : new DateTimeOffset(ticks, TimeSpan.Zero);
        }
    }

    /// <summary>本地缓存根目录（图标/目录/详情图片都在其下）。</summary>
    public static string CacheDirectory
    {
        get
        {
            EnsureInitialized();
            return _store!.DirectoryPath;
        }
    }

    /// <summary>本地缓存统计（目录文件、图标文件、详情图片文件）。</summary>
    public static ClientCacheStatistics CacheStatistics
    {
        get
        {
            var local = Store.Statistics();
            var media = StoreMediaService.CacheStatistics();
            return new ClientCacheStatistics(local.CatalogFiles, local.IconFiles, local.Bytes + media.Bytes, media.Files);
        }
    }

    /// <summary>上次发布目录时因为与当前系统架构/版本不兼容而被隐藏的软件数量。</summary>
    public static int HiddenIncompatibleCount => Volatile.Read(ref _hiddenIncompatible);

    /// <summary>Resolves an icon that the cache could download but not persist to disk.</summary>
    public static bool TryGetMemoryIcon(string? source, [NotNullWhen(true)] out byte[]? bytes) =>
        Store.TryGetMemoryIcon(source, out bytes);

    public static string CacheDescription
    {
        get
        {
            var state = CacheState;
            string sourceName = SourceDisplayName;
            string stateText = state.Source switch
            {
                CatalogCacheSource.Network => "已从软件服务更新",
                CatalogCacheSource.Memory => "正在使用内存缓存",
                CatalogCacheSource.Disk => "正在使用本地缓存",
                CatalogCacheSource.Offline => "正在使用离线缓存",
                _ => "目录缓存尚未加载"
            };
            return state.UpdatedAtUtc.HasValue
                ? $"{sourceName} · {stateText} · 更新于 {state.UpdatedAtUtc.Value.ToLocalTime():MM-dd HH:mm}"
                : $"{sourceName} · {stateText}";
        }
    }

    public static string RuntimeArchitecture => RuntimeInformation.OSArchitecture switch
    {
        Architecture.X64 => "x64",
        Architecture.X86 => "x86",
        Architecture.Arm64 => "arm64",
        _ => throw new PlatformNotSupportedException("当前 Windows 系统架构暂不支持下载安装包。")
    };

    /// <summary>目录内容变化（重新发布目录、详情回写、清空缓存）。可能在后台线程触发，UI 需切回 Dispatcher。</summary>
    public static event EventHandler? CatalogChanged;

    /// <summary>缓存状态/统计变化。可能在后台线程触发，UI 需切回 Dispatcher。</summary>
    public static event EventHandler? CacheChanged;

    /// <summary>软件源切换完成。可能在后台线程触发，UI 需切回 Dispatcher 后重新加载目录。</summary>
    public static event EventHandler? SourceChanged;

    /// <summary>后端服务地址切换完成（内存目录/详情/图标已清空）。可能在后台线程触发，UI 需切回 Dispatcher 后重新加载。</summary>
    public static event EventHandler? BackendChanged;

    // ---------------------------------------------------------------------------------------------
    // 软件源发现 / 切换
    // ---------------------------------------------------------------------------------------------

    /// <summary>
    /// 从后端 <c>GET /api/v1/sources</c> 获取软件源列表，并解析当前来源：
    /// 保存的 <c>source_id</c>（或上次运行时解析到的 ID）仍可用就继续使用，否则回退到服务端
    /// <c>is_default=true</c> 的可用来源，再否则使用第一个可用来源。来源变化会清空按来源隔离的缓存
    /// 并触发 <see cref="SourceChanged"/> / <see cref="CatalogChanged"/>。
    /// </summary>
    /// <param name="forceRefresh"><see langword="true"/> 时忽略 5 分钟内存复用窗口，强制重新请求。</param>
    public static async Task<IReadOnlyList<SourceInfo>> GetSourcesAsync(bool forceRefresh = false,
        CancellationToken ct = default)
    {
        EnsureInitialized();
        if (!forceRefresh && _sources.Count > 0
            && DateTimeOffset.UtcNow - _sourcesLoadedAt <= SourcesReuseWindow)
        {
            return _sources;
        }

        Task<IReadOnlyList<SourceInfo>> task;
        lock (SourcesTaskGate)
        {
            // 已完成的旧任务不能复用：forceRefresh 或 5 分钟缓存窗口过期后必须重新请求。
            if (_sourcesTask is null || _sourcesTask.IsCompleted)
            {
                _sourcesTask = LoadSourcesCoreAsync();
            }

            task = _sourcesTask;
        }

        try
        {
            return await task.WaitAsync(ct).ConfigureAwait(false);
        }
        catch
        {
            lock (SourcesTaskGate)
            {
                if (ReferenceEquals(_sourcesTask, task))
                {
                    _sourcesTask = null;
                }
            }

            throw;
        }
    }

    private static async Task<IReadOnlyList<SourceInfo>> LoadSourcesCoreAsync()
    {
        var stopwatch = Stopwatch.StartNew();
        try
        {
            var response = await GetAsync<SourcesResponse>("sources", CancellationToken.None, SourcesRequestTimeout)
                .ConfigureAwait(false);
            var items = new List<SourceInfo>();
            foreach (var item in response.Items ?? new List<SourceInfo>())
            {
                if (item is null || string.IsNullOrWhiteSpace(item.Id)) continue;
                string id = NormalizeSourceId(item.Id);
                if (id.Length == 0)
                {
                    AppLogService.Warn($"忽略服务端返回的非法软件源 ID：{item.Id}");
                    continue;
                }

                item.Id = id;
                item.Name = string.IsNullOrWhiteSpace(item.Name) ? id : item.Name.Trim();
                items.Add(item);
            }

            var snapshot = items.AsReadOnly();
            _sources = items;
            _sourcesLoadedAt = DateTimeOffset.UtcNow;
            ApplyResolvedSource(snapshot);
            AppLogService.Info(
                "已获取服务端软件源列表：" +
                string.Join("、", snapshot.Select(source =>
                    $"{source.Id}({DisplayNameOf(source)}, enabled={source.Enabled}, default={source.IsDefault}, " +
                    $"search_mode={source.SearchMode ?? "?"})")) +
                $"，共 {snapshot.Count} 个，耗时 {stopwatch.ElapsedMilliseconds} ms。");
            return snapshot;
        }
        catch (Exception error) when (IsMetadataFailure(error))
        {
            AppLogService.Warn(
                $"获取服务端软件源列表失败：{error.GetType().Name}：{error.Message}，耗时 {stopwatch.ElapsedMilliseconds} ms。",
                error);
            throw;
        }
    }

    /// <summary>把服务端来源列表解析成当前来源，并在来源变化时清空按来源隔离的状态。</summary>
    private static void ApplyResolvedSource(IReadOnlyList<SourceInfo> items)
    {
        string preferred = NormalizeSourceId(_currentSourceId);
        if (preferred.Length == 0)
        {
            preferred = NormalizeSourceId(AppSettingsService.Current.SourceId);
        }

        SourceInfo? resolved = FindEnabledSource(items, preferred);
        string? note = null;
        if (resolved is null && preferred.Length > 0)
        {
            resolved = items.FirstOrDefault(source => source.Enabled && source.IsDefault)
                       ?? items.FirstOrDefault(source => source.Enabled);
            if (resolved is not null)
            {
                note = $"保存的软件源（{preferred}）在服务端不可用，已自动切换到“{DisplayNameOf(resolved)}”。";
            }
        }
        else if (resolved is null)
        {
            resolved = items.FirstOrDefault(source => source.Enabled && source.IsDefault)
                       ?? items.FirstOrDefault(source => source.Enabled);
        }

        _sourceFallbackNote = note;
        if (resolved is null)
        {
            // 没有可用来源：保留已有来源（如果有），目录加载会给出可读错误。
            AppLogService.Warn("服务端没有可用的软件源。");
            return;
        }

        string resolvedId = resolved.Id!;
        bool changed = !string.Equals(_currentSourceId, resolvedId, StringComparison.Ordinal);
        bool hadSource = !string.IsNullOrEmpty(_currentSourceId);
        _currentSource = resolved;
        _currentSourceId = resolvedId;
        if (!changed)
        {
            return;
        }

        _store = CreateStore(resolvedId);
        ResetCatalogState();
        if (hadSource)
        {
            // 已运行中的来源被服务端禁用/删除：按“切换来源”处理，通知页面重新加载。
            AppLogService.Info($"当前软件源已不可用，自动切换为“{DisplayNameOf(resolved)}”。");
            CatalogChanged?.Invoke(null, EventArgs.Empty);
            SourceChanged?.Invoke(null, EventArgs.Empty);
        }
    }

    /// <summary>
    /// 切换软件源：先确认服务端仍提供该来源，再持久化 <c>source_id</c>、重建按来源隔离的缓存、
    /// 清空内存目录/详情/已知应用表，最后触发 <see cref="CatalogChanged"/> 与 <see cref="SourceChanged"/>。
    /// </summary>
    public static async Task SwitchSourceAsync(string? sourceId, CancellationToken ct = default)
    {
        string id = NormalizeSourceId(sourceId);
        if (id.Length == 0)
        {
            throw new ArgumentException("软件源 ID 不能为空。", nameof(sourceId));
        }

        EnsureInitialized();
        var sources = await GetSourcesAsync(forceRefresh: true, ct).ConfigureAwait(false);
        SourceInfo target = FindEnabledSource(sources, id)
                            ?? throw new ArgumentException($"软件源“{id}”不存在或未启用。", nameof(sourceId));

        await CatalogGate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            if (!string.Equals(target.Id, _currentSourceId, StringComparison.Ordinal))
            {
                AppLogService.Info($"正在切换软件源：{SourceDisplayName} → {DisplayNameOf(target)}。");
                _currentSource = target;
                _currentSourceId = target.Id!;
                _sourceFallbackNote = null;
                _store = CreateStore(target.Id!);
                ResetCatalogState();
                AppSettingsService.SetSourceId(target.Id!);
                CatalogChanged?.Invoke(null, EventArgs.Empty);
                SourceChanged?.Invoke(null, EventArgs.Empty);
                AppLogService.Info($"已切换到软件源：{DisplayNameOf(target)}。");
            }
            else
            {
                _currentSource = target;
                _sourceFallbackNote = null;
                AppSettingsService.SetSourceId(target.Id!);
            }
        }
        finally
        {
            CatalogGate.Release();
        }
    }

    /// <summary>
    /// 切换后端服务地址：保存设置 → 清空来源表/内存目录/详情/已知应用表 → 按新地址重建目录缓存，
    /// 然后触发 <see cref="BackendChanged"/> 与 <see cref="CatalogChanged"/>（调用方随后应重新加载目录）。
    /// 地址无效时抛出 <see cref="ArgumentException"/>，不会改动当前地址。
    /// </summary>
    public static async Task SetBackendBaseUrlAsync(string? url)
    {
        EnsureInitialized();
        string normalized = AppSettingsService.NormalizeBackendBaseUrl(url);
        // 保留调用方上下文（UI 线程），让设置保存与事件都在 UI 线程上触发。
        await CatalogGate.WaitAsync().ConfigureAwait(true);
        try
        {
            if (string.Equals(normalized, BackendBaseUrl, StringComparison.OrdinalIgnoreCase))
            {
                return;
            }

            string previous = BackendBaseUrl;
            AppLogService.Info($"正在切换后端服务地址：{previous} → {normalized}。");
            AppSettingsService.SetBackendBaseUrl(normalized);
            _sources = new List<SourceInfo>();
            _sourcesLoadedAt = default;
            _sourcesTask = null;
            _currentSource = null;
            _sourceFallbackNote = null;
            _store = CreateStore(_currentSourceId);
            ResetCatalogState();
            BackendChanged?.Invoke(null, EventArgs.Empty);
            CatalogChanged?.Invoke(null, EventArgs.Empty);
            AppLogService.Info($"已切换后端服务地址：{normalized}。");
        }
        finally
        {
            CatalogGate.Release();
        }
    }

    /// <summary>
    /// 测试服务端连接：对候选地址请求 <c>GET /api/v1/health</c>（5 秒超时），不修改当前设置。
    /// 地址格式无效时抛出 <see cref="ArgumentException"/>。
    /// </summary>
    public static async Task<(bool Ok, string Message)> TestConnectionAsync(string? url, CancellationToken ct = default)
    {
        string normalized = AppSettingsService.NormalizeBackendBaseUrl(url);
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeout.CancelAfter(TimeSpan.FromSeconds(5));
        try
        {
            var healthUri = new Uri(new Uri(normalized), "api/v1/health");
            using var response = await Client
                .GetAsync(healthUri, HttpCompletionOption.ResponseHeadersRead, timeout.Token)
                .ConfigureAwait(false);
            return response.IsSuccessStatusCode
                ? (true, "连接正常。")
                : (false, $"服务返回 HTTP {(int)response.StatusCode}。");
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            return (false, "连接超时（5 秒）。");
        }
        catch (Exception error) when (error is HttpRequestException or IOException or InvalidOperationException)
        {
            return (false, $"无法连接到服务端：{error.Message}");
        }
    }

    // ---------------------------------------------------------------------------------------------
    // 目录
    // ---------------------------------------------------------------------------------------------

    /// <summary>加载当前来源的完整目录（客户端本地筛选/分页）；按 source_id 隔离内存与磁盘缓存。</summary>
    public static async Task<IReadOnlyList<AppItem>> GetAppsAsync(CancellationToken ct = default)
    {
        EnsureInitialized();
        var stopwatch = Stopwatch.StartNew();
        AppLogService.Info($"开始加载软件目录：源={SourceDisplayName}。");
        await CatalogGate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            SourceInfo source = await EnsureSourceAsync(ct).ConfigureAwait(false);
            string sourceName = DisplayNameOf(source);
            var now = DateTimeOffset.UtcNow;
            if (HasLoaded && IsFresh(_catalogUpdatedAt, now))
            {
                SetCacheState(_lastNetworkError is null ? CatalogCacheSource.Memory : CatalogCacheSource.Offline,
                    CatalogUpdatedAtUtc ?? _catalogUpdatedAt, Store.LastError, _lastNetworkError);
                AppLogService.Info($"软件目录加载完成（内存缓存）：源={sourceName}，{Apps.Count} 款软件，" +
                                   $"耗时 {stopwatch.ElapsedMilliseconds} ms。");
                return Apps;
            }

            var disk = await ReadUsableDiskAsync(now, ct).ConfigureAwait(false);
            if (!HasLoaded && disk?.IsFresh(now) == true)
            {
                await PublishCatalogAsync(disk.Items!, disk.UpdatedAtUtc, CatalogCacheSource.Disk, ct)
                    .ConfigureAwait(false);
                AppLogService.Info($"软件目录加载完成（本地缓存）：源={sourceName}，{Apps.Count} 款软件，" +
                                   $"耗时 {stopwatch.ElapsedMilliseconds} ms。");
                return Apps;
            }

            try
            {
                // 目录请求永远走普通 GET：服务端只读本地快照并由后台线程自动更新；客户端不拼 refresh 参数。
                var response = await GetAsync<SourceCatalogResponse>(SourcePath(source.Id!, "apps"), ct,
                    CatalogRequestTimeout).ConfigureAwait(false);
                if (response.Items is null || response.Total is null || response.Total < response.Items.Count)
                {
                    throw new JsonException("软件目录响应缺少有效的 items 或 total 字段。");
                }

                if (!string.IsNullOrWhiteSpace(response.Source)
                    && !string.Equals(response.Source, source.Id, StringComparison.OrdinalIgnoreCase))
                {
                    throw new JsonException("软件目录响应的来源与请求不一致。");
                }

                var normalized = MapSummaries(response.Items, source);
                if (normalized.Length == 0 && response.Items.Count > 0)
                {
                    throw new JsonException("软件目录响应中没有可用的软件条目。");
                }

                ct.ThrowIfCancellationRequested();
                _lastNetworkError = null;
                Details.Clear();
                DateTimeOffset fetchedAt = DateTimeOffset.UtcNow;
                await PublishCatalogAsync(normalized, fetchedAt, CatalogCacheSource.Network, ct)
                    .ConfigureAwait(false);
                // 目录落盘（key 含来源 ID），离线时仍可浏览。
                await Store.WriteCatalogAsync(normalized, fetchedAt, ct).ConfigureAwait(false);
                SetCatalogUpdatedAt(response.UpdatedAt);
                SetCacheState(CatalogCacheSource.Network, CatalogUpdatedAtUtc ?? fetchedAt,
                    response.Stale ? $"“{sourceName}”目录当前为服务端缓存数据，可能不是最新。" : Store.LastError);
                AppLogService.Info(
                    $"软件目录加载完成：源={sourceName}，{Apps.Count} 款软件，缓存=Network，" +
                    $"按当前系统隐藏 {HiddenIncompatibleCount} 款，耗时 {stopwatch.ElapsedMilliseconds} ms。");
                return Apps;
            }
            catch (Exception error) when (IsMetadataFailure(error) && !ct.IsCancellationRequested)
            {
                _lastNetworkError = FriendlyMetadataError(error);
                if (HasLoaded && CanUseOffline(_catalogUpdatedAt, now))
                {
                    AppLogService.Warn(
                        $"“{sourceName}”目录更新失败，正在使用内存缓存：{_lastNetworkError}" +
                        $"（{error.GetType().Name}：{error.Message}）");
                    SetCacheState(CatalogCacheSource.Offline, CatalogUpdatedAtUtc ?? _catalogUpdatedAt,
                        Store.LastError, _lastNetworkError);
                    return Apps;
                }

                if (disk is not null)
                {
                    AppLogService.Warn(
                        $"“{sourceName}”目录更新失败，已回退本地目录缓存：{_lastNetworkError}" +
                        $"（{error.GetType().Name}：{error.Message}）");
                    await PublishCatalogAsync(disk.Items!, disk.UpdatedAtUtc, CatalogCacheSource.Offline, ct)
                        .ConfigureAwait(false);
                    return Apps;
                }

                AppLogService.Warn(
                    $"“{sourceName}”目录更新失败且没有可用缓存：{_lastNetworkError}" +
                    $"（{error.GetType().Name}：{error.Message}）");
                ResetCatalogState();
                SetCacheState(CatalogCacheSource.None, null, Store.LastError, _lastNetworkError);
                CatalogChanged?.Invoke(null, EventArgs.Empty);
                throw;
            }
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            AppLogService.Info($"软件目录加载已取消：源={SourceDisplayName}，耗时 {stopwatch.ElapsedMilliseconds} ms。");
            throw;
        }
        catch (Exception exception) when (exception is not OutOfMemoryException)
        {
            AppLogService.Warn(
                $"软件目录加载失败：源={SourceDisplayName}，耗时 {stopwatch.ElapsedMilliseconds} ms，" +
                $"{exception.GetType().Name}：{exception.Message}",
                exception);
            throw;
        }
        finally
        {
            CatalogGate.Release();
        }
    }

    /// <summary>
    /// 获取当前来源的分类列表（<c>GET /sources/{id}/categories</c>）。来源声明不支持分类、接口返回 404/204
    /// 或数据为空时返回空列表，调用方回退为按目录条目分类名聚合。
    /// </summary>
    public static async Task<IReadOnlyList<SourceCategory>> GetCategoriesAsync(CancellationToken ct = default)
    {
        EnsureInitialized();
        SourceInfo source = await EnsureSourceAsync(ct).ConfigureAwait(false);
        if (!source.CapabilitiesOrDefault.Categories)
        {
            return Array.Empty<SourceCategory>();
        }

        try
        {
            var response = await GetAsync<SourceCategoriesResponse>(SourcePath(source.Id!, "categories"), ct,
                StoreRequestTimeout).ConfigureAwait(false);
            if (!string.IsNullOrWhiteSpace(response.Source)
                && !string.Equals(response.Source, source.Id, StringComparison.OrdinalIgnoreCase))
            {
                throw new JsonException("分类响应的来源与请求不一致。");
            }

            var categories = new List<SourceCategory>();
            foreach (var category in response.Categories ?? new List<SourceCategory>())
            {
                if (category is null || string.IsNullOrWhiteSpace(category.Name)) continue;
                string name = category.Name.Trim();
                if (string.Equals(name, "全部", StringComparison.Ordinal)) continue;
                categories.Add(new SourceCategory
                {
                    Id = category.Id?.Trim() ?? string.Empty,
                    Name = name,
                    Count = category.Count,
                    Group = category.Group
                });
            }

            return categories.AsReadOnly();
        }
        catch (HttpRequestException error) when (error.StatusCode is HttpStatusCode.NotFound or HttpStatusCode.NoContent)
        {
            AppLogService.Verbose($"软件源“{SourceDisplayName}”没有分类接口，按目录条目聚合分类。");
            return Array.Empty<SourceCategory>();
        }
        catch (JsonException error)
        {
            // 部分服务端对“不支持的能力”返回 204/空响应；分类不是目录加载的必要条件。
            AppLogService.Verbose($"软件源“{SourceDisplayName}”分类响应不可用，按目录条目聚合分类：{error.Message}");
            return Array.Empty<SourceCategory>();
        }
    }

    // ---------------------------------------------------------------------------------------------
    // 详情
    // ---------------------------------------------------------------------------------------------

    /// <summary>加载一款软件的详情（<c>GET /sources/{id}/apps/{app_id}</c>），缓存按 <c>source_id + app_id</c> 隔离。</summary>
    public static async Task<AppItem> GetAppAsync(string id, CancellationToken ct = default, bool forceRefresh = false)
    {
        EscapeResourceId(id);
        EnsureInitialized();
        await CatalogGate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            SourceInfo source = await EnsureSourceAsync(ct).ConfigureAwait(false);
            string sourceId = source.Id!;
            string cacheKey = CompositeKey(sourceId, id);
            var now = DateTimeOffset.UtcNow;
            // forceRefresh 只跳过客户端详情缓存；目录摘要不能当成详情（会缺截图/更新说明）。
            (AppItem App, DateTimeOffset Updated)? cached = Details.TryGetValue(cacheKey, out var detail) ? detail : null;
            var known = FindKnownApp(sourceId, id);
            if (!forceRefresh && cached is { } hit && IsFresh(hit.Updated, now))
            {
                SetCacheState(_lastNetworkError is null ? CatalogCacheSource.Memory : CatalogCacheSource.Offline,
                    hit.Updated, Store.LastError, _lastNetworkError);
                return hit.App;
            }

            try
            {
                var details = await GetAsync<SourceAppDetails>(
                    SourcePath(sourceId, $"apps/{EscapeResourceId(id)}"), ct, StoreRequestTimeout).ConfigureAwait(false);
                if (!string.IsNullOrWhiteSpace(details.Id)
                    && !string.Equals(details.Id, id, StringComparison.Ordinal))
                {
                    throw new JsonException("软件详情响应与请求的应用 ID 不一致。");
                }

                if (string.IsNullOrWhiteSpace(details.Id)) details.Id = id;
                var app = NormalizeApp(details.ToAppItem(sourceId, DisplayNameOf(source)));
                app.DisplayIconSource = (cached?.App ?? known)?.DisplayIconSource;
                ct.ThrowIfCancellationRequested();
                _lastNetworkError = null;
                Details[cacheKey] = (app, DateTimeOffset.UtcNow);
                RegisterKnownApp(app);
                // 单款详情只加载这一张图标，不做目录整体预热。
                await EnsureIconAsync(app, ct).ConfigureAwait(false);
                await ReplaceCatalogAppAsync(app, ct).ConfigureAwait(false);
                SetCacheState(CatalogCacheSource.Network, Details[cacheKey].Updated, Store.LastError);
                return app;
            }
            catch (Exception error) when (IsMetadataFailure(error) && !ct.IsCancellationRequested
                && (error is not HttpRequestException request
                    || request.StatusCode is null || (int)request.StatusCode >= 500))
            {
                _lastNetworkError = FriendlyMetadataError(error);
                // 详情拉取失败时退回已缓存的详情或目录摘要，界面至少还能显示基本信息。
                var fallback = cached?.App ?? known;
                var fallbackUpdated = cached?.Updated ?? _catalogUpdatedAt;
                if (fallback is not null && CanUseOffline(fallbackUpdated, now))
                {
                    SetCacheState(CatalogCacheSource.Offline, fallbackUpdated, Store.LastError, _lastNetworkError);
                    return fallback;
                }

                SetCacheState(CacheState.Source, CacheState.UpdatedAtUtc, Store.LastError, _lastNetworkError);
                throw;
            }
        }
        finally
        {
            CatalogGate.Release();
        }
    }

    /// <summary>
    /// 把详情回写到内存目录与磁盘目录缓存（同一来源同一 ID 的条目替换为详情对象）。
    /// 目录尚未包含该软件（例如服务端搜索返回的目录外条目）时只更新已知应用表。
    /// </summary>
    private static async Task ReplaceCatalogAppAsync(AppItem app, CancellationToken ct)
    {
        var apps = Apps;
        int index = -1;
        for (int i = 0; i < apps.Count; i++)
        {
            if (string.Equals(apps[i].Id, app.Id, StringComparison.Ordinal)
                && string.Equals(apps[i].SourceId ?? string.Empty, app.SourceId ?? string.Empty,
                    StringComparison.OrdinalIgnoreCase))
            {
                index = i;
                break;
            }
        }

        if (index < 0) return;
        var updated = apps.ToArray();
        updated[index] = app;
        Volatile.Write(ref _apps, Array.AsReadOnly(updated));
        if (_catalogUpdatedAt.HasValue)
        {
            await Store.WriteCatalogAsync(Apps, _catalogUpdatedAt.Value, ct).ConfigureAwait(false);
        }

        CatalogChanged?.Invoke(null, EventArgs.Empty);
    }

    // ---------------------------------------------------------------------------------------------
    // 下载地址 / 在线搜索 / 刷新
    // ---------------------------------------------------------------------------------------------

    /// <summary>
    /// 按软件 ID 获取当前来源的最新版下载地址（<c>GET /sources/{id}/apps/{app_id}/download</c>）。
    /// 下载地址由服务端实时解析，客户端不缓存。
    /// </summary>
    public static async Task<AppRelease> GetLatestReleaseAsync(string appId, CancellationToken ct = default)
    {
        BclCompat.ThrowIfNullOrWhiteSpace(appId);
        EnsureInitialized();
        SourceInfo source = await EnsureSourceAsync(ct).ConfigureAwait(false);
        return await GetLatestReleaseCoreAsync(source, appId, ct).ConfigureAwait(false);
    }

    /// <summary>
    /// 按软件对象获取最新版下载地址。<see cref="AppItem.SourceId"/> 决定请求哪个来源（下载任务在切换来源后
    /// 仍能拿到自己来源的地址）；缺失时使用当前来源。
    /// </summary>
    public static async Task<AppRelease> GetLatestReleaseAsync(AppItem app, CancellationToken ct = default)
    {
        ArgumentNullException.ThrowIfNull(app);
        EnsureInitialized();
        string sourceId = !string.IsNullOrWhiteSpace(app.SourceId) ? app.SourceId!.Trim() : _currentSourceId;
        SourceInfo? source = FindSourceById(sourceId);
        if (source is null)
        {
            source = await EnsureSourceAsync(ct).ConfigureAwait(false);
        }

        return await GetLatestReleaseCoreAsync(source, app.Id, ct).ConfigureAwait(false);
    }

    private static async Task<AppRelease> GetLatestReleaseCoreAsync(SourceInfo source, string appId,
        CancellationToken ct)
    {
        BclCompat.ThrowIfNullOrWhiteSpace(appId);
        if (!source.CapabilitiesOrDefault.Download)
        {
            throw new NotSupportedException($"软件源“{DisplayNameOf(source)}”不提供安装包下载地址。");
        }

        string path = SourcePath(source.Id!, $"apps/{EscapeResourceId(appId)}/download") +
                      $"?architecture={Uri.EscapeDataString(RuntimeArchitecture)}";
        AppRelease release = await GetAsync<AppRelease>(path, ct, StoreRequestTimeout).ConfigureAwait(false);
        release = NormalizeRelease(release);
        if (!string.Equals(release.Platform, "windows", StringComparison.OrdinalIgnoreCase))
        {
            throw new JsonException($"软件源“{DisplayNameOf(source)}”返回的安装包平台不是 Windows。");
        }

        if (!IsReleaseArchitectureCompatible(release.Architecture))
        {
            throw new JsonException($"安装包架构（{release.Architecture}）与当前 Windows 系统（{RuntimeArchitecture}）不一致。");
        }

        return release;
    }

    /// <summary>
    /// 服务端搜索（<c>GET /sources/{id}/search?q=&amp;limit=</c>）：<c>search_mode=api</c> 的来源由搜索框自动调用，
    /// 可能返回目录之外的软件。来源声明不支持搜索时返回空列表。
    /// </summary>
    public static async Task<IReadOnlyList<AppItem>> SearchAsync(string query, CancellationToken ct = default)
    {
        if (string.IsNullOrWhiteSpace(query)) return Array.Empty<AppItem>();
        EnsureInitialized();
        SourceInfo source = await EnsureSourceAsync(ct).ConfigureAwait(false);
        if (!source.CapabilitiesOrDefault.Search)
        {
            AppLogService.Verbose($"软件源“{DisplayNameOf(source)}”不支持服务端搜索。");
            return Array.Empty<AppItem>();
        }

        string text = query.Trim();
        string path = SourcePath(source.Id!, "search") +
                      $"?q={Uri.EscapeDataString(text)}&limit={SearchResultLimit}";
        var response = await GetAsync<SourceSearchResponse>(path, ct, StoreRequestTimeout).ConfigureAwait(false);
        if (!string.IsNullOrWhiteSpace(response.Source)
            && !string.Equals(response.Source, source.Id, StringComparison.OrdinalIgnoreCase))
        {
            throw new JsonException("软件搜索响应的来源与请求不一致。");
        }

        if (response.Items is null || response.Items.Count == 0) return Array.Empty<AppItem>();
        var results = new List<AppItem>(response.Items.Count);
        var ids = new HashSet<string>(StringComparer.Ordinal);
        foreach (var summary in response.Items)
        {
            AppItem app;
            try
            {
                app = NormalizeApp(summary.ToAppItem(source.Id!, DisplayNameOf(source)));
            }
            catch (JsonException)
            {
                continue;
            }

            if (!ids.Add(app.Id)) continue;
            RegisterKnownApp(app);
            results.Add(app);
        }

        if (results.Count > 0)
        {
            AppLogService.Verbose(
                $"服务端搜索完成：源={DisplayNameOf(source)}，关键词“{text}”，{results.Count} 条结果。");
        }

        return results.AsReadOnly();
    }

    /// <summary>
    /// 请求服务端刷新当前来源的目录（<c>POST /sources/{id}/refresh</c>，需要 <c>capabilities.refresh</c>）。
    /// 成功后清空本地目录缓存与内存目录，下一次加载会重新拉取服务端最新快照。
    /// 当前客户端没有自动调用入口（服务端按自己的节奏刷新目录），保留给未来的手动刷新界面使用。
    /// </summary>
    public static async Task<bool> RefreshSourceAsync(CancellationToken ct = default)
    {
        EnsureInitialized();
        SourceInfo source = await EnsureSourceAsync(ct).ConfigureAwait(false);
        if (!source.CapabilitiesOrDefault.Refresh)
        {
            return false;
        }

        var stopwatch = Stopwatch.StartNew();
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeout.CancelAfter(RefreshRequestTimeout);
        using var response = await Client
            .PostAsync(BuildApiUri(SourcePath(source.Id!, "refresh")), content: null, timeout.Token)
            .ConfigureAwait(false);
        response.EnsureSuccessStatusCode();

        await Store.DeleteCatalogAsync(ct).ConfigureAwait(false);
        await CatalogGate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            ResetCatalogState();
            SetCacheState(CatalogCacheSource.None, null, Store.LastError);
            CatalogChanged?.Invoke(null, EventArgs.Empty);
        }
        finally
        {
            CatalogGate.Release();
        }

        AppLogService.Info(
            $"已刷新软件源“{DisplayNameOf(source)}”的服务端目录，耗时 {stopwatch.ElapsedMilliseconds} ms。");
        return true;
    }

    // ---------------------------------------------------------------------------------------------
    // 图标
    // ---------------------------------------------------------------------------------------------

    /// <summary>
    /// 按需加载一款软件的图标：先查本地缓存，未命中再请求服务端返回的直连地址，失败回退代理地址；
    /// 内部并发上限 6，失败静默返回 <see langword="false"/>，UI 继续显示文字徽章。
    /// </summary>
    public static async Task<bool> EnsureIconAsync(AppItem app, CancellationToken ct = default)
    {
        ArgumentNullException.ThrowIfNull(app);
        EnsureInitialized();
        if (!string.IsNullOrWhiteSpace(app.DisplayIconSource)) return true;
        if (string.IsNullOrWhiteSpace(app.IconUrl) && string.IsNullOrWhiteSpace(app.IconFallbackUrl)) return false;

        await IconDownloadGate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            if (!string.IsNullOrWhiteSpace(app.DisplayIconSource)) return true;
            foreach (string candidate in EnumerateIconUrls(app))
            {
                // 单条失败（404/超时/坏图）不能阻止兜底地址。
                try
                {
                    var cached = await Store.FindIconAsync(candidate, ct).ConfigureAwait(false);
                    if (!string.IsNullOrWhiteSpace(cached))
                    {
                        app.DisplayIconSource = cached;
                        return true;
                    }
                }
                catch (OperationCanceledException) when (ct.IsCancellationRequested)
                {
                    throw;
                }
                catch (Exception error) when (error is HttpRequestException or IOException or InvalidDataException
                    or UnauthorizedAccessException or OperationCanceledException)
                {
                    // 继续尝试下一个候选地址。
                }
            }

            foreach (string candidate in EnumerateIconUrls(app))
            {
                try
                {
                    var path = await Store.GetIconAsync(candidate, ct).ConfigureAwait(false);
                    if (!string.IsNullOrWhiteSpace(path))
                    {
                        app.DisplayIconSource = path;
                        return true;
                    }
                }
                catch (OperationCanceledException) when (ct.IsCancellationRequested)
                {
                    throw;
                }
                catch (Exception error) when (error is HttpRequestException or IOException or InvalidDataException
                    or UnauthorizedAccessException or OperationCanceledException)
                {
                    // 主地址失败时继续尝试兜底地址。
                }
            }

            return false;
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception error) when (error is HttpRequestException or IOException or InvalidDataException
            or UnauthorizedAccessException or OperationCanceledException)
        {
            // 图标缺失不应影响列表加载。
            return false;
        }
        finally
        {
            IconDownloadGate.Release();
        }
    }

    /// <summary>图标候选地址：服务端返回的主地址在前，兜底代理地址在后。</summary>
    private static IEnumerable<string> EnumerateIconUrls(AppItem app)
    {
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        if (!string.IsNullOrWhiteSpace(app.IconUrl) && seen.Add(app.IconUrl!))
        {
            yield return app.IconUrl!;
        }

        if (!string.IsNullOrWhiteSpace(app.IconFallbackUrl) && seen.Add(app.IconFallbackUrl!))
        {
            yield return app.IconFallbackUrl!;
        }
    }

    public static async Task DiscardIconAsync(AppItem app)
    {
        var path = app.DisplayIconSource;
        app.DisplayIconSource = null;
        await Store.DiscardIconAsync(path).ConfigureAwait(false);
        CacheChanged?.Invoke(null, EventArgs.Empty);
    }

    // ---------------------------------------------------------------------------------------------
    // 缓存清理
    // ---------------------------------------------------------------------------------------------

    public static async Task<CacheClearResult> ClearCacheAsync(CancellationToken ct = default)
    {
        EnsureInitialized();
        await CatalogGate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            foreach (var app in Apps.Concat(Details.Values.Select(value => value.App)).Concat(KnownApps.Values))
            {
                app.DisplayIconSource = null;
            }

            var errors = (await Store.ClearAsync(ct).ConfigureAwait(false)).ToList();
            foreach (var error in await StoreMediaService.ClearCacheAsync(ct).ConfigureAwait(false))
            {
                errors.Add(error);
            }

            bool localCleared = errors.Count == 0;
            ResetCatalogState();
            KnownApps.Clear();

            bool backendCleared = false;
            try
            {
                using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
                timeout.CancelAfter(TimeSpan.FromSeconds(12));
                // 后端 DELETE /cache 清理版本缓存与各来源的目录/详情/图片缓存（不会清令牌）。
                using var response = await Client.DeleteAsync(BuildApiUri("cache"), timeout.Token).ConfigureAwait(false);
                response.EnsureSuccessStatusCode();
                backendCleared = true;
            }
            catch (Exception error) when (IsMetadataFailure(error) && !ct.IsCancellationRequested)
            {
                errors.Add("本地缓存已处理，但软件服务的版本缓存暂时无法清理。请连接服务后重试。");
            }

            SetCacheState(CatalogCacheSource.None, null, errors.Count > 0 ? string.Join(" ", errors) : null);
            CatalogChanged?.Invoke(null, EventArgs.Empty);
            return new CacheClearResult(localCleared, backendCleared, errors);
        }
        finally
        {
            CatalogGate.Release();
        }
    }

    // ---------------------------------------------------------------------------------------------
    // 内部实现
    // ---------------------------------------------------------------------------------------------

    /// <summary>确保当前来源已解析；服务端来源表过期时刷新一次，失败则继续使用已解析的来源。</summary>
    private static async Task<SourceInfo> EnsureSourceAsync(CancellationToken ct)
    {
        EnsureInitialized();
        bool needsRefresh = _sources.Count == 0
                            || _currentSource is null
                            || DateTimeOffset.UtcNow - _sourcesLoadedAt > SourcesReuseWindow;
        if (needsRefresh)
        {
            try
            {
                await GetSourcesAsync(_sources.Count == 0, ct).ConfigureAwait(false);
            }
            catch (HttpRequestException error) when (error.StatusCode == HttpStatusCode.NotFound)
            {
                throw new InvalidDataException("当前服务端不支持统一软件源接口（GET /api/v1/sources），请升级服务端。", error);
            }
            catch (Exception error) when (IsMetadataFailure(error) && _currentSource is not null
                && !ct.IsCancellationRequested)
            {
                AppLogService.Warn(
                    $"刷新服务端软件源列表失败，继续使用来源“{SourceDisplayName}”：" +
                    $"{error.GetType().Name}：{error.Message}");
            }
        }

        return _currentSource is { Enabled: true } source
            ? source
            : throw new InvalidDataException("服务端没有可用的软件源，请稍后重试。");
    }

    private static SourceInfo? FindSourceById(string? sourceId)
    {
        if (string.IsNullOrWhiteSpace(sourceId)) return null;
        string id = sourceId.Trim();
        return _sources.FirstOrDefault(source =>
            string.Equals(source.Id, id, StringComparison.OrdinalIgnoreCase));
    }

    private static SourceInfo? FindEnabledSource(IEnumerable<SourceInfo> sources, string sourceId)
    {
        if (sourceId.Length == 0) return null;
        return sources.FirstOrDefault(source =>
            source.Enabled && string.Equals(source.Id, sourceId, StringComparison.OrdinalIgnoreCase));
    }

    private static string DisplayNameOf(SourceInfo source) =>
        string.IsNullOrWhiteSpace(source.Name) ? source.Id ?? string.Empty : source.Name.Trim();

    /// <summary>当前来源是否声明支持下载（详情页据此决定显示“开始下载”还是“前往官网下载”）。</summary>
    public static bool SupportsDownload(string? sourceId)
    {
        SourceInfo? source = FindSourceById(sourceId) ?? _currentSource;
        return source?.CapabilitiesOrDefault.Download ?? true;
    }

    private static void EnsureInitialized()
    {
        if (Volatile.Read(ref _initialized)) return;
        lock (InitializationGate)
        {
            if (_initialized) return;
            // 保证先加载 settings.json，再读取保存的来源 ID（空字符串表示跟随服务端默认来源）。
            AppSettingsService.Initialize();
            _currentSourceId = NormalizeSourceId(AppSettingsService.Current.SourceId);
            _store = CreateStore(_currentSourceId);
            Volatile.Write(ref _initialized, true);
        }
    }

    /// <summary>来源 ID 规整：只接受字母、数字与 <c>. _ -</c>，最长 64 字符；非法值视为未选择。</summary>
    internal static string NormalizeSourceId(string? value)
    {
        string text = value?.Trim() ?? string.Empty;
        if (text.Length == 0 || text.Length > MaxSourceIdLength) return string.Empty;
        foreach (char character in text)
        {
            if (!char.IsLetterOrDigit(character) && character is not ('.' or '_' or '-'))
            {
                return string.Empty;
            }
        }

        return text;
    }

    /// <summary>按当前来源重建本地缓存：目录缓存 key 含 <c>source_id</c>，切换来源时不会串用目录缓存。</summary>
    private static ClientCacheStore CreateStore(string sourceId) =>
        new(CacheRootPath, BackendBaseUrl, RuntimeArchitecture, sourceId);

    private static ClientCacheStore Store
    {
        get
        {
            EnsureInitialized();
            return _store!;
        }
    }

    private static void ResetCatalogState()
    {
        Volatile.Write(ref _apps, Array.Empty<AppItem>());
        Volatile.Write(ref _hasLoaded, false);
        Volatile.Write(ref _hiddenIncompatible, 0);
        Volatile.Write(ref _cacheState, new CatalogCacheState(CatalogCacheSource.None, null, null, null,
            SourceDisplayName));
        _catalogUpdatedAt = null;
        SetCatalogUpdatedAt(null);
        _lastNetworkError = null;
        Details.Clear();
        KnownApps.Clear();
    }

    private static string CompositeKey(string sourceId, string appId) => sourceId + ":" + appId;

    private static void RegisterKnownApp(AppItem app)
    {
        if (app is null || string.IsNullOrWhiteSpace(app.Id)) return;
        KnownApps[CompositeKey(app.SourceId ?? _currentSourceId, app.Id)] = app;
    }

    private static AppItem? FindKnownApp(string sourceId, string appId)
    {
        if (KnownApps.TryGetValue(CompositeKey(sourceId, appId), out var registered)) return registered;
        if (Details.TryGetValue(CompositeKey(sourceId, appId), out var detail)) return detail.App;
        return Apps.FirstOrDefault(item => string.Equals(item.Id, appId, StringComparison.Ordinal)
                                           && string.Equals(item.SourceId ?? string.Empty, sourceId,
                                               StringComparison.OrdinalIgnoreCase));
    }

    private static AppItem[] MapSummaries(IEnumerable<SourceAppSummary> items, SourceInfo source)
    {
        string sourceId = source.Id!;
        string sourceName = DisplayNameOf(source);
        var result = new List<AppItem>();
        var ids = new HashSet<string>(StringComparer.Ordinal);
        foreach (var summary in items)
        {
            AppItem app;
            try
            {
                app = NormalizeApp(summary.ToAppItem(sourceId, sourceName));
            }
            catch (JsonException)
            {
                // 单条脏数据不能拖垮整份目录。
                continue;
            }

            if (!ids.Add(app.Id)) continue;
            RegisterKnownApp(app);
            result.Add(app);
        }

        return result.ToArray();
    }

    private static async Task PublishCatalogAsync(IEnumerable<AppItem> items, DateTimeOffset updated,
        CatalogCacheSource source, CancellationToken ct)
    {
        // 按当前系统过滤：32 位系统不显示只有 x64 安装包的软件，Windows 7 不显示要求 Win10/11 的软件。
        var compatible = new List<AppItem>();
        var hidden = 0;
        foreach (var item in items)
        {
            if (PlatformCompatibility.IsCompatible(item, RuntimeArchitecture, Environment.OSVersion.Version))
            {
                compatible.Add(item);
            }
            else
            {
                hidden++;
            }
        }

        Volatile.Write(ref _hiddenIncompatible, hidden);
        var snapshot = Array.AsReadOnly(compatible.ToArray());
        foreach (var app in snapshot)
        {
            // 旧版本缓存条目可能没有来源信息（来源功能之前写入）：按当前来源补齐，保证详情/下载/筛选一致。
            if (string.IsNullOrWhiteSpace(app.SourceId)) app.SourceId = _currentSourceId;
            if (string.IsNullOrWhiteSpace(app.SourceName)) app.SourceName = SourceDisplayName;
            RegisterKnownApp(app);
        }

        // 大目录（几千款）与精选目录都统一由列表行按需加载图标，发布目录时不做批量图标磁盘分配/预热。
        _catalogUpdatedAt = updated;
        Volatile.Write(ref _apps, snapshot);
        Volatile.Write(ref _hasLoaded, true);
        SetCacheState(source, updated, Store.LastError, source == CatalogCacheSource.Offline ? _lastNetworkError : null);
        CatalogChanged?.Invoke(null, EventArgs.Empty);
    }

    private static async Task<CatalogCacheDocument?> ReadUsableDiskAsync(DateTimeOffset now, CancellationToken ct)
    {
        var disk = await Store.ReadCatalogAsync(now, ct).ConfigureAwait(false);
        if (disk is null) return null;
        try
        {
            disk.Items = disk.Items!.Select(app => NormalizeApp(app, displayOnly: true)).ToList();
            ValidateUniqueIds(disk.Items);
            return disk;
        }
        catch (JsonException)
        {
            return null;
        }
    }

    private static void ValidateUniqueIds(IEnumerable<AppItem> items)
    {
        var ids = items.Select(app => app.Id).ToArray();
        if (ids.Distinct(StringComparer.Ordinal).Count() != ids.Length)
            throw new JsonException("软件目录中包含重复的软件 ID。");
    }

    private static bool IsFresh(DateTimeOffset? updated, DateTimeOffset now) => updated.HasValue
        && now >= updated.Value && now - updated.Value < TimeSpan.FromMinutes(5);

    private static bool CanUseOffline(DateTimeOffset? updated, DateTimeOffset now) => updated.HasValue
        && now >= updated.Value && now - updated.Value <= TimeSpan.FromHours(24);

    private static bool IsMetadataFailure(Exception error) =>
        error is HttpRequestException or IOException or JsonException or InvalidDataException or OperationCanceledException;

    private static string FriendlyMetadataError(Exception error) => error is OperationCanceledException
        ? "更新软件资料超时，请稍后重试。"
        : error is JsonException ? "软件服务返回的资料格式无效，请稍后重试。"
        : "更新软件资料失败，请检查软件服务和网络连接。";

    private static void SetCacheState(CatalogCacheSource source, DateTimeOffset? updated, string? warning = null,
        string? networkError = null)
    {
        Volatile.Write(ref _cacheState, new CatalogCacheState(source, updated, warning, networkError,
            SourceDisplayName));
        CacheChanged?.Invoke(null, EventArgs.Empty);
    }

    /// <summary>记录服务端目录响应的 <c>updated_at</c>（UTC ticks，<see langword="null"/> 表示未知）。</summary>
    private static void SetCatalogUpdatedAt(DateTimeOffset? updatedAt) =>
        Volatile.Write(ref _catalogServerUpdatedAtTicks, updatedAt?.UtcDateTime.Ticks ?? UnknownCatalogTimestamp);

    private static bool IsReleaseArchitectureCompatible(string releaseArchitecture)
    {
        if (string.Equals(releaseArchitecture, RuntimeArchitecture, StringComparison.OrdinalIgnoreCase)) return true;
        // x64 Windows 通过 WOW64 运行 32 位安装包；其余按不兼容处理。
        return string.Equals(RuntimeArchitecture, "x64", StringComparison.OrdinalIgnoreCase)
               && string.Equals(releaseArchitecture, "x86", StringComparison.OrdinalIgnoreCase);
    }

    private static bool IsReleaseUsable(AppRelease release) =>
        !string.IsNullOrWhiteSpace(release.DownloadUrl)
        && (string.IsNullOrWhiteSpace(release.Platform)
            || string.Equals(release.Platform, "windows", StringComparison.OrdinalIgnoreCase))
        && (string.IsNullOrWhiteSpace(release.Architecture) || IsReleaseArchitectureCompatible(release.Architecture));

    private static async Task<T> GetAsync<T>(string path, CancellationToken ct, TimeSpan? timeout = null)
        where T : class
    {
        using var requestCancellation = CancellationTokenSource.CreateLinkedTokenSource(ct);
        requestCancellation.CancelAfter(timeout ?? DefaultRequestTimeout);
        CancellationToken requestToken = requestCancellation.Token;
        using var response = await Client
            .GetAsync(BuildApiUri(path), HttpCompletionOption.ResponseHeadersRead, requestToken)
            .ConfigureAwait(false);
        response.EnsureSuccessStatusCode();
        await using var stream = await response.Content.ReadAsStreamAsync(requestToken).ConfigureAwait(false);
        return await JsonSerializer.DeserializeAsync<T>(stream, JsonOptions, requestToken).ConfigureAwait(false)
               ?? throw new JsonException("软件服务返回了空响应。");
    }

    private static AppItem NormalizeApp(AppItem app, bool displayOnly = false)
    {
        if (app is null || string.IsNullOrWhiteSpace(app.Id) || string.IsNullOrWhiteSpace(app.Name)
            || app.Id is "." or "..")
        {
            throw new JsonException("软件目录中的软件缺少 ID 或名称。");
        }

        app.Category = string.IsNullOrWhiteSpace(app.Category) ? "未分类" : app.Category;
        app.CategoryId = app.CategoryId?.Trim() ?? string.Empty;
        app.Tags ??= Array.Empty<string>();
        app.Features ??= Array.Empty<string>();
        app.Labels ??= Array.Empty<string>();
        app.DetailTags ??= Array.Empty<AppDetailTag>();
        app.Screenshots ??= Array.Empty<AppScreenshot>();
        app.IconUrl = ResolveOptionalHttpUrl(app.IconUrl);
        app.IconFallbackUrl = ResolveOptionalHttpUrl(app.IconFallbackUrl);
        app.WebsiteUrl = ResolveOptionalHttpUrl(app.WebsiteUrl) ?? string.Empty;
        app.DownloadPageUrl = ResolveOptionalHttpUrl(app.DownloadPageUrl) ?? string.Empty;
        app.CoverUrl = ResolveOptionalHttpUrl(app.CoverUrl);
        app.CoverProxyUrl = ResolveOptionalHttpUrl(app.CoverProxyUrl);
        if (app.Screenshots.Count > 0)
        {
            var screenshots = new List<AppScreenshot>(app.Screenshots.Count);
            foreach (var screenshot in app.Screenshots)
            {
                if (screenshot is null) continue;
                string? url = ResolveOptionalHttpUrl(screenshot.Url);
                string? proxyUrl = ResolveOptionalHttpUrl(screenshot.ProxyUrl);
                if (url is null && proxyUrl is null) continue;
                screenshots.Add(new AppScreenshot
                {
                    Index = screenshot.Index,
                    Kind = string.Equals(screenshot.Kind, "video", StringComparison.OrdinalIgnoreCase)
                        ? "video" : "image",
                    Url = url ?? string.Empty,
                    ProxyUrl = proxyUrl,
                    VideoProxyUrl = ResolveOptionalHttpUrl(screenshot.VideoProxyUrl)
                });
            }

            app.Screenshots = screenshots.Count == 0
                ? Array.Empty<AppScreenshot>()
                : screenshots.AsReadOnly();
        }

        if (app.LatestRelease is not null)
        {
            try
            {
                var release = NormalizeRelease(app.LatestRelease, displayOnly);
                app.LatestRelease = IsReleaseUsable(release) ? release : null;
            }
            catch (JsonException)
            {
                app.LatestRelease = null;
            }
        }

        return app;
    }

    private static AppRelease NormalizeRelease(AppRelease release, bool displayOnly = false)
    {
        if (string.IsNullOrWhiteSpace(release.Id) || string.IsNullOrWhiteSpace(release.Version)
            || string.IsNullOrWhiteSpace(release.FileName) || release.Id is "." or "..")
        {
            throw new JsonException("软件版本信息缺少 ID、版本号或安装包文件名。");
        }

        release.DownloadUrl = ResolveHttpUrl(release.DownloadUrl, allowEmpty: displayOnly);
        return release;
    }

    private static string? ResolveOptionalHttpUrl(string? value)
    {
        if (string.IsNullOrWhiteSpace(value)) return null;
        try
        {
            return ResolveHttpUrl(value, allowEmpty: false);
        }
        catch (JsonException)
        {
            return null;
        }
    }

    private static string ResolveHttpUrl(string? value, bool allowEmpty)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return allowEmpty ? string.Empty : throw new JsonException("软件版本信息缺少下载地址。");
        }

        if (!Uri.TryCreate(BackendUri, value, out var uri)
            || (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
        {
            throw new JsonException("软件目录中的网址格式无效。");
        }

        return uri.AbsoluteUri;
    }

    private static string EscapeResourceId(string id)
    {
        BclCompat.ThrowIfNullOrWhiteSpace(id);
        if (id is "." or "..")
        {
            throw new ArgumentException("资源 ID 不能是路径片段。", nameof(id));
        }

        return Uri.EscapeDataString(id);
    }

    /// <summary>把 <c>api/v1/</c> 相对路径拼成当前服务端的绝对地址。</summary>
    private static Uri BuildApiUri(string path) => new(BackendUri, "api/v1/" + path.TrimStart('/'));

    /// <summary>统一来源接口路径：<c>sources/{source_id}/{relative}</c>。</summary>
    private static string SourcePath(string sourceId, string relative) =>
        $"sources/{Uri.EscapeDataString(sourceId)}/{relative.TrimStart('/')}";
}
