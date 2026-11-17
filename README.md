# astrbot_plugin_forum_parser

适用于 [AstrBot](https://docs.astrbot.app/) 的论坛帖子主楼自动解析与图片渲染插件。

当用户在群聊或私聊中发送受支持的论坛帖子链接（或分享卡片）时，插件会自动提取**帖子主楼（1L）完整内容**，使用专门设计的自定义多主题 HTML 模板，通过 AstrBot 远程 HTML 渲染服务（`remote` / `astrbot-t2i-service`）渲染为高清长图并发送给用户。

## 功能特性

- **双论坛深度支持**
  - **Stage1st (`https://stage1st.com/2b/`)**：兼容 `saraba1st.com` 等官方同源域名；支持直接配置**账号 + 密码（及可选安全提问）**自动维护网页登录态；参考社区脚本实现**官方 App 接口不可见内容解锁**（自动获取“内容审核中，即将开放”及“作者被禁止或删除 内容自动屏蔽”的主楼原文，并在 `sid` 过期时自动重登刷新）；自动过滤 Discuz 防复制干扰码并展开反白剧透文字与评分记录。
  - **NGA 玩家社区 (`https://bbs.nga.cn/`)**：兼容 `ngabbs.com`、`nga.178.com`、`g.nga.cn` 等同源域名；支持配置 `ngaPassportUid` + `ngaPassportCid` 或完整 Cookie 字符串；采用 `read.php?__output=11` 与移动端 `app_api.php` 双通道容灾解析；内置完整的 NGA BBCode 转语义化 HTML 引擎（自动展开 `[collapse]` 折叠块、转换引用、表格、列表、删除线、AC娘表情标签及未显式插入正文的尾部图片附件）。
- **多平台与富媒体链接提取**
  - 支持 **OneBot v11 (`aiocqhttp`)** 与 **Telegram (`telegram`)** 平台。
  - **OneBot v11 默认合并转发**：在 QQ 平台默认以合并转发消息形式发送（包含帖子标题与原帖链接、主楼完整渲染长图，以及帖子主楼内的所有配图/GIF 原图节点，可在设置中调整）。
  - **Telegram 无损完整长图推送**：在 Telegram 平台仅推送自定义模板渲染生成的单张主楼完整长图，并固定采用原图文件方式（Send as File / `send_document`）推送，避免 `sendPhoto` 压缩模糊及 `10000px` 高度限制。
  - 除纯文本 URL 外，自动提取 QQ 小程序 / JSON 分享卡片（含 `c.pc.qq.com` 嵌套跳转解包）及 Telegram 消息中的超链接实体（`text_link`）。
- **专属自定义 HTML 渲染模板**
  - 不使用默认纯文本文转图模板，内置专为论坛主楼阅读排版的 `HTML + Jinja2` 卡片模板。
  - 支持 **论坛特色自适应主题**（Stage1st 经典暖鹅黄 `#F6F7EB` / NGA 经典泥潭浅黄 `#FFF9EB`）、**现代极简浅色**、**现代柔和深色** 三种视觉模式。
  - 主楼图片由插件携带论坛登录态与防盗链 `Referer` 异步预下载内嵌，解决海外/容器化远程渲染节点无法加载防盗链或权限附件图片的问题。
- **可配置远程 T2I 服务与平台尺寸自适应**
  - 支持在插件设置中单独配置 `t2i_endpoint`（如自建 `soulter/astrbot-t2i-service` 地址），留空则自动复用 AstrBot 全局远程渲染配置。
  - 默认完整渲染整栋主楼长图，同时提供可选的超长主楼字数与高度截断开关。
- **防内存溢出（OOM）与临时文件即时清理机制**
  - **流式分块落盘代替常驻内存**：渲染长图与 OneBot11 合并转发的帖子原图均通过 64KB 流式分块直写请求级临时目录（`TempScope`），不将多张大图同时堆叠在 Python 内存中；内存解析缓存严格限制为仅缓存 `<=128KB` 的纯文本轻量结果（总上限 `1MB`），渲染完成后立即释放重型 HTML 字符串引用。
  - **多层体积上限与并发背压保护**：所有 HTTP 响应均带 `Content-Length` 预检与分块读取硬上限（网页/JSON 5MB、渲染卡片内嵌图片单图上限 5MB / 整帖合计上限 10MB、OneBot11 合并转发单图上限 10MB / 整包本地图片总预算 50MB——超出预算或源站失效的配图自动安全跳过并附上纯文本提示，防止下游 `Node.to_dict()` 异常中断发送）；内置全局并发信号量 `max_concurrent_tasks`（默认 `2`）防止群聊刷屏引发并发内存峰值叠加。
  - **发送即刻销毁（双重钩子保障）**：渲染长图、合并转发落盘配图及 AstrBot 内置 `html_renderer` 生成在 `data/temp/io_temp_img_*` 的文件均绑定至 `TempScope`，在消息发送完成瞬间通过 `@filter.after_message_sent` 钩子与生成器 `finally` 块整目录销毁，空目录自动移除。
  - **启动自检、定时配额巡检与停用清空**：插件启动时自动清扫崩溃遗留文件与 `.tmp` 半成品；运行期每 60 秒执行 TTL 超时与磁盘总字节配额（160MB）淘汰；插件停用/重载（`terminate`）时全量清空。

## 安装方式

将本仓库克隆至 AstrBot 的 `data/plugins/` 目录下（或在 AstrBot WebUI 插件市场通过仓库地址安装）：

```bash
cd AstrBot/data/plugins
git clone https://github.com/Volundio/astrbot-plugin-forum-parser.git astrbot_plugin_forum_parser
```

随后在 AstrBot WebUI **插件** 页面重载或启用插件即可。

## 配置说明

在 AstrBot WebUI 打开本插件的 **配置** 面板即可可视化设置：

| 配置分组 | 关键配置项 | 说明 |
| --- | --- | --- |
| 顶层配置 | `t2i_endpoint` | 文本转图像服务 API 地址（如 `http://127.0.0.1:8999`）。留空则使用 AstrBot 全局远程渲染服务。 |
| `render_options` | `theme` / `viewport_width` / `device_scale_factor` / `embed_images` / `enable_length_limit` | 控制卡片配色主题、宽度、清晰度倍率、主楼图片内嵌数量上限以及超长帖截断保护。 |
| `parse_options` | `allow_alias_domains` / `parse_share_cards` / `cooldown_seconds` | 控制同源别名域名开关、QQ JSON 卡片/TG 超链接提取以及同帖重复触发冷却时间。 |
| `onebot_options` | `use_forward` / `forward_include_images` / `forward_max_images` | 控制 QQ (OneBot11) 默认合并转发开关、是否在合并转发中一并附带帖子原图及原图数量上限。 |
| `stage1st` | `username` / `password` / `question_id` / `answer` / `enable_invisible_parse` | 配置 S1 账号密码（可选安全提问、Cookie 或 `app_sid` 兜底），开启后自动解锁审核中与禁言屏蔽内容。 |
| `nga` | `passport_uid` / `passport_cid` / `cookie` | 配置浏览器登录 NGA 后的 `ngaPassportUid` 与 `ngaPassportCid`（或完整 Cookie 字符串）。 |

## 使用方法

1. **自动触发**：在已启用的 OneBot v11 或 Telegram 会话中直接发送包含 Stage1st 或 NGA 帖子的链接或分享卡片，机器人会自动解析主楼并发送渲染长图。
2. **手动指令**：发送 `/forum_parse <帖子链接>`（或别名 `/解析帖子 <帖子链接>`）可无视冷却时间强制解析指定链接，并在出错时直接返回具体原因。

## 开发与测试

```bash
# 运行单元测试
pytest -v

# 运行 Ruff 代码检查与格式化检查
ruff check .
ruff format --check .
```

## 参考与致谢

- [AstrBot 插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html) & [AstrBot T2I Service](https://github.com/AstrBotDevs/astrbot-t2i-service)
- Stage1st 不可见内容解锁机制参考自 Kangaroo0425 的油猴脚本 [查看S1不可见内容(审核中/禁言)自动版 (GreasyFork #419494)](https://greasyfork.org/zh-CN/scripts/419494-%E6%9F%A5%E7%9C%8Bs1%E4%B8%8D%E5%8F%AF%E8%A7%81%E5%86%85%E5%AE%B9-%E5%AE%A1%E6%A0%B8%E4%B8%AD-%E7%A6%81%E8%A8%80-%E8%87%AA%E5%8A%A8%E7%89%88)
