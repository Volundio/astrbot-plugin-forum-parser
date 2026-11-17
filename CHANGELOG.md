# Changelog

All notable changes to `astrbot_plugin_forum_parser` will be documented in this file.

## [v0.4.0] - 2026-11-18

### Added
- **新增 Bangumi 番组计划（`bangumi.tv` / `bgm.tv` / `chii.in`）解析支持**：
  - 支持识别并解析三大等价域名的**小组讨论帖**（`/group/topic/...`、`/rakuen/topic/group/...`）、**条目讨论帖**（`/subject/topic/...`）以及**日志长评**（`/blog/...`），同步支持 `#post_...` 指定回复/楼中楼楼层精准解析（自动附带父楼层引用语境），并自动忽略普通条目详情页（如 `/subject/123`）。
  - 严格限定平台作用域：**仅在 QQ OneBot (`aiocqhttp`) 平台触发解析 Bangumi 链接，对 Telegram 平台收到的 Bangumi 链接完全不解析**。
  - 集成主网页 DOM 提取 + `next.bgm.tv/p1` 公开接口兜底（含 Bangumi BBCode 及 `(bgmXX)` 表情完整转换），完整提取帖子正文、作者署名、预估阅读时长、`[mask]` 剧透条、贴贴表态分布（`data_likes_list`）与关联作品条目封面卡片。
- **专属 Bangumi 渲染卡片模板（`core/templates/bangumi_card.html`）**：
  - 采用 Bangumi 经典樱花粉（`#F09199`）主题配色并支持暗色/极简浅色切换，专设小组徽标、阅读时长胶囊、贴贴表情计数栏与 2 列关联作品条目卡片区。

### Fixed
- **彻底修复 QQ 渲染长图中 S1 附件裂图但合并转发单独能发出（如 `thread-2294225`）的问题**：
  - 打通卡片内联与合并转发下载管线为**单次流式落盘复用**：正文配图独享通道率先下载（放宽跨境超时至 `25s`、`sock_read=12s`、最多 3 次新连接重试），不再与小表情争抢连接；卡片内联上限提升至单图 `6MB` / 合计 `12MB`，超出内联上限但在 `10MB` 内的防盗链图片自动转存至 `p.sda1.dev` 供远程渲染器绘制；已落盘原图直接复用于合并转发，彻底消除二次下载与条件不一致。
  - 修复 Discuz 动态附件接口返回非标准 `Content-Type: image` 或字节流时的预检误杀，改为基于二进制魔数校验；放宽 `.stage1st.com` 全子域 Cookie 匹配。
  - 移除 `.forum-img` 的 `max-height: 1100px` 限高裁切，竖长截图不再在卡片中被缩窄变形；含下载失败配图的结果不再写入轻量缓存。

## [v0.3.2] - 2026-11-18

### Fixed
- **Stage1st 论坛附件图片提取与跨境转存修复**：
  - 优化 Discuz `<img>` 属性候选提取策略（优先保留带鉴权签名参数的链接及懒加载真实 `file` 地址，同时记录 `data-fallback-src` 在主地址返回 `403/404` 时自动回退尝试备选地址）。
  - 为图片流式下载与内联预取补齐跨境短超时与 `Connection: close` 新连接自动重试（最多 3 次），并在拉取或上传图床失败时通过 `logger.warning` 输出明确状态码与原因。
- **Telegraph 多图并发加载断流修复（`p.sda1.dev` 边缘缓存串行预热 + 免重传复用）**：
  - 原帖已托管在 `p.sda1.dev`、`telegra.ph` 等公开免防盗链图床上的图片直接复用原链接，避免无谓重复上传导致热缓存旧链接退化为冷缓存新链接。
  - 在向 `telegra.ph` 发布页面前，按阅读顺序逐张串行 `GET` 预热图床外链的 Cloudflare 边缘缓存（64KB 分块流式丢弃、零磁盘占用、自动处理首次冷回源断流重试），消除客户端打开页面并发拉取多张冷缓存图片时触发的源站断流（`ERR_CONTENT_LENGTH_MISMATCH`）。

