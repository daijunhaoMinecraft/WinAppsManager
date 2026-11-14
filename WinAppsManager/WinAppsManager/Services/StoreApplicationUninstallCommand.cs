using System.Text;

namespace WinAppsManager.Services;

/// <summary>
/// Builds the Appx removal command:
/// <c>Get-AppxPackage -name &lt;identity&gt; | Remove-AppxPackage</c> for the current user.
/// Passing the package identity (not the full name) removes every registered version of the
/// package, and leaving out <c>-PreserveApplicationData</c> also clears its local data.
/// </summary>
public static class StoreApplicationUninstallCommand
{
    public static string Build(string packageName)
    {
        if (string.IsNullOrWhiteSpace(packageName))
        {
            throw new ArgumentException("包名称不能为空。", nameof(packageName));
        }

        if (packageName.Any(char.IsControl) || packageName.Contains('"') || packageName.Contains('\''))
        {
            throw new ArgumentException("包名称包含无效字符。", nameof(packageName));
        }

        var command = new StringBuilder();
        command.Append("powershell.exe -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -Command \"");
        command.Append("try { Get-AppxPackage -Name '");
        command.Append(packageName);
        command.Append("' -ErrorAction Stop | Remove-AppxPackage -Confirm:$false -ErrorAction Stop } ");
        command.Append("catch { Write-Error $_.Exception.Message; exit 1 }\"");
        return command.ToString();
    }
}
