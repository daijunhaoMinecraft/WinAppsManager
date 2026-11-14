using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace WinAppsManager.Services;

/// <summary>
/// 兼容层：客户端同时面向 <c>net8.0-windows10.0.19041.0</c>（Win10/11 完整功能）与
/// <c>net6.0-windows7.0</c>（Windows 7 SP1 及以上）。这里集中封装 net7/net8 才有的 BCL API，
/// 保证两个目标行为一致，避免把框架差异散落到业务代码里。
/// </summary>
internal static class BclCompat
{
    /// <summary>snake_case 命名策略，替代 .NET 8 的 <c>JsonNamingPolicy.SnakeCaseLower</c>。</summary>
    public static readonly JsonNamingPolicy SnakeCaseLower = new SnakeCaseNamingPolicy();

    public static void ThrowIfNullOrWhiteSpace(string? value, string? parameterName = null)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            throw new ArgumentException("值不能为空或空白字符串。", parameterName);
        }
    }

    /// <summary>替代 .NET 7+ 的 <c>Stream.ReadExactlyAsync</c>（按字节数组读满）。</summary>
    public static async Task ReadExactlyAsync(Stream stream, byte[] buffer, CancellationToken cancellationToken)
    {
#if NET8_0_OR_GREATER
        await stream.ReadExactlyAsync(buffer, cancellationToken).ConfigureAwait(false);
#else
        var offset = 0;
        while (offset < buffer.Length)
        {
            var read = await stream.ReadAsync(buffer.AsMemory(offset), cancellationToken).ConfigureAwait(false);
            if (read == 0)
            {
                throw new EndOfStreamException("下载内容长度不足，无法完成安装包校验。");
            }

            offset += read;
        }
#endif
    }

    /// <summary>流式 SHA256（替代只有 net7+ 提供的 <c>SHA256.HashDataAsync</c>）。</summary>
    public static async Task<byte[]> Sha256Async(Stream stream, CancellationToken cancellationToken)
    {
        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        var buffer = new byte[81920];
        while (true)
        {
            var read = await stream.ReadAsync(buffer.AsMemory(), cancellationToken).ConfigureAwait(false);
            if (read == 0)
            {
                break;
            }

            hash.AppendData(buffer, 0, read);
        }

        return hash.GetHashAndReset();
    }

    /// <summary>卸载页后台回收：net8 用 Aggressive，net6 退回 Forced（同样阻塞并压缩）。</summary>
    public static void CompactCollect()
    {
#if NET8_0_OR_GREATER
        GC.Collect(GC.MaxGeneration, GCCollectionMode.Aggressive, blocking: true, compacting: true);
#else
        GC.Collect(GC.MaxGeneration, GCCollectionMode.Forced, blocking: true, compacting: true);
#endif
    }

    private sealed class SnakeCaseNamingPolicy : JsonNamingPolicy
    {
        public override string ConvertName(string name)
        {
            if (string.IsNullOrEmpty(name))
            {
                return name;
            }

            var builder = new StringBuilder(name.Length + 8);
            for (var index = 0; index < name.Length; index++)
            {
                var current = name[index];
                if (!char.IsUpper(current))
                {
                    builder.Append(current);
                    continue;
                }

                var previous = index > 0 ? name[index - 1] : '\0';
                var next = index + 1 < name.Length ? name[index + 1] : '\0';
                var needsSeparator = index > 0
                    && (char.IsLower(previous) || char.IsDigit(previous) || char.IsLower(next));
                if (needsSeparator)
                {
                    builder.Append('_');
                }

                builder.Append(char.ToLowerInvariant(current));
            }

            return builder.ToString();
        }
    }
}
