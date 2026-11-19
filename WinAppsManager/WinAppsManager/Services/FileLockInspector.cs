using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

namespace WinAppsManager.Services;

/// <summary>
/// Detects processes that keep a file open and, when safe, ends them before a forced-uninstall
/// deletion. Detection uses the Windows Restart Manager (<c>rstrtmgr.dll</c>), the same API the
/// shell uses to find "files in use"; killing is limited to normal user processes and never touches
/// the critical system set below.
/// </summary>
public static class FileLockInspector
{
    private const int ErrorSuccess = 0;
    private const int ErrorMoreData = 234;
    private const int MaxTextLength = 260;
    private const int KillWaitMilliseconds = 3000;

    private static readonly HashSet<string> ProtectedProcessNames = new(StringComparer.OrdinalIgnoreCase)
    {
        "System",
        "Idle",
        "Registry",
        "smss",
        "csrss",
        "wininit",
        "services",
        "lsass",
        "winlogon",
        "svchost",
        "fontdrvhost",
        "dwm",
        "audiodg",
        "MsMpEng",
        "SecurityHealthService",
        "SgrmBroker"
    };

    /// <summary>True when <paramref name="path"/> is a file that currently has a sharing lock.</summary>
    public static bool IsLocked(string path)
    {
        try
        {
            using var stream = new FileStream(
                path,
                FileMode.Open,
                FileAccess.Read,
                FileShare.None,
                bufferSize: 1,
                FileOptions.None);
            return false;
        }
        catch (IOException)
        {
            return true;
        }
        catch (Exception exception) when (exception is UnauthorizedAccessException or
                                              NotSupportedException or
                                              ArgumentException)
        {
            // An ACL problem is not a sharing lock; deleting will fail on its own and is reported.
            return false;
        }
    }

    /// <summary>
    /// Asks Restart Manager which processes hold <paramref name="path"/> and ends the non-critical
    /// ones. Returns a human-readable note for the cleanup result; an empty note means nothing was
    /// killed (either no holder or every holder was protected).
    /// </summary>
    public static LockReleaseResult TryRelease(string path)
    {
        var processes = GetLockingProcesses(path);
        if (processes.Count == 0)
        {
            return LockReleaseResult.Empty;
        }

        var killed = new List<string>();
        var skipped = new List<string>();
        foreach (LockingProcess process in processes)
        {
            if (process.ProcessId == Environment.ProcessId)
            {
                skipped.Add($"{process.Name}（当前程序）");
                continue;
            }

            if (process.ApplicationType == RmAppType.RmCritical ||
                ProtectedProcessNames.Contains(process.Name) ||
                process.ProcessId <= 4)
            {
                skipped.Add($"{process.Name}（系统进程）");
                continue;
            }

            try
            {
                using var running = Process.GetProcessById((int)process.ProcessId);
                running.Kill();
                if (!running.WaitForExit(KillWaitMilliseconds))
                {
                    skipped.Add($"{process.Name}（未在 {KillWaitMilliseconds / 1000} 秒内退出）");
                    continue;
                }

                killed.Add($"{process.Name} (PID {process.ProcessId})");
            }
            catch (Exception exception) when (exception is ArgumentException or
                                                  InvalidOperationException or
                                                  System.ComponentModel.Win32Exception or
                                                  NotSupportedException)
            {
                skipped.Add($"{process.Name}（结束失败：{exception.Message}）");
            }
        }

        if (killed.Count > 0)
        {
            AppLogService.Info($"已结束占用文件的进程：{string.Join("、", killed)}。");
            // Give Windows a moment to release the last handle before the caller retries.
            Thread.Sleep(250);
        }

        string note = killed.Count > 0
            ? $"已结束占用进程：{string.Join("、", killed)}"
            : string.Empty;
        if (skipped.Count > 0 && killed.Count == 0)
        {
            note = $"占用进程无法结束：{string.Join("、", skipped)}";
        }

        return new LockReleaseResult(killed, skipped, note);
    }

