# astrbot_plugin_chat_report

_✨ 聊天消息举报插件 ✨_

基于 AstrBot（aiocqhttp / NapCatQQ）的群聊举报插件：先由 AI 自动研判举报内容，
能处理就自动撤回、警告、禁言；拿不准时才私聊管理员与本群指定用户，由人工回复处理。

## 💡 功能特性

### 举报触发规则（严格匹配）

必须 **同时** 满足两个条件才会触发，避免日常聊天误触：

1. **必须艾特机器人本人**：只 @ 群友不触发。
2. **整条消息只有「@ + 举报」**：把消息里的 @ 去掉、去掉空白后，文字必须**正好**是「举报」。
   多一个字、一张图、一个表情都不触发（例如「举报我」「举报一下」「@群友 来吧 举报我」都不会触发）。

两种举报方式：

| 方式 | 消息内容 | 被举报人 |
|:---|:---|:---|
| 引用举报 | 引用一条消息 + `@机器人 举报` | 被引用消息的作者 |
| 无引用举报 | `@机器人 @某人 举报` | 被 @ 的那个人 |

- 同时引用 + @ 别人时，**以引用的作者为准**（引用优先），@ 的顺序不影响判定。
- 只 `@机器人 举报` 而没指明对象时，机器人会回一条使用提示，不会静默无响应。

### AI 自动研判

AI 输出 JSON：`decision` / `confidence` / `reason` / `target_message_id`，据此分流：

| AI 结论 | 处理 |
|:---|:---|
| `violate` 且置信度 ≥ 阈值，且能定位到消息 ID | **自动**：撤回 + 群内艾特警告 + 禁言 + 警告次数 +1，**不私聊管理员** |
| `violate` 但无法定位消息 ID | 转人工（通知里说明原因） |
| `safe` | 视开关决定：是否在群内发安全提示 / 是否私聊通知管理员 |
| `uncertain` 或置信度低于阈值 | 转人工 |
| 被举报消息是非文本（纯图片 / 语音 / 文件） | 直接转人工，不自动分析 |

- 消息内容渲染为可读文本：图片 → `[图片]`、表情 → `[表情]`、语音 → `[语音]`、视频 → `[视频]`、文件 → `[文件]` 等，不会出现 `ComponentType.Image` 这类内部名。
- 无引用举报时，会把被举报人最近 N 条缓存消息（带消息 ID）交给 AI，供其挑选违规消息以便撤回。

### 转人工通知

- **通知对象**：AstrBot 全局管理员 + 本群群主/管理员（开关）+ 指定 QQ 列表。
- **发送方式**（按顺序尝试，任一成功即止）：
  1. 带来源群的**群临时会话** —— 管理员未加机器人好友也能收到；
  2. 普通私聊；
  3. 若全部失败，改为**在群内艾特该管理员**并附上通知内容，保证通知不丢。
- 管理员或指定用户**私聊回复「1」**（可配置）视为处理完成，并向其他已通知对象发送「已处理」通知。
- 收件人昵称优先取群名片/昵称缓存，其次查真实 QQ 昵称，避免出现「临时会话(12345678)」。

### 防封号

无引用举报转人工时，通知内容默认**不含**具体消息内容，只给出最近消息的发送时间。

### WebUI 配置

- **面板插件配置**：`_conf_schema.json`，4 组（AI 研判 / 默认群配置 / 行为开关 / 通知模板）。
- **独立配置页面**：`pages/config`，分 5 个区（AI 研判 / 默认群配置 / 行为开关 / 通知模板 / 分群覆盖），
  支持卡片式编辑、模板变量一键插入、模板恢复默认、**可视化分群覆盖**（勾选要覆盖的项即可，无需手写 JSON）。

## 📦 安装

1. 将本目录放入 AstrBot 插件目录，目录名必须为 `astrbot_plugin_chat_report`：

```bash
cp -r astrbot_plugin_chat_report /AstrBot/data/plugins/
```

2. 重启 AstrBot（`main.py` 改动必须完整重启，热重载无效）。
3. 在面板 → 插件管理 → `聊天消息举报` 中配置，或打开独立配置页面。

> ⚠️ 机器人**必须是群管理员**：撤回他人消息、禁言、主动发起群临时会话都依赖管理员权限。

## ⌨️ 使用说明

1. 群成员引用违规消息并 `@机器人 举报`；或发送 `@机器人 @某人 举报`。
2. AI 自动研判：
   - 违规 → 自动撤回、群内 `@对方` 警告、禁言；
   - 安全 → 视开关决定是否在群里提示 / 私聊通知；
   - 拿不准 → 私聊管理员和指定用户，附举报 ID。
3. 管理员 / 指定用户私聊回复 `1` → 处理完成，通知其他已通知对象。

## ⚙️ 配置项

### AI 研判

