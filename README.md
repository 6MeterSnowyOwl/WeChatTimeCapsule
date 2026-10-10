# 微信时间胶囊

> v0.3.1 RC7 · 源码候选版本 · 非腾讯或微信官方软件

**当前公开的是源码。Windows 下载包仍在准备中，尚未通过 FFmpeg 分发合规及独立干净 Windows 验收。RC7 不是已经公开发布的稳定版本。**

此源码集合不包含 FFmpeg 可执行文件、wheel、Windows Portable 包、真实聊天或私人运行证据。源码与尚未获准分发的 Windows RC7 二进制分开审核。当前 Windows 包仍为 `FFMPEG_CORRESPONDING_SOURCE=BLOCKED`、`LICENSE_DISTRIBUTION_CHECK=BLOCKED`、`CLEAN_MACHINE_TEST=NOT_RUN`，因此 `PUBLIC_RELEASE_READY=NO`。

源码运行见下文；固定 Windows 构建与副作用见 [BUILD_WINDOWS.md](BUILD_WINDOWS.md)，FFmpeg 三种有限选择见 [FFMPEG_DISTRIBUTION.md](FFMPEG_DISTRIBUTION.md)，独立电脑测试步骤见 [CLEAN_WINDOWS_TEST.md](CLEAN_WINDOWS_TEST.md)。

把你明确选择的一对一微信聊天保存为普通 Markdown 和本地媒体。每个联系人始终只有一份持续更新、可直接打开的“当前时间胶囊”；快速更新与完整核对都会更新同一路径，不再为日常操作新增版本副本。

所有处理均在本机进行。程序不会发送微信消息、修改微信数据库、操作微信界面、上传聊天内容或联网补抓头像与链接内容。

## 当前文件模型

```text
exports/<联系人显示名>/
├── chat.md
├── capsule_meta.json
├── assets/
│   ├── avatars/
│   │   ├── me.*
│   │   ├── contact.*
│   │   ├── avatar_history.json
│   │   └── history/contact/
│   ├── images/
│   └── videos/
├── integrity/current.json
├── state/
├── state_backups/
├── reports/
├── target.json
└── archive_state.json
```

`chat.md`、`capsule_meta.json`、`assets/` 和 `integrity/current.json` 共同构成当前胶囊。写入先在 staging 中完整渲染并验证，再通过带恢复日志的事务安装；若中途失败，上一份可读胶囊会恢复。`state/assets/` 保留媒体的 durable 副本；保存前仍会严格验证当前资源和完整性，不承诺缺失资源自动重建。

v0.2.x 已有的 `snapshots/`、`snapshot_index.json` 和旧 integrity manifests 原样保留，迁移、快速更新与完整核对都不会重写或追加它们。它们仅作为兼容历史存在，普通 GUI 不显示快照列表或单快照删除入口。本版可把对象从列表移除而不删除任何档案，仍不提供“彻底删除整个备份对象”。

## GUI

```powershell
.\.venv\Scripts\python.exe gui.py
```

主窗口是单页手风琴列表：每个对象默认显示头像、名称、完整性状态、消息数和紧凑更新时间；一次只展开一个对象。展开后可以：

- 快速保存：只读取各分片游标后的新消息和边界重叠；
- 完整核对并保存：重新扫描所选会话在全部当前可读分片中的记录，补找遗漏旧消息；
- 打开根目录下的 `chat.md` 或联系人目录；
- 查看联系人头像历史；
- 完全离线验证当前胶囊。

未保存对象显示灰色空心圆，不伪造消息数量；完整胶囊显示绿色勾，校验异常显示红色叉，缺少基线的兼容状态显示黄色警告。完整性图标与联系人名称位于同一行，并提供文字 tooltip。

首次启动包含三页本地隐私说明，并使用 Windows/Qt Documents 下的 `微信时间胶囊` 作为新用户默认保存位置。多微信账号时必须明确选择，界面只显示掩码标识。耗时操作运行在线程池中，同一对象的写操作由跨进程文件锁保护。