    private static IReadOnlyList<LockingProcess> GetLockingProcesses(string path)
    {
        if (string.IsNullOrWhiteSpace(path) || !File.Exists(path))
        {
            return Array.Empty<LockingProcess>();
        }

        uint session = 0;
        var sessionKey = new StringBuilder(256);
        int result = RmStartSession(out session, 0, sessionKey);
        if (result != ErrorSuccess)
        {
            AppLogService.Verbose($"Restart Manager 无法开始会话：0x{result:X8}。");
            return Array.Empty<LockingProcess>();
        }

        try
        {
            string[] files = [path];
            result = RmRegisterResources(session, 1, files, 0, null, 0, null);
            if (result != ErrorSuccess)
            {
                AppLogService.Verbose($"Restart Manager 无法登记文件“{path}”：0x{result:X8}。");
                return Array.Empty<LockingProcess>();
            }

            uint needed = 0;
            uint count = 0;
            uint rebootReasons;
            result = RmGetList(session, out needed, ref count, null, out rebootReasons);
            if (result == ErrorSuccess)
            {
                return Array.Empty<LockingProcess>();
            }

            if (result != ErrorMoreData || needed == 0)
            {
                AppLogService.Verbose($"Restart Manager 查询失败：0x{result:X8}。");
                return Array.Empty<LockingProcess>();
            }

            count = needed;
            var infos = new RmProcessInfo[count];
            result = RmGetList(session, out needed, ref count, infos, out rebootReasons);
            if (result != ErrorSuccess)
            {
                AppLogService.Verbose($"Restart Manager 读取进程列表失败：0x{result:X8}。");
                return Array.Empty<LockingProcess>();
            }

            var processes = new List<LockingProcess>((int)count);
            for (int index = 0; index < count; index++)
            {
                RmProcessInfo info = infos[index];
                string name = string.IsNullOrWhiteSpace(info.strAppName)
                    ? $"PID {info.Process.dwProcessId}"
                    : info.strAppName;
                processes.Add(new LockingProcess(
                    info.Process.dwProcessId,
                    name,
                    info.ApplicationType));
            }

            return processes;
        }
        finally
        {
            RmEndSession(session);
        }
    }

    private readonly record struct LockingProcess(
        uint ProcessId,
        string Name,
        RmAppType ApplicationType);

    public readonly record struct LockReleaseResult(
        IReadOnlyList<string> Killed,
        IReadOnlyList<string> Skipped,
        string Note)
    {
        public static LockReleaseResult Empty { get; } =
            new(Array.Empty<string>(), Array.Empty<string>(), string.Empty);
    }

    private enum RmAppType
    {
        RmUnknownApp = 0,
        RmMainWindow = 1,
        RmOtherWindow = 2,
        RmService = 3,
        RmExplorer = 4,
        RmConsole = 5,
        RmCritical = 1000
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct RmUniqueProcess
    {
        public uint dwProcessId;
        public FILETIME ProcessStartTime;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct RmProcessInfo
    {
        public RmUniqueProcess Process;

        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = MaxTextLength)]
        public string strAppName;

        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)]
        public string strServiceShortName;

        public RmAppType ApplicationType;
        public uint AppStatus;
        public uint TSSessionId;

        [MarshalAs(UnmanagedType.Bool)]
        public bool bRestartable;
    }

    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    private static extern int RmStartSession(
        out uint pSessionHandle,
        int dwSessionFlags,
        StringBuilder strSessionKey);

    [DllImport("rstrtmgr.dll")]
    private static extern int RmEndSession(uint pSessionHandle);

    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    private static extern int RmRegisterResources(
        uint pSessionHandle,
        uint nFiles,
        [In] string[] rgsFilenames,
        uint nApplications,
        [In] RmUniqueProcess[]? rgApplications,
        uint nServices,
        [In] string[]? rgsServiceNames);

    [DllImport("rstrtmgr.dll")]
    private static extern int RmGetList(
        uint dwSessionHandle,
        out uint pnProcInfoNeeded,
        ref uint pnProcInfo,
        [In, Out] RmProcessInfo[]? rgAffectedApps,
        out uint lpdwRebootReasons);
}
