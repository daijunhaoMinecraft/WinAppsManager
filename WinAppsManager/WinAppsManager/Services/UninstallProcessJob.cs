using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;

namespace WinAppsManager.Services;

/// <summary>
/// Windows Job Object wrapper for one started uninstaller. It keeps the whole process
/// tree visible so the runner can wait for a child that outlives the launcher and can
/// terminate every descendant when the user stops the batch. No limits are attached, so
/// closing this object never kills a process that is still running.
/// </summary>
internal sealed class UninstallProcessJob : IDisposable
{
    private const int JobObjectBasicAccountingInformation = 1;

    private IntPtr _handle;

    private UninstallProcessJob(IntPtr handle) => _handle = handle;

    /// <summary>
    /// Creates a job object and adds the started process to it. Returns <c>null</c> when
    /// the process cannot be tracked; the caller then falls back to a plain process wait.
    /// </summary>
    public static UninstallProcessJob? TryCreate(Process process)
    {
        ArgumentNullException.ThrowIfNull(process);
        if (!OperatingSystem.IsWindows())
        {
            return null;
        }

        IntPtr handle = CreateJobObject(IntPtr.Zero, null);
        if (handle == IntPtr.Zero)
        {
            return null;
        }

        var job = new UninstallProcessJob(handle);
        try
        {
            if (!AssignProcessToJobObject(handle, process.Handle))
            {
                job.Dispose();
                return null;
            }
        }
        catch (Exception exception) when (exception is Win32Exception or InvalidOperationException)
        {
            Debug.WriteLine(exception);
            job.Dispose();
            return null;
        }

        return job;
    }

    /// <summary>
    /// Number of processes still alive in the job, or <see cref="uint.MaxValue"/> when the
    /// job cannot be queried.
    /// </summary>
    public uint ActiveProcessCount()
    {
        if (_handle == IntPtr.Zero)
        {
            return uint.MaxValue;
        }

        return QueryInformationJobObject(
            _handle,
            JobObjectBasicAccountingInformation,
            out JobAccounting accounting,
            Marshal.SizeOf<JobAccounting>(),
            IntPtr.Zero)
            ? accounting.ActiveProcesses
            : uint.MaxValue;
    }

    /// <summary>Terminates every process in the job. Returns false when that is impossible.</summary>
    public bool Terminate(uint exitCode)
    {
        if (_handle == IntPtr.Zero)
        {
            return false;
        }

        return TerminateJobObject(_handle, exitCode);
    }

    public void Dispose()
    {
        if (_handle == IntPtr.Zero)
        {
            return;
        }

        CloseHandle(_handle);
        _handle = IntPtr.Zero;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JobAccounting
    {
        public long TotalUserTime;
        public long TotalKernelTime;
        public long ThisPeriodTotalUserTime;
        public long ThisPeriodTotalKernelTime;
        public uint TotalPageFaultCount;
        public uint TotalProcesses;
        public uint ActiveProcesses;
        public uint TotalTerminatedProcesses;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateJobObject(IntPtr securityAttributes, string? name);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool QueryInformationJobObject(
        IntPtr job,
        int informationClass,
        out JobAccounting information,
        int informationLength,
        IntPtr returnLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool TerminateJobObject(IntPtr job, uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CloseHandle(IntPtr handle);
}