## [v0.3.1] - 2026-11-18

### Fixed
- **日志规范合规修复**：将 `core/` 下所有运行时子模块（`renderer.py`、`temp_manager.py`、`telegraph_publisher.py`、`parsers/stage1st.py`、`parsers/nga.py`）统一改为 `from astrbot.api import logger`，彻底移除内置 `logging` 导入，严格符合 AstrBot 插件市场审核规范并统一插件级日志路由。

## [v0.3.0] - 2026-11-18

### Added
- **Telegram `telegra.ph` 即时预览（Instant View）发布支持**：
  - Telegram 平台默认改为发布解析后的帖子（主楼或指定楼层）至 `telegra.ph`，仅返回干净的 `telegra.ph` 链接以触发原生 ⚡ Instant View 阅读体验。
  - **配图自动流式转存 `p.sda1.dev`**：帖子配图携带论坛登录态与防盗链请求头流式下载至临时作用域，通过 `POST https://p.sda1.dev/api/v1/upload_external_noform?filename=...`（Binary 请求体）转存为无防盗链 HTTPS 直链写入 Telegraph，单张传完即刻删除临时文件，防止内存堆积与磁盘残留。
  - **富文本语义映射与超长帖自动分页**：完整适配引用块、删除线、反白剧透提示、展开折叠块、表格转列表及 S1 评分记录；超出 Telegraph 单页 64KB 限制时自动切分多页并生成下一页续读链接；自动创建并持久化复用 `telegraph_access_token`。
  - **渲染长图自动兜底与模式可配**：新增 `telegram_options` 配置分组，当 `telegra.ph` 偶发超时失败时自动回退为现有的自定义 HTML 模板渲染长图（Send as File）发送，也支持在设置中手动切换回固定长图模式。

## [v0.2.1] - 2026-11-17

### Improved
- **NGA 单回复楼层真实楼层号还原优化**：
  - 针对 NGA `read.php?pid=...&opt=128` 纯 `pid` 链接，自动通过 `to=1` 302 跳转反查真实页码并获取准确的楼层号（如 `41楼`、`45楼`），避免 NGA 单条 `pid` 接口将 `lou` 固定置为 `0` 导致楼层号丢失；对 `read.php?tid=...&page=...#pid...Anchor` 锚点链接直接单次请求目标页提取真实楼层号与用户组名称。

## [v0.2.0] - 2026-11-17

### Added
- **指定楼层链接精准解析（不误退回主楼）**：
  - **Stage1st**：支持识别并解析指向特定楼层的链接（包括 `forum.php?mod=redirect&goto=findpost&ptid=...&pid=...`、省略 `ptid` 的 `findpost&pid=...`、以及带 `#pid...` / `#post_...` 锚点的链接），精准提取目标楼层内容、楼层号（如 `239楼`）与本层评分，并且当目标楼层作者被禁言屏蔽或审核中时同样支持通过 S1 官方 App 接口精准解锁该楼层。
  - **NGA**：同步支持识别并解析 NGA 单条回复/指定楼层链接（`read.php?pid=...`、`read.php?tid=...&pid=...`、`#pid...Anchor` 及 `#l<楼层>`）。
  - 渲染卡片顶栏与发言人徽章、OneBot v11 合并转发节点名称、Telegram 文件名、同帖消息去重、冷却追踪与解析缓存均已适配同帖不同楼层独立区分。

## [v0.1.3] - 2026-11-17

### Added
- 新增标准插件更新日志文件 `CHANGELOG.md`，适配 AstrBot WebUI 插件更新日志面板展示。

