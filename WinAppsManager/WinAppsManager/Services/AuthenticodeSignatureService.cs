using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;

namespace WinAppsManager.Services;

/// <summary>
/// Authenticode information for a downloaded installer. Verification is informational only:
/// a missing or invalid signature is shown as a warning but never prevents the user from
/// opening the installer.
/// </summary>
public sealed record AuthenticodeSignatureInfo(
    bool HasSignature,
    bool IsValid,
    string Company,
    string Subject,
    string Issuer,
    DateTime? NotBefore,
    DateTime? NotAfter,
    string StatusText,
    string DetailText,
    string? Error);

public static class AuthenticodeSignatureService
{
    private static readonly Guid GenericVerifyV2 = new("00AAC56B-CD44-11D0-8CC2-00C04FC295EE");

    private const int WtdUiNone = 2;
    private const int WtdRevokeNone = 0;
    private const int WtdChoiceFile = 1;
    private const int WtdStateActionVerify = 1;
    private const int WtdStateActionClose = 2;
    private const int WtdRevocationCheckNone = 0x00000010;
    private const int WtdCacheOnlyUrlRetrieval = 0x00001000;

    /// <summary>Inspect a downloaded file without blocking or preventing the caller from opening it.</summary>
    public static AuthenticodeSignatureInfo Inspect(string filePath)
    {
        if (string.IsNullOrWhiteSpace(filePath) || !File.Exists(filePath))
        {
            return new AuthenticodeSignatureInfo(
                false, false, string.Empty, string.Empty, string.Empty, null, null,
                "数字签名：无法检查", "安装包文件不存在，无法读取数字签名。", "文件不存在");
        }

        X509Certificate2? certificate = TryLoadCertificate(filePath);
        int? verifyResult = TryVerifyAuthenticode(filePath);
        bool hasSignature = certificate is not null || verifyResult == 0;
        if (!hasSignature)
        {
            return new AuthenticodeSignatureInfo(
                false, false, string.Empty, string.Empty, string.Empty, null, null,
                "数字签名：未发现",
                "该安装包没有可读取的 Authenticode 数字签名，无法确认发布者；仍可打开安装包。",
                verifyResult is null ? "无法读取签名" : DescribeVerifyResult(verifyResult.Value));
        }

        string company = certificate is null ? "未知" : GetCompany(certificate);
        string subject = certificate?.Subject ?? string.Empty;
        string issuer = certificate?.Issuer ?? string.Empty;
        DateTime? notBefore = certificate?.NotBefore;
        DateTime? notAfter = certificate?.NotAfter;

        // WinVerifyTrust already validates the chain, signature digest, and timestamp; do not
        // re-check the certificate's current validity window, because a timestamped Authenticode
        // signature can remain valid after the signing certificate expires.
        bool isValid = verifyResult == 0;
        string? error = verifyResult is null
            ? "无法执行 Authenticode 校验"
            : verifyResult == 0
                ? null
                : DescribeVerifyResult(verifyResult.Value);

        string status = isValid ? "数字签名：有效" : "数字签名：校验未通过";
        string detail = certificate is null
            ? "找到签名信息，但无法读取签名证书。"
            : $"签发公司：{company} · 证书有效期：{certificate.NotBefore:yyyy-MM-dd} 至 {certificate.NotAfter:yyyy-MM-dd}";
        if (!string.IsNullOrWhiteSpace(error))
        {
            detail += $" · {error}";
        }

        if (!isValid)
        {
            detail += "。仍可打开安装包，请自行确认来源。";
        }

        return new AuthenticodeSignatureInfo(
            true, isValid, company, subject, issuer, notBefore, notAfter, status, detail, error);
    }

    private static X509Certificate2? TryLoadCertificate(string filePath)
    {
        try
        {
            X509Certificate certificate = X509Certificate.CreateFromSignedFile(filePath);
            return new X509Certificate2(certificate);
        }
        catch (CryptographicException)
        {
            return null;
        }
        catch (IOException)
        {
            return null;
        }
        catch (UnauthorizedAccessException)
        {
            return null;
        }
    }

