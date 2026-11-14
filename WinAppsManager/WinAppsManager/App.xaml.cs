using System.Windows;
using System.Windows.Threading;
using WinAppsManager.Services;

namespace WinAppsManager;

public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        // 日志必须最先初始化：设置加载失败或启动阶段的异常也要能留下记录。
        AppLogService.Initialize();
        RegisterExceptionHandlers();
        AppSettingsService.Initialize();
        base.OnStartup(e);
    }

    protected override void OnExit(ExitEventArgs e)
    {
        AppLogService.Info("应用正在退出：关闭设置服务并刷新日志。");
        try
        {
            AppSettingsService.Shutdown();
        }
        catch (Exception exception)
        {
            AppLogService.Error("关闭设置服务时出现异常。", exception);
        }
        finally
        {
            AppLogService.Shutdown();
        }

        base.OnExit(e);
    }

    /// <summary>
    /// 未处理异常钩子只负责写日志，不改变原有崩溃行为：不设置
    /// <see cref="DispatcherUnhandledExceptionEventArgs.Handled"/>，也不吞掉任务异常。
    /// </summary>
    private void RegisterExceptionHandlers()
    {
        DispatcherUnhandledException += OnDispatcherUnhandledException;
        AppDomain.CurrentDomain.UnhandledException += OnAppDomainUnhandledException;
        TaskScheduler.UnobservedTaskException += OnUnobservedTaskException;
    }

    private void OnDispatcherUnhandledException(object sender, DispatcherUnhandledExceptionEventArgs e)
    {
        // 保持原有行为：不设置 e.Handled = true，让应用继续按原方式崩溃/上报。
        AppLogService.Error("未处理的 UI 线程异常。", e.Exception);
    }

    private static void OnAppDomainUnhandledException(object sender, UnhandledExceptionEventArgs e)
    {
        AppLogService.Error(
            $"应用程序域未处理异常（进程即将终止：{e.IsTerminating}）。",
            e.ExceptionObject as Exception);
    }

    private static void OnUnobservedTaskException(object? sender, UnobservedTaskExceptionEventArgs e)
    {
        // 保持现状：只记录，不调用 e.SetObserved()，不改变任务异常的处理行为。
        AppLogService.Error("未观察的后台任务异常。", e.Exception);
    }
}