首次使用前应了解：历史读取会对当前账号的数据库与附件目录进行账号级清点，数据库解密密钥来自当前微信进程内存；必要的本地缓存、密钥资料和已保存聊天应视为私密数据。请只对自己的账号使用。当前适用腾讯中国大陆服务条款风险为 `UNKNOWN`，该状态不表示已经确认许可或违规。

## 头像历史语义

只为联系人保存头像历史，不为本人头像建立历史：

- 每次更新捕获到的联系人头像按 SHA-256 去重；
- 内容未变化时不会新增记录；
- 内容变化时新增一项，并标记唯一的当前头像；
- 迁移时从旧 `snapshots/*/assets/avatars/contact.*` 导入唯一内容；
- `first_observed_at` 表示本软件首次观察到该头像的时间，不声称是联系人实际更换头像的时间。

头像来源依次为可严格匹配的本地缓存、本机头像数据库 BLOB、用户手动指定的本地图片。数据库中的远程 URL 不会被下载或写入 Markdown/普通日志。头像缺失时 Markdown 使用文字标题，不产生损坏链接。

## 支持的消息类型

可保存：文字、图片、引用/回复、系统消息、位置、联系人名片，以及本地可恢复的 MP4 视频；具体恢复范围取决于当前可读记录和本地附件。

部分支持：网页/公众号链接、文件卡片、小程序卡片。只保存消息内已有的可读字段，不下载网页正文、文件正文、文章、视频站内容或小程序页面。

暂未完整支持：语音、动画表情、拍一拍和无法识别的卡片 subtype。它们显示人类可读占位，不暴露技术 type number、消息 ID 或私人原始 payload，也不会阻断其他消息。

既有历史验收样本包含 181 条占位、5 条部分支持及 1439 处表示差异。表示差异尚未全部解释，不能自动判为损坏，也不能据此宣称全部附件已恢复。这些是历史样本的聚合记录，不是本轮 RC7 新增真实账号采集或版本验收。

一对一联系人的显示名由选择界面与 Markdown 共用同一个 resolver：备注 `remark` → 昵称 `nick_name` → 微信号 `alias` → 内部 `username/wxid`。消息表中的数字 sender id 不作为显示名。

## 当前支持环境

- Windows 11 已在开发机测试；Windows 10 尚未完成实机验证
- 已启动并登录的 Windows 微信；程序启动时会检查当前微信的基础本地读取能力，检查通过后即可使用
- 源码推荐 64 位 CPython 3.12；RC7 固定构建要求 3.12.14 x64，其他 Python 版本未在此预备集合中验证
- `wechatauto-replica==1.2.1`

微信客户端未来的大版本更新可能改变本地数据结构，届时可能需要更新微信时间胶囊。
WeChat 4.1.13.63、4.1.13.65 是历史已测试版本，本轮没有重新执行真实微信验收；其他 4.x build 仍由运行时只读 Probe 判定，不能据此声称所有 4.x 都已测试。Windows 11 开发机验证不能替代独立无 Python 干净机器验收，`CLEAN_MACHINE_TEST=NOT_RUN`。

