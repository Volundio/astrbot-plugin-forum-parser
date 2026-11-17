# Changelog

All notable changes to `astrbot_plugin_forum_parser` will be documented in this file.

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