### Improved
- **跨境直连网络抗抖动优化（香港/海外服务器连接内地源站）**：
  - 将 Stage1st 与 NGA 请求超时细化为短握手/短读取超时（`connect=4s, sock_read=8s`），遇到跨境晚高峰丢包或陈旧 Keep-Alive 死连接卡住时，自动附带 `Connection: close` 切换全新 TCP 连接快速重试，避免单次死等 15 秒超时。
  - Stage1st 官方 App 接口 `/thread` 与 `/thread/page` 改为 `asyncio.gather` 并发请求，并将 `pageSize` 从 `40` 精简为 `2`（仅拉取主楼），大幅减小跨境响应包体积与耗时。
  - 优化 `TCPConnector` 连接池参数（缩短空闲保活时长并启用 `enable_cleanup_closed`）。

### Fixed
- 修复 `asyncio.TimeoutError` 转字符串为空导致日志中异常原因显示空白的问题。
- 修复 Stage1st 网页端与 App 接口同时失败时报错文案仅显示网页错误、未展示 App 接口真实错误（如阅读权限不足或接口超时）的问题。
- 修复仅因网页端网络超时而走 App 接口容灾恢复普通帖子时，卡片顶栏误显示“已解锁不可见内容”徽章的问题。

## [v0.1.2] - 2026-11-17

### Changed
- **Telegram 无损完整长图推送**：在 Telegram 平台下统一仅推送自定义模板渲染的主楼完整长图，并固定以 `Comp.File`（Send as File / `send_document`）原图文件方式发送，避免 `sendPhoto` 压缩模糊与 `10000px` 尺寸上限。
- 默认关闭 `enable_length_limit`（超长主楼截断保护）并移除旧的 `9600px` 高度裁剪限制，默认渲染整栋主楼完整长图。
- **图片体积预算上调**：
  - 渲染卡片内嵌图片上限上调为 **单图 `5MB`、整帖合计 `10MB`**（改为按原始图片二进制字节精确累计，不受 Base64 编码膨胀影响）。
  - OneBot v11 合并转发图片上限上调为 **单图 `10MB`、整包本地图片总预算 `50MB`**。
  - 完整渲染长图流式接收上限同步上调至 `35MB`，临时目录巡检磁盘总配额上调至 `160MB`。

## [v0.1.1] - 2026-11-17

### Fixed
- **图床与接口旧域名迁移**：将已停用的 NGA 旧图床 `img.nga.178.com` 与 S1 旧域名 `*.saraba1st.com` 自动重写至现役 `img.nga.cn` 与 `*.stage1st.com`。
- **OneBot v11 合并转发稳定性加固**：合并转发原图节点仅传入本地校验落盘成功的文件路径，源站失效或超限配图自动跳过并附纯文本说明，防止触发 AstrBot `RespondStage` 未捕获的下载异常导致整条转发失败。

## [v0.1.0] - 2026-11-17

### Added
- 初始版本发布，支持 **OneBot v11 (`aiocqhttp`)** 与 **Telegram (`telegram`)** 平台自动识别并解析论坛帖子主楼：
  - 支持 **Stage1st (`stage1st.com`)**：支持配置账号密码（含安全提问）登录，参考社区脚本通过官方 App 接口解锁“内容审核中”与“作者被禁止或删除 内容自动屏蔽”不可见内容。
  - 支持 **NGA (`bbs.nga.cn`)**：支持配置 `ngaPassportUid` + `ngaPassportCid` 或 Cookie，支持 `read.php` 与移动端 `app_api.php` 双通道解析及完整 BBCode 转 HTML 引擎。
  - 支持提取纯文本链接、同源别名域名、QQ 小程序/JSON 分享卡片及 Telegram `text_link` 超链接实体。
  - 内置专属多主题 `HTML + Jinja2` 渲染卡片模板（论坛特色自适应 / 现代浅色 / 现代深色），支持自定义远程 `t2i_endpoint`。
  - OneBot v11 默认使用合并转发一并发送帖子摘要、主楼渲染长图与帖子配图。
  - 内置流式落盘防 OOM 保护与发送完成即刻清理临时文件机制（零垃圾残留）。