## 安装与运行

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe gui.py
```

以上仅供源码开发者；安装会从上游取得依赖（可能包括 imageio-ffmpeg wheel 内的 native FFmpeg）。`requirements.txt` 固定直接依赖，不是全部传递依赖 lockfile，不能保证新解析的环境与 RC7 逐字节相同。不要将安装结果、虚拟环境或缓存提交到仓库。

Portable RC 使用 PyInstaller onedir 形式，不需要安装 Python；但当前下载包尚未获准公开分发。CLI 入口为 `.\.venv\Scripts\python.exe main.py`，与 GUI 使用同一当前胶囊核心。

## 许可证

本项目自身源代码采用 Apache-2.0 许可证；第三方组件继续受各自许可证约束，完整文本见 `THIRD_PARTY_LICENSES/`。
对应的 Qt/PySide 源码归档与 binary 分开提供，详见 `THIRD_PARTY_SOURCE.md`。源码归档不是应用运行依赖。
RC7 打包排除未使用且 GPL-only 的 Qt VirtualKeyboard，保留原生 Windows 输入插件。QtPdf/PDFium 等声明由已核验 Qt 源归档提取到独立的 `Qt-6.11.2-license-notices.zip` 配套材料；该归档涵盖全部源模块，不能把声明数量当作实际交付模块数量。

当前已构建 RC7 的 HEVC 图片解码使用 imageio-ffmpeg 内附的 Gyan FFmpeg 7.1（GPL-3.0-or-later，包含静态 x264/x265）。此源码仓库**不分发那个原生二进制**。其精确来源和 core commit 已确认，但完整静态依赖、补丁和构建材料尚缺；Python wrapper 的 BSD-2-Clause 与 Qt/PySide 源码均不替代该缺项。Windows binary gate 仍为 `FFMPEG_CORRESPONDING_SOURCE=BLOCKED` / `LICENSE_DISTRIBUTION_CHECK=BLOCKED`。具体边界见 [FFMPEG_DISTRIBUTION.md](FFMPEG_DISTRIBUTION.md)。

RC7 未进行代码签名，Windows 可能显示未知发布者或 reputation 提示；请保留正常 Windows 安全设置。

归档保持账号与会话范围绑定，并记录各数据分片的独立游标。快速保存读取游标后的新消息及重叠窗口，完整核对重新扫描当前可读记录。消息合并使用已有 canonical identity，而不把跨分片重复的 local ID 当成同一消息。状态缺失、不一致或身份冲突时停止并报告；不会把微信中已删除的消息自动从既有档案删除。

## 完整性与恢复

`integrity/current.json` 记录当前 `chat.md`、`capsule_meta.json` 及全部 `assets/` 的大小和 SHA-256。验证器检查必需文件、空文件、元数据 schema、本地资源引用、路径逃逸、manifest 缺失/多余文件与 hash mismatch。发现异常只报告，不自动修改文件。

事务日志在启动时恢复未提交的 current capsule；已提交但尚未清理的日志只做清理。旧 snapshots 的验证与删除实现仍留在兼容层，但不暴露给日常 GUI。

## 日志与隐私

日志只记录数量、阶段和异常类型，不记录正文、联系人身份、数据库密钥、图片密钥或内存地址。GUI 诊断 ZIP 只含应用/环境/兼容性与聚合完整性信息，并执行敏感信息自检；不会自动上传。

`chat.md` 和 `state/messages.jsonl` 含私人数据。`.gitignore` 排除常见备份/输出、日志、数据库、密钥缓存、虚拟环境及 native binary；它不能移除已经被 Git 跟踪的文件，也不能替代提交前的明确文件清单审核。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试包含多分片、canonical identity、archive-first、媒体保全、事务恢复、GUI 保存链路与隐私等合成场景。冻结 RC6 的旧 writer 测试保留两个已知失败子项，全量发现不能称为全绿。这里只包含已审核的公开测试，三个本机私人验收 helper 与八个相关测试未纳入仓库；它们不是主 GUI 的产品依赖。

## 当前边界

- “完整历史”只指本机数据库当前仍可读取的记录；已删除、未落地或不可解密的内容无法恢复。
- 本机共享头像缓存没有公开稳定的 username 映射；无法严格证明归属的头像不会猜配。
- 未实现整个账号批量导出、自动遍历联系人或恢复到微信。
- 项目自身采用 Apache-2.0；第三方许可与对应源码说明见 `THIRD_PARTY_LICENSES.txt` 和 `THIRD_PARTY_SOURCE.md`。