| 配置项 | 默认 | 说明 |
|:---|:---:|:---|
| `ai_provider_id` | 空 | 用于研判的 AI 提供商，留空用当前默认；可指定 DeepSeek 等 |
| `ai_confidence_threshold` | 0.7 | 判定违规的置信度阈值，达到才自动撤回禁言 |
| `ai_timeout` | 30 | 单次调用超时（秒），超时转人工 |
| `ai_retry_times` | 1 | 调用失败重试次数 |
| `ai_system_prompt` | 内置 | 研判系统提示词，务必保留输出 JSON 的要求 |

### 默认群配置（可被分群覆盖）

| 配置项 | 默认 | 说明 |
|:---|:---:|:---|
| `default_group_rules` | 内置 | 默认群规，AI 研判依据 |
| `default_receiver_ids` | `[]` | 默认指定用户 QQ 列表 |
| `auto_include_group_admins` | `false` | 自动把本群群主/管理员纳入通知对象 |
| `default_mute_duration` | 600 | 默认禁言时长（秒） |
| `recent_msg_count` | 10 | 无引用举报读取的最近消息条数 |
| `report_cooldown` | 60 | 同一人同群举报冷却（秒），0 不限 |
| `daily_report_limit` | 20 | 单人每日举报上限，0 不限 |
| `group_overrides` | `{}` | 分群覆盖（JSON），页面上可视化编辑 |

### 行为开关

| 配置项 | 默认 | 说明 |
|:---|:---:|:---|
| `notify_admin_when_safe` | `false` | AI 判断安全时私聊通知管理员与指定用户 |
| `announce_safe_in_group` | `false` | **AI 判断安全时在群内发一条安全提示** |
| `notify_on_resolved` | `true` | 处理完成后通知其他已通知对象 |
| `broadcast_result_in_group` | `false` | 在群内播报处理结果 |
| `enable_target_report_count` | `true` | 统计被举报人累计被举报次数 |
| `allow_everyone_report` | `true` | 允许所有人举报；关闭后仅白名单可举报 |
| `reporters_whitelist` | `[]` | 举报人白名单 |
| `receiver_can_handle` | `true` | 指定用户与管理同等处理权限 |
| `reply_content` | `1` | 管理员处理完成的回复内容 |
| `report_expire_minutes` | 60 | 超时未处理时长（分钟），超时标记 `expired` |

### 模板

| 配置项 | 用途 |
|:---|:---|
| `warn_template` | 群内警告（会先自动艾特被举报人） |
| `safe_group_template` | **群内安全提示**（`announce_safe_in_group` 开启时） |
| `safe_notify_template` | 安全通知（私聊） |
| `manual_notify_template` | 转人工通知（引用举报，含消息内容） |
| `manual_notify_template_nocontent` | 转人工通知（无引用，不含内容，防封号） |
| `resolved_notify_template` | 处理完成通知 |
| `group_broadcast_template` | 群内处理结果播报 |

## 🧩 模板变量

| 变量 | 说明 |
|:---:|:---|
| `{group_name}` / `{group_id}` | 群名称 / 群号 |
| `{sender}` | 被举报消息发送者昵称 |
| `{target}` | 被举报人（昵称(QQ)） |
| `{reporter}` | 举报人（昵称(QQ)） |
| `{message}` | 被举报消息内容（无引用举报时为空/占位） |
| `{message_id}` | 被举报消息 ID |
| `{target_report_count}` | 被举报人累计被举报次数 |
| `{warn_count}` | 被举报人累计警告次数 |
| `{ai_judgement}` | AI 初步判断：安全 / 拿不准 / 违规但无法处理 |
| `{ai_confidence}` | 置信度（百分比） |
| `{report_id}` | 本次举报 ID |
| `{recent_times}` / `{recent_count}` | 最近消息的发送时间 / 条数 |
| `{time}` | 当前时间 |
| `{handler}` | 处理人（处理完成通知 / 播报时有效） |
| `{mute_duration}` | 禁言时长（已格式化，如 `10分钟`） |

变量缺失时会原样保留 `{变量名}`，不会报错。

## 🔀 分群覆盖

默认群配置可针对单个群覆盖，key 为群号。可覆盖字段：

`group_rules`、`receiver_ids`、`mute_duration`、`recent_msg_count`、`report_cooldown`、
`daily_limit`、`notify_admin_when_safe`、`announce_safe_in_group`、`auto_include_group_admins`、
`reply_content`、`warn_template`、`safe_group_template`

JSON 示例：

```json
{
  "123456789": {
    "group_rules": "本群禁止发广告、禁止刷屏。",
    "receiver_ids": ["10001", "10002"],
    "mute_duration": 3600,
    "recent_msg_count": 15,
    "notify_admin_when_safe": true,
    "announce_safe_in_group": true
  }
}
```

> 推荐在独立配置页面的「分群覆盖」区可视化编辑：新增群 → 填群号 → 勾选要覆盖的项，不勾的走默认值。

## 📌 数据

运行数据保存在 AstrBot 插件数据目录：`data/plugin_data/astrbot_plugin_chat_report/`

- `counters.json`：被举报次数、警告次数统计
- `reports.json`：待处理举报
- `audit.jsonl`：审计日志（谁举报、谁被举报、AI 判断、是否处理、处理人）

## 📄 License

MIT
