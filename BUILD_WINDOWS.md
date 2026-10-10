# Windows 源码运行与 RC7 本地构建

此仓库不包含运行环境或 FFmpeg binary。不要求普通下载用户安装 Python；以下命令仅供开发者。本轮未执行安装或重建。**构建成功不是获准分发。**

## 源码运行

用 64 位 CPython 3.12 在仓库根创建 `.venv`，安装 `requirements.txt`，运行 `gui.py`，具体命令见 README。程序连接微信会访问自己的账号数据与认证缓存，先阅读本机访问说明。不要为安装/源码验证导入其他人的备份。

## 固定 RC7 构建

已有 `packaging/build_windows.ps1`、`wechat_time_capsule.spec` 和版本资源直接复用，没有修改产品或打包逻辑。

要求：CPython **3.12.14 x64**；`requirements.txt` 的 wechatauto-replica **1.2.1**、PySide6 **6.11.2**；`requirements-build.txt` 的 PyInstaller **6.22.2**，匹配 Shiboken6 **6.11.2**。直接依赖固定，传递依赖没有完整 lock；新 pip 解析不能保证等于冻结 RC7，不能承诺 byte-for-byte 可重现。

先在仓库父目录的 `outputs/release-compliance/third-party-source/` 放置这两个准确源码归档及索引；它们不提交到本源码仓库：

| 文件 | SHA-256 |
|---|---|
| `qt-everywhere-src-6.11.2.tar.xz` | `6dcfbca271d76a6502741a2c0dc6fc98ef7dd0b7b4cfd0abcebb285a86a26f33` |
| `pyside-setup-everywhere-src-6.11.2.zip` | `c0fdd62b91a1d36d5ee2e1fb71050a32fbc93fcdeef0fdcb41d29afaaf00d9b5` |

准确官方 URL 见 `THIRD_PARTY_SOURCE.md`。如需下载，现有 `stage_corresponding_source.py --download` 会访问官方站点、写指定 target、检查尺寸/hash、写 `SOURCE_INDEX.txt` 和 `SHA256SUMS.txt`；不应无意启动约 1 GB 的下载。

在全新独立 checkout、确认输出位置未存在且对应源材料准备好后，可手动使用：

```powershell
# -Python312 指向自己安装的准确版本解释器；不要复制个人路径到仓库。
.\packaging\build_windows.ps1 -Python312 '<CPython 3.12.14 x64 python.exe>'
```

这是现有本地候选 recipe 的调用说明，不是本轮执行记录：

- 创建仓库内 `.venv-release-py312/`，默认安装依赖；已有并核实的环境可用 `-SkipInstall`。`-FreshVenv` 被拒绝。
- 工作文件写入 `work/release-v0.3.1-rc7/`；对其中 source/build/dist 执行限定清理。原用户档案不在这个范围。
- 成品写仓库父目录 `outputs/release/v0.3.1-rc7/`。该目标已存在时直接拒绝，不要删除已有候选来绕过保护。
- 收集安装环境的第三方声明会更新 checkout 内 `THIRD_PARTY_LICENSES/`；审阅差异，不要自动提交。现有验收按冻结环境要求 66 文件，新传递依赖可能使它失败，不要修改数量断言冒充合规。
- spec 依赖 script 提供的 `WTC_STAGE_ROOT`、版本/manifest 与工作目录，不应绕过脚本直接运行 PyInstaller。不要用新的打包命令偷偷移除 FFmpeg 或现有媒体功能。

`verify_release.ps1` 的 `VERIFY_RELEASE=PASS` 仅是当前结构/版本/hash 验证。该脚本同时保留 `FFMPEG_CORRESPONDING_SOURCE=BLOCKED`、`LICENSE_DISTRIBUTION_CHECK=BLOCKED` 和 `PUBLIC_RELEASE_READY=NO`；不得把 exit 0 当成许可清除。

## 不在本轮做的事

没有必要为源码文档准备重建已验收的 RC7。若后续选择替换 FFmpeg，必须制作有可区分版本/哈希的新候选，收齐实际对应源码与材料，执行媒体和 packaged EXE 回归，再做独立干净 Windows 验收；不能覆盖原 RC7。公共下载需最后人工批准。