    private static int? TryVerifyAuthenticode(string filePath)
    {
        IntPtr pathPointer = IntPtr.Zero;
        IntPtr fileInfoPointer = IntPtr.Zero;
        try
        {
            pathPointer = Marshal.StringToHGlobalUni(filePath);
            var fileInfo = new WinTrustFileInfo
            {
                Size = Marshal.SizeOf<WinTrustFileInfo>(),
                FilePath = pathPointer,
                FileHandle = IntPtr.Zero,
                KnownSubject = IntPtr.Zero
            };
            fileInfoPointer = Marshal.AllocHGlobal(fileInfo.Size);
            Marshal.StructureToPtr(fileInfo, fileInfoPointer, false);

            var data = new WinTrustData
            {
                Size = Marshal.SizeOf<WinTrustData>(),
                UiChoice = WtdUiNone,
                RevocationChecks = WtdRevokeNone,
                UnionChoice = WtdChoiceFile,
                File = fileInfoPointer,
                StateAction = WtdStateActionVerify,
                ProviderFlags = WtdRevocationCheckNone | WtdCacheOnlyUrlRetrieval
            };

            Guid actionId = GenericVerifyV2;
            int result = WinVerifyTrust(IntPtr.Zero, ref actionId, ref data);
            data.StateAction = WtdStateActionClose;
            _ = WinVerifyTrust(IntPtr.Zero, ref actionId, ref data);
            return result;
        }
        catch (Exception exception) when (exception is DllNotFoundException or EntryPointNotFoundException
            or MarshalDirectiveException or ArgumentException)
        {
            return null;
        }
        finally
        {
            if (fileInfoPointer != IntPtr.Zero)
            {
                Marshal.DestroyStructure<WinTrustFileInfo>(fileInfoPointer);
                Marshal.FreeHGlobal(fileInfoPointer);
            }

            if (pathPointer != IntPtr.Zero)
            {
                Marshal.FreeHGlobal(pathPointer);
            }
        }
    }

    private static string GetCompany(X509Certificate2 certificate)
    {
        foreach (string part in certificate.Subject.Split(','))
        {
            string trimmed = part.Trim();
            if (trimmed.StartsWith("O=", StringComparison.OrdinalIgnoreCase))
            {
                string organization = trimmed[2..].Trim().Trim('"');
                if (!string.IsNullOrWhiteSpace(organization))
                {
                    return organization;
                }
            }
        }

        string? simpleName = certificate.GetNameInfo(X509NameType.SimpleName, false);
        return string.IsNullOrWhiteSpace(simpleName) ? "未知" : simpleName.Trim();
    }

    private static string DescribeVerifyResult(int result)
    {
        return (uint)result switch
        {
            0x800B0100 => "未发现数字签名",
            0x800B0101 => "签名证书已过期",
            0x800B0109 => "证书链不受信任",
            0x800B010A => "证书链不完整",
            0x80096010 => "文件签名摘要不匹配",
            0x80096005 => "无法验证文件签名",
            0x80096004 => "数字签名格式无效",
            _ => $"校验失败（0x{result:X8}）"
        };
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct WinTrustFileInfo
    {
        public int Size;
        public IntPtr FilePath;
        public IntPtr FileHandle;
        public IntPtr KnownSubject;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct WinTrustData
    {
        public int Size;
        public IntPtr PolicyCallbackData;
        public IntPtr SipClientData;
        public int UiChoice;
        public int RevocationChecks;
        public int UnionChoice;
        public IntPtr File;
        public int StateAction;
        public IntPtr StateData;
        public IntPtr UrlReference;
        public int ProviderFlags;
        public int UiContext;
    }

    [DllImport("wintrust.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern int WinVerifyTrust(IntPtr hwnd, ref Guid actionId, ref WinTrustData data);
}
