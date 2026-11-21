using System.IO;
using System.Windows;
using System.Windows.Controls;
using Microsoft.Win32;
using WinAppsManager.Models;
using WinAppsManager.Services;

namespace WinAppsManager.Dialogs;

/// <summary>
/// “添加自启动项”对话框。启动类型可选 8 种落点（所有用户/当前用户的 Run 与 RunOnce、两个启动文件夹、
/// 登录时执行的计划任务），校验与写入都由 <see cref="StartupItemService"/> 完成；
/// 失败时不关闭窗口，直接在窗口内显示原因，方便用户改完再提交。
/// </summary>
public partial class AddStartupItemWindow : Window
{
    /// <summary>名称是否仍然跟着程序路径自动生成；用户手动改过名称后就不再覆盖。</summary>
    private bool _nameFollowsPath = true;

    /// <summary>正在由代码写入名称，避免把自动填充当成用户输入。</summary>
    private bool _suppressNameTracking;

    public AddStartupItemWindow()
    {
        InitializeComponent();
        PopulateStartTypes();
        NameBox.TextChanged += NameBox_OnTextChanged;
        UpdateHint();
        Loaded += (_, _) => PathBox.Focus();
    }

    /// <summary>写入是否成功；调用方据此决定是否刷新列表。</summary>
    public bool ResultSuccess { get; private set; }

    /// <summary>成功时的提示文案。</summary>
    public string ResultMessage { get; private set; } = string.Empty;

    /// <summary>失败时的技术细节。</summary>
    public string? ResultError { get; private set; }

    private StartupItemSource SelectedSource
        => (StartTypeCombo.SelectedItem as ComboBoxItem)?.Tag is StartupItemSource source
            ? source
            : StartupItemSource.MachineRun;

    private void PopulateStartTypes()
    {
        foreach (StartupItemSource source in StartupSourceMetadata.AddableSources)
        {
            StartTypeCombo.Items.Add(new ComboBoxItem
            {
                Content = StartupSourceMetadata.GetAddLabel(source),
                Tag = source
            });
        }

        StartTypeCombo.SelectedIndex = 0;
    }

    private void StartTypeCombo_OnSelectionChanged(object sender, SelectionChangedEventArgs args)
    {
        if (HintText is not null)
        {
            UpdateHint();
        }
    }

    private void UpdateHint()
    {
        if (HintText is null)
        {
            return;
        }

        StartupItemSource source = SelectedSource;
        string name = NameBox is null ? string.Empty : NameBox.Text.Trim();
        if (name.Length == 0)
        {
            name = "名称";
        }

        string location = StartupSourceMetadata.DescribeRegistrationLocation(source, name);
        HintText.Text = StartupSourceMetadata.GetKind(source) switch
        {
            StartupItemKind.StartupFolder => $"{location}{Environment.NewLine}登录后由资源管理器运行这个快捷方式。",
            StartupItemKind.ScheduledTask => $"{location}{Environment.NewLine}登录时触发，可在任务计划程序里查看和修改。",
            _ => location
        };
    }

    private void PathBox_OnTextChanged(object sender, TextChangedEventArgs args) => HideError();

    private void PathBox_OnLostFocus(object sender, RoutedEventArgs args) => SuggestNameFromPath();

    private void NameBox_OnTextChanged(object sender, TextChangedEventArgs args)
    {
        if (!_suppressNameTracking)
        {
            _nameFollowsPath = false;
        }

        UpdateHint();
    }

    /// <summary>名称默认取程序的“文件说明”（与列表里显示的一致），取不到时用文件名。</summary>
    private void SuggestNameFromPath()
    {
        if (!_nameFollowsPath)
        {
            return;
        }

        string path = PathBox.Text.Trim().Trim('"');
        if (path.Length == 0)
        {
            return;
        }

        ExecutableMetadata metadata = ExecutableMetadataService.Read(path);
        string suggested = metadata.FileDescription.Length > 0
            ? metadata.FileDescription
            : Path.GetFileNameWithoutExtension(path);
        if (suggested.Length == 0)
        {
            return;
        }

        _suppressNameTracking = true;
        try
        {
            NameBox.Text = suggested;
        }
        finally
        {
            _suppressNameTracking = false;
        }

        UpdateHint();
    }

    private void BrowseButton_OnClick(object sender, RoutedEventArgs args)
    {
        var dialog = new OpenFileDialog
        {
            Title = "选择要开机自启动的程序",
            Filter = "程序 (*.exe)|*.exe|所有文件 (*.*)|*.*",
            CheckFileExists = true,
            Multiselect = false
        };

        if (dialog.ShowDialog(this) != true)
        {
            return;
        }

        PathBox.Text = dialog.FileName;
        SuggestNameFromPath();
    }

    private async void ConfirmButton_OnClick(object sender, RoutedEventArgs args)
    {
        HideError();
        ConfirmButton.IsEnabled = false;
        try
        {
            StartupOperationResult result = await StartupItemService.AddAsync(
                SelectedSource,
                NameBox.Text,
                PathBox.Text,
                ArgumentsBox.Text);

            if (!result.Success)
            {
                ResultError = result.Error;
                ShowError(result.Error is null ? result.Message : $"{result.Message} {result.Error}");
                return;
            }

            ResultSuccess = true;
            ResultMessage = result.Message;
            DialogResult = true;
        }
        finally
        {
            ConfirmButton.IsEnabled = true;
        }
    }

    private void CancelButton_OnClick(object sender, RoutedEventArgs args) => DialogResult = false;

    private void ShowError(string message)
    {
        ErrorText.Text = message;
        ErrorPanel.Visibility = Visibility.Visible;
    }

    private void HideError()
    {
        if (ErrorPanel is null)
        {
            return;
        }

        ErrorText.Text = string.Empty;
        ErrorPanel.Visibility = Visibility.Collapsed;
    }
}
