${channel_notice}## 🚀 推荐下载

### [⬇️ FluentYTDL-${version}-win64-full.7z](https://github.com/${repository}/releases/download/v${version}/FluentYTDL-${version}-win64-full.7z)

**便携完整版 — 解压即用，无需安装。**

已内置 `yt-dlp`、`FFmpeg`、`Deno`、`AtomicParsley` 和 POT Provider，解压后双击 `FluentYTDL.exe` 即可启动。

> 💡 **使用提示**
> - 需要 [7-Zip](https://www.7-zip.org/) 或 WinRAR 解压
> - 请解压到**非系统盘**的独立文件夹（例如 `D:\FluentYTDL`），避免 UAC 权限问题
> - 升级时直接解压覆盖原目录即可，配置与下载记录不受影响
> - 首次运行若被 SmartScreen 拦截，点击「更多信息」→「仍要运行」

---

<details>
<summary><b>📦 其他下载方式</b>（安装向导 / 校验文件）</summary>

<br>

**安装向导** — [`FluentYTDL-${version}-win64-setup.exe`](https://github.com/${repository}/releases/download/v${version}/FluentYTDL-${version}-win64-setup.exe)

支持简体中文和英文，默认仅为当前用户安装，也可选择为所有用户安装（需要管理员权限）。适合希望「像普通软件一样安装」的用户，功能与便携版一致。

**校验文件** — [`SHA256SUMS.txt`](https://github.com/${repository}/releases/download/v${version}/SHA256SUMS.txt)

用于验证下载完整性。PowerShell 中执行：

```powershell
Get-FileHash .\FluentYTDL-${version}-win64-full.7z -Algorithm SHA256
```

将输出的哈希值与 `SHA256SUMS.txt` 中对应行比对即可。

</details>

<details>
<summary><b>⚙️ 内部文件</b>（普通用户请忽略）</summary>

<br>

| 文件 | 用途 |
| --- | --- |
| `FluentYTDL-${version}-win64-app-core.7z` | **应用内更新包，供程序自动更新使用，请勿单独解压运行。** 不含 `bin/` 工具目录，单独解压无法运行。 |
| `update-manifest.json` | 程序内更新器读取的版本清单。 |

</details>

---

## ✨ ${version} 更新内容

${changelog}

---

## 🔄 更新方式

${update_instructions}

> 覆盖安装和应用内更新保留账号、配置与下载记录。卸载会清理账号、配置和历史，保留下载成品。

## 🐛 反馈

使用中遇到问题请提交 [Issue](https://github.com/${repository}/issues)，附上日志文件（**设置 → 打开日志目录**）可以大幅加快排查速度。

---

<sub>版本 `${version}` · 通道 `${channel}` · 构建自 [`${commit}`](https://github.com/${repository}/commit/${commit})</sub>
