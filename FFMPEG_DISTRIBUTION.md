# FFmpeg：一次有限发行选择

2026-10-10。技术材料判断，不是法律合规认证。**本源码仓库不分发 FFmpeg executable、DLL、wheel 或 Portable ZIP。** 安装上游依赖可能另行取得 native FFmpeg；仓库提供许可文本/依赖说明不等于当前 Windows RC7 可以分发。

## 当前准确事实

RC7 的唯一 native FFmpeg 为 imageio-ffmpeg 0.6.0 中 Gyan `7.1-essentials_build-www.gyan.dev`，GPL-3.0-or-later，含静态 x264/x265；SHA-256 `2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3`。它与官方 PyPI wheel、[Gyan 官方 7.1 包](https://github.com/GyanD/codexffmpeg/releases/tag/7.1)对应 entry 相同，FFmpeg core 为 `n7.1` / `b08d7969c550a804a59511c7b83f2dd8cc0499b8`。

已核实 core 源归档不等于完整静态构建的对应源码。目前缺部分外部/传递库的精确版本、补丁或无补丁证据、完整构建/安装控制脚本及所需声明。不将一个通用下载链接、LGPL标签或 imageio wrapper 的 BSD-2-Clause 当作充分材料。

## A / B / C

| 路径 | 现有功能 / 普通用户 | 材料与最短判断 | 是否重打包 |
|---|---|---|---|
| A 保留 exact Gyan 7.1 | 功能不变，用户无需配置 | 优先；只在缺项有明确取得路径时继续。目前缺项仍未取得，停止无限补抓 | 仅补准确配套材料可以不重建 binary；如包内通知需要变更，则作为新可区分候选 |
| B 换来源/材料完整的构建 | 经验证后可保留功能，无额外配置 | BtbN LGPL shared 有公开配方，但完整 CCS 未落齐、core 升到 9.0；自建最小 n7.1 LGPL 没有现成工具链/产物。两者都未证明此刻明显更短或可直接合规替换 | 必须新候选，验证 HEVC→JPEG、打包依赖、源码/许可与干净机 |
| C 不捆绑，由用户提供 | 有正确 external FFmpeg 可恢复；没有则降低开箱能力 | 不自动采用；需要额外用户说明/配置/失败验收，不能把功能减少冒充同等发行 | 必须有明确产品决定及新候选，不修改当前 RC7 |

**本轮决策：源码准备独立完成；原 RC7 FFmpeg 继续 BLOCKED，不下载/替换新 binary，也不实施 C。** Windows 下载的下一步只在 A 的完整材料能够提供或 B 的一项有限构建方案能够落实时推进，不能再次转为无期限依赖研究。

### A 的 GPLv3 §6(d) 边界

网络下载可让对应源码位于另一服务器；下载 binary 的位置必须明确指向完整对应源码、提供等效且无需额外费用的获取方式，分发者仍负责持续可得。不要求把源码塞进 EXE ZIP，但也不是指向一个普通 upstream tarball 就够。先验证 source manifest、精确依赖/patch/build 材料与可获取性，再准备下载旁的指路说明。当前不提供虚假的“完整源码链接”。原文见 `THIRD_PARTY_LICENSES/GPL-3.0.txt` 第 1 与第 6(d) 条，官方文本见 [GPLv3](https://www.gnu.org/licenses/gpl-3.0.html)。

### C 会影响什么

现有上游先检查 PATH 中的 ffmpeg，再调用 imageio 的选择；后者支持 `IMAGEIO_FFMPEG_EXE`。因此仅设置该变量不保证覆盖 PATH 已有的程序。这里是代码行为说明，不是已采用的配置流程。

- 实际调用为 HEVC 输入首帧→JPEG；不需要 x265 编码或 PNG 编码。
- 无 FFmpeg 时，WXGF/HEVC 图片的可显示恢复会受影响，普通微信照片也可能采用这种源格式；不能说“普通图片全部不受影响”。使用此路径的表情首帧也可能受影响，未证明全部表情都走此路径。
- 已取得的 JPEG/PNG/GIF 字节保存和已缓存 MP4 复制不通过 FFmpeg，不因这一依赖移除本身失效；它们仍受各自的本地来源/密钥/完整性条件约束。

[FFmpeg 官方许可说明](https://ffmpeg.org/legal.html)区分可选 GPL 部分和 exact binary/source 要求；其 LGPL linking checklist 不代替本 GPLv3 executable 的实际义务。其他第三方组件依然适用各自许可证；不自动把 Apache 应用改 GPL，也不做“全面法律风险已消除”的声明。
