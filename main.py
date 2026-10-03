"""AstrBot 举报插件（AI 自动研判 + 转人工处理）

核心流程：
1. 用户举报（引用举报 / 无引用 @被举报人 举报），先由 AI 自动研判。
2. AI 判断「违规且可处理」→ 自动撤回 + 群内艾特警告 + 禁言 + 记录警告次数，不私聊管理员。
3. AI 判断「安全」→ 由 notify_admin_when_safe 开关决定是否私聊通知管理员与指定用户。
4. AI「拿不准 / 置信度过低 / 消息无法获取 / 机器人无权限」→ 私聊通知本群管理员与指定用户，
   回复 1 视为处理完成，并通知其他已通知对象。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import At, Plain, Reply
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

try:  # 兼容不同版本的 AstrBot
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
except ImportError:  # pragma: no cover
    get_astrbot_plugin_data_path = None

try:  # 独立配置页面（pages/config）读写配置用的插件 Web API
    from astrbot.api.web import error_response, json_response, request
except ImportError:  # pragma: no cover
    error_response = json_response = None
    request = None

PLUGIN_NAME = "astrbot_plugin_chat_report"

# 每个用户在每个群最多缓存多少条消息（用于无引用举报回溯）
MAX_CACHE_PER_USER = 30

DEFAULT_WARN_TEMPLATE = (
    "你发送的消息不合群规，警告第 {warn_count} 次。已禁言 {mute_duration}。"
)

DEFAULT_SAFE_TEMPLATE = (
    "【举报通知-AI判断安全】\n"
    "群：{group_name}({group_id})\n"
    "发送者/被举报人：{target}\n"
    "消息内容：{message}\n"
    "举报人：{reporter}\n"
    "被举报人累计被举报次数：第 {target_report_count} 次\n"
    "AI初步判断：{ai_judgement}（置信度：{ai_confidence}）\n"
    "说明：AI判断该消息未发现违规，暂不处理。"
)

DEFAULT_MANUAL_TEMPLATE = (
    "【举报待处理-AI拿不准】\n"
    "群：{group_name}({group_id})\n"
    "发送者/被举报人：{target}\n"
    "消息内容：{message}\n"
    "举报人：{reporter}\n"
    "被举报人累计被举报次数：第 {target_report_count} 次\n"
    "AI初步判断：{ai_judgement}（置信度：{ai_confidence}）\n"
    "请管理员分析并处理。处理完请回复 1。\n"
    "举报ID：{report_id}"
)

DEFAULT_MANUAL_TEMPLATE_NOCONTENT = (
    "【举报待处理-AI拿不准】\n"
    "群：{group_name}({group_id})\n"
    "发送者/被举报人：{target}\n"
    "消息内容：（无引用举报，为防止封号不展示具体消息内容）\n"
    "该用户最近 {recent_count} 条消息发送时间：{recent_times}\n"
    "举报人：{reporter}\n"
    "被举报人累计被举报次数：第 {target_report_count} 次\n"
    "AI初步判断：{ai_judgement}（置信度：{ai_confidence}）\n"
    "请管理员分析并处理。处理完请回复 1。\n"
    "举报ID：{report_id}"
)

DEFAULT_RESOLVED_TEMPLATE = (
    "【举报已处理】\n"
    "举报ID：{report_id}\n"
    "群：{group_name}({group_id})\n"
    "已经由管理员：{handler} 处理。"
)

DEFAULT_BROADCAST_TEMPLATE = "举报（{report_id}）已由 {handler} 处理完成。"

DEFAULT_SAFE_GROUP_TEMPLATE = (
    "【举报结果】经 AI 初步判断，{target} 被举报的消息未发现违规，本次不作处理。"
)

DEFAULT_SYSTEM_PROMPT = (
    "你是一个严格的群聊内容审核助手。请根据群规判断「被举报的消息或用户最近发言」是否违规。\n"
    "违规类型包括：涉政、广告、刷屏、辱骂、骚扰、色情、诈骗、人身攻击、违反群规等。\n"
    "你必须只输出一个 JSON 对象，不要输出任何多余文字，格式如下：\n"
    '{{"decision": "violate|safe|uncertain", "confidence": 0.0, '
    '"reason": "简短原因", "target_message_id": ""}}\n'
    "decision 说明：violate=确认违规；safe=确认安全；uncertain=无法判断或证据不足。\n"
    "confidence 为 0 到 1 之间的小数。\n"
    "若你能确定某条消息违规，请把该消息ID填入 target_message_id，否则留空。\n"
    "【本群群规】\n{group_rules}"
)

DEFAULT_GROUP_RULES = (
    "1. 禁止发布涉政、色情、暴力、赌博、诈骗等违法内容；\n"
    "2. 禁止刷屏、广告、外链推广；\n"
    "3. 禁止辱骂、人身攻击、骚扰他人；\n"
    "4. 其他影响群内秩序的行为。"
)


class _SafeDict(dict):
    """模板渲染时，缺失的变量原样保留，避免抛异常。"""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _render(template: str, ctx: dict[str, Any]) -> str:
    try:
        return str(template).format_map(_SafeDict(ctx))
    except Exception:
        return str(template)


def _fmt_duration(seconds: int) -> str:
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "0分钟"
    if seconds % 60 == 0:
        return f"{seconds // 60}分钟"
    return f"{seconds}秒"


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# 消息段类型 → 中文展示文案（用于把被举报的消息转成可读文本，避免出现 ComponentType.X）
_SEG_LABELS = {
    "Image": "[图片]",
    "Face": "[表情]",
    "MFace": "[表情]",
    "WeChatEmoji": "[表情]",
    "Record": "[语音]",
    "TTS": "[语音]",
    "Video": "[视频]",
    "File": "[文件]",
    "OnlineFile": "[文件]",
    "Json": "[卡片消息]",
    "Xml": "[卡片消息]",
    "MiniApp": "[小程序]",
    "Music": "[音乐]",
    "Poke": "[戳一戳]",
    "Dice": "[骰子]",
    "Rps": "[猜拳]",
    "Shake": "[窗口抖动]",
    "Location": "[位置]",
    "Contact": "[名片]",
    "Forward": "[合并转发]",
    "Node": "[合并转发]",
    "Nodes": "[合并转发]",
    "Markdown": "[Markdown 消息]",
    "Keyboard": "[按钮消息]",
    "Reply": "[引用]",
}


@register(
    "astrbot_plugin_chat_report",
    "yosers",
    "聊天消息举报：AI自动研判，违规自动撤回禁言警告，拿不准转人工私聊管理员处理",
    "1.0.0",
)
class ChatReportPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config

        self.data_dir = self._resolve_data_dir()
        self.reports_file = self.data_dir / "reports.json"
        self.counters_file = self.data_dir / "counters.json"
        self.audit_file = self.data_dir / "audit.jsonl"

        # group:user -> deque[{text, message_id, time}]
        self._msg_cache: dict[str, deque] = {}
        # (group:user) -> 被举报次数
        self._report_counts: dict[str, int] = {}
        # (group:user) -> 警告次数
        self._warn_counts: dict[str, int] = {}
        # report_id -> pending report
        self._pending: dict[str, dict[str, Any]] = {}
        # 举报冷却/频率: group:reporter -> {"last": ts, "day": "2026-10-03", "count": n}
        self._rate: dict[str, dict[str, Any]] = {}
        # group_id -> group_name
        self._group_names: dict[str, str] = {}
        # user_id -> 展示名（群里解析到的名片/昵称），私聊通知时用来避免显示成“临时会话(xxx)”
        self._user_names: dict[str, str] = {}
        self._expire_task: asyncio.Task | None = None

        self._load_state()
        self._register_web_apis()

    # ------------------------------------------------------------------ #
    # 基础工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _resolve_data_dir() -> Path:
        try:
            if get_astrbot_plugin_data_path:
                base = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
            else:
                base = Path(os.path.abspath(__file__)).parents[2] / PLUGIN_NAME
        except Exception:
            base = Path(os.path.abspath(__file__)).parent / "data"
        base.mkdir(parents=True, exist_ok=True)
        return base

    # _conf_schema.json 采用「分组对象」结构，落盘配置形如 {"ai": {...}, ...}
    CONFIG_GROUP_KEYS = ("ai", "groups", "behavior", "templates")

    def _c(self, key: str, default: Any) -> Any:
        """读取配置项：兼容扁平结构与分组（嵌套）结构。"""
        value: Any = None
        try:
            value = self.config.get(key)
            if value is None:
                for group in self.CONFIG_GROUP_KEYS:
                    sub = self.config.get(group)
                    if isinstance(sub, dict) and sub.get(key) is not None:
                        value = sub.get(key)
                        break
        except Exception:
            value = None
        return default if value is None else value

    def _parse_overrides(self) -> dict[str, dict]:
        raw = self._c("group_overrides", "")
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                data = json.loads(raw)
                return data if isinstance(data, dict) else {}
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 分群配置 JSON 解析失败：{e}")
        return {}

    def _group_conf(self, group_id: str) -> dict[str, Any]:
        conf: dict[str, Any] = {
            "group_rules": self._c("default_group_rules", DEFAULT_GROUP_RULES),
            "receiver_ids": list(self._c("default_receiver_ids", []) or []),
            "mute_duration": int(self._c("default_mute_duration", 600)),
            "recent_msg_count": int(self._c("recent_msg_count", 10)),
            "report_cooldown": int(self._c("report_cooldown", 60)),
            "daily_limit": int(self._c("daily_report_limit", 20)),
            "notify_admin_when_safe": bool(self._c("notify_admin_when_safe", False)),
            "announce_safe_in_group": bool(
                self._c("announce_safe_in_group", False)
            ),
            "auto_include_group_admins": bool(
                self._c("auto_include_group_admins", False)
            ),
            "reply_content": str(self._c("reply_content", "1") or "1"),
            "warn_template": self._c("warn_template", DEFAULT_WARN_TEMPLATE),
            "safe_group_template": self._c(
                "safe_group_template", DEFAULT_SAFE_GROUP_TEMPLATE
            ),
        }
        override = self._parse_overrides().get(str(group_id))
        if isinstance(override, dict):
            conf.update(override)
        return conf

    # ------------------------------------------------------------------ #
    # WebUI 独立配置页面（pages/config）后端接口
    # ------------------------------------------------------------------ #
    @staticmethod
    def _schema_file() -> Path:
        return Path(os.path.abspath(__file__)).with_name("_conf_schema.json")

    def _schema_defaults(self) -> dict[str, Any]:
        """把 _conf_schema.json 解析为 {分组: {键: 默认值}}。"""
        try:
            schema = json.loads(self._schema_file().read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 读取 _conf_schema.json 失败：{e}")
            return {}

        def walk(items: Any) -> dict[str, Any]:
            out: dict[str, Any] = {}
            if not isinstance(items, dict):
                return out
            for key, item in items.items():
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "object":
                    out[key] = walk(item.get("items", {}))
                else:
                    out[key] = item.get("default")
            return out

        return walk(schema)

    def _current_group_conf(self, group: str, defaults: dict[str, Any]) -> dict[str, Any]:
        raw = self.config.get(group)
        raw = raw if isinstance(raw, dict) else {}
        conf: dict[str, Any] = {}
        for key, default in defaults.items():
            if key in raw and raw[key] is not None:
                conf[key] = raw[key]
            else:
                fallback = self.config.get(key)
                conf[key] = fallback if fallback is not None else default
        return conf

    def _collect_config(self) -> dict[str, Any]:
        defaults = self._schema_defaults()
        return {
            group: self._current_group_conf(group, items)
            for group, items in defaults.items()
            if isinstance(items, dict)
        }

    @staticmethod
    def _coerce_value(value: Any, ref: Any) -> Any:
        """按 schema 默认值的类型把前端提交的值转成正确类型。"""
        if isinstance(ref, bool):
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on", "是")
            return bool(value)
        if isinstance(ref, int):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return ref
        if isinstance(ref, float):
            try:
                return float(value)
            except (TypeError, ValueError):
                return ref
        if isinstance(ref, list):
            if isinstance(value, list):
                return [str(v).strip() for v in value if str(v).strip()]
            if isinstance(value, str):
                parts = re.split(r"[\n,，;；\s]+", value)
                return [p for p in (x.strip() for x in parts) if p]
            return ref
        if value is None:
            return ref if ref is not None else ""
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    def _provider_options(self) -> list[dict[str, str]]:
        options: list[dict[str, str]] = []
        try:
            providers = self.context.get_all_providers()
        except Exception:
            providers = []
        for provider in providers or []:
            try:
                meta = provider.meta()
            except Exception:
                meta = None
            pid = str(getattr(meta, "id", "") or "").strip()
            model = str(getattr(meta, "model", "") or "").strip()
            if not pid:
                continue
            options.append({"id": pid, "label": f"{pid}（{model}）" if model else pid})
        return options

    def _known_groups(self) -> list[dict[str, str]]:
        """运行时见过的群（由群消息事件累积），仅用于页面里的群号联想。"""
        out: list[dict[str, str]] = []
        try:
            for gid, gname in self._group_names.items():
                out.append({"id": str(gid), "name": str(gname or "")})
        except Exception:
            pass
        return out

    def _register_web_apis(self) -> None:
        if json_response is None or request is None:
            logger.warning(f"[{PLUGIN_NAME}] 当前 AstrBot 无 astrbot.api.web，跳过配置页接口注册")
            return
        p = PLUGIN_NAME
        self.context.register_web_api(f"/{p}/config", self.api_get_config, ["GET"], "读取插件配置")
        self.context.register_web_api(f"/{p}/config", self.api_save_config, ["POST"], "保存插件配置")
        logger.info(f"[{PLUGIN_NAME}] 配置页接口已注册：/{p}/config (GET/POST)")

    async def api_get_config(self):
        return json_response(
            {
                "plugin": PLUGIN_NAME,
                "config": self._collect_config(),
                "defaults": self._schema_defaults(),
                "providers": self._provider_options(),
                "known_groups": self._known_groups(),
            }
        )

    async def api_save_config(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体需为 JSON 对象")
        incoming = payload.get("config", payload)
        if not isinstance(incoming, dict):
            return error_response("config 字段需为对象")

        defaults = self._schema_defaults()
        new_conf: dict[str, Any] = {}
        for group, items in defaults.items():
            if not isinstance(items, dict):
                continue
            src = incoming.get(group)
            src = src if isinstance(src, dict) else {}
            stored = self.config.get(group)
            stored = stored if isinstance(stored, dict) else {}
            clean: dict[str, Any] = {}
            for key, default in items.items():
                base = stored.get(key)
                if base is None:
                    base = self._c(key, default)
                ref = default if default is not None else base
                clean[key] = self._coerce_value(src[key], ref) if key in src else base
            new_conf[group] = clean

        try:
            self.config.update(new_conf)
            save_async = getattr(self.config, "save_config_async", None)
            if save_async is not None:
                await save_async(new_conf)
            else:
                self.config.save_config(new_conf)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 保存配置失败：{e}", exc_info=True)
            return error_response(f"保存失败：{e}", status_code=500)

        logger.info(f"[{PLUGIN_NAME}] 通过独立配置页保存了插件配置")
        return json_response({"ok": True, "config": self._collect_config()})

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def initialize(self):
        self._expire_task = asyncio.create_task(self._expire_loop())
        logger.info(f"[{PLUGIN_NAME}] 举报插件已加载，数据目录：{self.data_dir}")

    async def terminate(self):
        if self._expire_task:
            self._expire_task.cancel()
        self._save_state()

    # ------------------------------------------------------------------ #
    # 事件：群消息（缓存 + 举报检测）
    # ------------------------------------------------------------------ #
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_group_message(self, event: AiocqhttpMessageEvent):
        try:
            group_id = str(event.get_group_id())
            sender_id = str(event.get_sender_id())
            self_id = str(event.get_self_id())
        except Exception:
            return

        self._cache_message(event, group_id, sender_id, self_id)

        if sender_id == self_id:
            return

        parsed = self._parse_report(event, self_id)
        if not parsed:
            return

        # @ 了机器人发「举报」但没说举报谁 → 回一条使用提示
        if parsed.get("no_target"):
            event.stop_event()
            try:
                await event.send(
                    event.plain_result(
                        "请艾特被举报人，或引用他发送的消息，再艾特我发送「举报」。"
                    )
                )
            except Exception as e:
                logger.error(f"[{PLUGIN_NAME}] 举报提示发送失败：{e}")
            return

        # 命中举报：阻止后续 LLM 等处理器
        event.stop_event()
        try:
            await self._handle_report(event, group_id, sender_id, self_id, parsed)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 处理举报异常：{e}", exc_info=True)

    # ------------------------------------------------------------------ #
    # 事件：私聊回复（处理确认）
    # ------------------------------------------------------------------ #
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_private_message(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        if not text:
            return
        sender_id = str(event.get_sender_id())

        # 找到该用户可处理、且尚未处理的举报
        report = None
        for item in self._pending.values():
            if item.get("handled") or item.get("expired"):
                continue
            if sender_id not in item.get("notified", []):
                continue
            allowed = item.get("allowed_reply_content", ["1"])
            if text not in allowed:
                continue
            # 指定用户是否和管理员同等处理
            if not item.get("receiver_can_handle", True) and sender_id not in item.get(
                "admin_ids", []
            ):
                continue
            report = item
            break

        if not report:
            return

        # 阻止这条「1」继续走 AstrBot 的 LLM 流程（否则会同时触发一次主动回复）
        event.stop_event()

        report["handled"] = True
        report["handler"] = sender_id
        handler_name = await self._resolve_private_name(event, sender_id)
        handler_display = f"{handler_name}({sender_id})"
        report["handler_display"] = handler_display

        group_id = str(report.get("group_id") or "")
        ctx = self._build_ctx(report, handler=handler_display)
        resolved_tpl = self._c("resolved_notify_template", DEFAULT_RESOLVED_TEMPLATE)
        resolved_text = _render(resolved_tpl, ctx)

        # 通知其他已通知对象（不含处理人）
        failed: list[str] = []
        if bool(self._c("notify_on_resolved", True)):
            for uid in report.get("notified", []):
                if uid == sender_id:
                    continue
                if not await self._send_private(event, uid, resolved_text, group_id):
                    failed.append(uid)
        if failed:
            await self._notify_in_group(event, group_id, failed, resolved_text)

        # 回复处理人，确认已记录
        await self._send_private(
            event, sender_id, f"已记录，本次举报（{report['report_id']}）由你处理完成。"
        )

        # 可选群内播报
        if bool(self._c("broadcast_result_in_group", False)):
            broadcast_tpl = self._c(
                "group_broadcast_template", DEFAULT_BROADCAST_TEMPLATE
            )
            await self._send_group(
                event, group_id, _render(broadcast_tpl, ctx)
            )

        self._audit("resolved", report=report, extra={"handler": sender_id})
        self._save_state()

    # ------------------------------------------------------------------ #
    # 举报解析
    # ------------------------------------------------------------------ #
    def _cache_message(
        self, event: AstrMessageEvent, group_id: str, sender_id: str, self_id: str
    ):
        if sender_id == self_id:
            return
        text = (event.message_str or "").strip()
        if not text:
            # 纯图片/语音等消息没有文本，转成 [图片] 这类占位，避免被整条丢弃
            try:
                text = self._chain_to_text(event.get_messages())
            except Exception:
                text = ""
        if not text:
            return
        try:
            message_id = str(event.message_obj.message_id)
        except Exception:
            message_id = ""
        key = f"{group_id}:{sender_id}"
        queue = self._msg_cache.setdefault(key, deque(maxlen=MAX_CACHE_PER_USER))
        queue.append({"text": text, "message_id": message_id, "time": int(time.time())})

    def _parse_report(
        self, event: AstrMessageEvent, self_id: str
    ) -> dict[str, Any] | None:
        """识别举报消息，返回 {is_reply, target_id, message_id, content}。"""
        try:
            chain = list(event.get_messages())
        except Exception:
            chain = []

        # 触发条件一：整条消息只能是「@ + 举报」，不允许夹带任何其它内容
        # （多一个字、一张图、一个表情都不算，避免日常聊天误触发）
        if any(not isinstance(seg, (At, Plain, Reply)) for seg in chain):
            return None
        if self._chain_text_only(chain).strip().lower() not in ("举报", "report"):
            return None

        # 触发条件二：必须 @ 了机器人本人。只 @ 别人 +「举报」不触发
        bot_id = str(self_id or "").strip()
        self_mentioned = bool(bot_id) and any(
            isinstance(seg, At) and str(seg.qq).strip() == bot_id for seg in chain
        )
        if not self_mentioned:
            return None

        reply_seg = next((seg for seg in chain if isinstance(seg, Reply)), None)
        # 被举报人候选：排除机器人自己与 @全体成员
        at_ids = [
            str(seg.qq).strip()
            for seg in chain
            if isinstance(seg, At) and str(seg.qq).strip() not in (bot_id, "all")
        ]

        if reply_seg is not None:
            target_id = str(reply_seg.sender_id or "")
            message_id = str(reply_seg.id or "")
            rchain = list(reply_seg.chain or [])
            # 纯文本部分（用于判断能否交给 AI 分析）
            plain_text = self._chain_text_only(rchain)
            if not plain_text:
                plain_text = (reply_seg.message_str or "").strip()
            # 展示用内容：优先按消息段渲染成中文（图片→[图片]），避免出现 ComponentType.Image
            content = self._chain_to_text(rchain) if rchain else plain_text
            return {
                "is_reply": True,
                "target_id": target_id or (at_ids[0] if at_ids else ""),
                "message_id": message_id,
                "content": content,
                "has_text": bool(plain_text),
            }

        if at_ids:
            return {
                "is_reply": False,
                "target_id": at_ids[0],
                "message_id": "",
                "content": "",
            }

        # @ 了机器人 + 「举报」，但没指明举报谁 → 交由上层回一条使用提示
        return {"no_target": True}

    @staticmethod
    def _seg_key(seg: Any) -> str:
        """取消息段的类型名，如 Image / Face（兼容枚举与类名两种情况）。"""
        seg_type = getattr(seg, "type", None)
        return str(getattr(seg_type, "name", "") or "") or type(seg).__name__

    @classmethod
    def _chain_to_text(cls, chain: list) -> str:
        """把消息段链转成可读文本：图片→[图片]、表情→[表情] 等。"""
        parts = []
        for seg in chain or []:
            if isinstance(seg, Plain):
                parts.append(str(getattr(seg, "text", "")))
            elif isinstance(seg, At):
                qq = str(getattr(seg, "qq", ""))
                if qq == "all":
                    parts.append("@全体成员")
                else:
                    parts.append(f"@{getattr(seg, 'name', '') or qq}")
            elif isinstance(seg, Reply):
                continue
            else:
                parts.append(_SEG_LABELS.get(cls._seg_key(seg), "[消息]"))
        return "".join(parts).strip()

    @staticmethod
    def _chain_text_only(chain: list) -> str:
        """只取纯文本部分，用于判断被举报的消息是否有可分析的文字。"""
        return "".join(
            str(getattr(seg, "text", ""))
            for seg in chain or []
            if isinstance(seg, Plain)
        ).strip()

    # ------------------------------------------------------------------ #
    # 主处理流程
    # ------------------------------------------------------------------ #
    async def _handle_report(
        self,
        event: AiocqhttpMessageEvent,
        group_id: str,
        sender_id: str,
        self_id: str,
        parsed: dict[str, Any],
    ):
        conf = self._group_conf(group_id)

        # 1. 举报权限
        if not self._can_report(sender_id, conf):
            logger.debug(f"[{PLUGIN_NAME}] 用户 {sender_id} 无举报权限，忽略")
            return

        # 2. 冷却与每日上限
        ok, reason = self._check_rate(group_id, sender_id, conf)
        if not ok:
            logger.debug(f"[{PLUGIN_NAME}] 举报被限流：{reason}")
            return

        # 3. 目标
        target_id = str(parsed.get("target_id") or "").strip()
        if not target_id or target_id == self_id:
            return
        is_reply = bool(parsed.get("is_reply"))
        message_id = str(parsed.get("message_id") or "")
        content = str(parsed.get("content") or "")
        has_text = bool(parsed.get("has_text", bool(content)))

        # 4. 计数
        enable_count = bool(self._c("enable_target_report_count", True))
        target_report_count = 0
        if enable_count:
            target_report_count = self._incr_report_count(group_id, target_id)
        warn_count = self._warn_counts.get(f"{group_id}:{target_id}", 0)

        # 5. 最近消息
        recent = self._recent_messages(group_id, target_id, conf["recent_msg_count"])

        # 6. 展示用信息
        group_name = await self._get_group_name(event, group_id)
        target_name = await self._resolve_name(event, group_id, target_id)
        reporter_name = await self._resolve_name(event, group_id, sender_id)
        target_display = f"{target_name}({target_id})"
        reporter_display = f"{reporter_name}({sender_id})"

        report_id = f"R{int(time.time())}{uuid.uuid4().hex[:4]}"
        base_ctx = {
            "group_name": group_name,
            "group_id": group_id,
            "target_id": target_id,
            "reporter_id": sender_id,
            "sender": target_name,
            "target": target_display,
            "reporter": reporter_display,
            "message": content if is_reply else "",
            "message_id": message_id,
            "target_report_count": target_report_count,
            "warn_count": warn_count,
            "report_id": report_id,
            "recent_count": len(recent),
            "recent_times": self._recent_times(recent),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mute_duration": _fmt_duration(conf["mute_duration"]),
        }

        # 7. AI 研判
        if is_reply and not has_text:
            # 引用非文本消息（图片/语音/文件），交给人工
            judgement = {
                "decision": "uncertain",
                "confidence": 0.0,
                "reason": "被举报消息为非文本内容，无法自动分析",
                "target_message_id": "",
            }
        else:
            judgement = await self._ai_judge(
                event, conf, group_id, target_id, is_reply, content, recent
            )

        decision = judgement.get("decision", "uncertain")
        confidence = float(judgement.get("confidence", 0.0) or 0.0)
        threshold = float(self._c("ai_confidence_threshold", 0.7))
        ai_msg_id = str(judgement.get("target_message_id") or "").strip()

        base_ctx["ai_confidence"] = f"{round(confidence * 100)}%"

        logger.info(
            f"[{PLUGIN_NAME}] 举报 {report_id} 群{group_id} 目标{target_id} "
            f"AI={decision} conf={confidence}"
        )

        # 8. 分支处理
        if decision == "safe":
            base_ctx["ai_judgement"] = "安全"
            if conf.get("announce_safe_in_group"):
                await self._announce_safe(event, conf, base_ctx)
            if conf.get("notify_admin_when_safe"):
                await self._notify_safe(event, conf, base_ctx)
            self._audit(
                "safe",
                report={
                    "report_id": report_id,
                    "group_id": group_id,
                    "target_id": target_id,
                    "reporter_id": sender_id,
                },
                extra={"confidence": confidence, "reason": judgement.get("reason", "")},
            )
            self._save_state()
            return

        if decision == "violate" and confidence >= threshold:
            # 需要能定位到具体消息才好撤回
            punish_msg_id = message_id or ai_msg_id
            if not punish_msg_id:
                base_ctx["ai_judgement"] = "违规但无法处理"
                await self._notify_manual(
                    event,
                    conf,
                    base_ctx,
                    is_reply=is_reply,
                    reason="AI 判定违规，但无法定位具体消息ID，无法自动撤回",
                    admin_ids=await self._collect_admins(event, group_id, conf),
                )
                return
            try:
                new_warn_count = await self._do_auto_punish(
                    event, conf, group_id, target_id, punish_msg_id, target_report_count
                )
                base_ctx["warn_count"] = new_warn_count
                self._audit(
                    "violate_auto",
                    report={
                        "report_id": report_id,
                        "group_id": group_id,
                        "target_id": target_id,
                        "reporter_id": sender_id,
                    },
                    extra={
                        "confidence": confidence,
                        "reason": judgement.get("reason", ""),
                        "message_id": punish_msg_id,
                        "warn_count": new_warn_count,
                        "mute_duration": conf["mute_duration"],
                    },
                )
                if bool(self._c("broadcast_result_in_group", False)):
                    bcast = self._c(
                        "group_broadcast_template", DEFAULT_BROADCAST_TEMPLATE
                    )
                    ctx = dict(base_ctx)
                    ctx["handler"] = "AI自动处理"
                    await self._send_group(event, group_id, _render(bcast, ctx))
                self._save_state()
                return
            except Exception as e:
                logger.error(f"[{PLUGIN_NAME}] 自动处理失败：{e}", exc_info=True)
                base_ctx["ai_judgement"] = "违规但无法处理"
                await self._notify_manual(
                    event,
                    conf,
                    base_ctx,
                    is_reply=is_reply,
                    reason=f"自动撤回/禁言失败：{e}",
                    admin_ids=await self._collect_admins(event, group_id, conf),
                )
                return

        # uncertain 或 置信度不足
        base_ctx["ai_judgement"] = "拿不准"
        await self._notify_manual(
            event,
            conf,
            base_ctx,
            is_reply=is_reply,
            reason=judgement.get("reason", ""),
            admin_ids=await self._collect_admins(event, group_id, conf),
        )

    # ------------------------------------------------------------------ #
    # AI 研判
    # ------------------------------------------------------------------ #
    async def _ai_judge(
        self,
        event: AstrMessageEvent,
        conf: dict[str, Any],
        group_id: str,
        target_id: str,
        is_reply: bool,
        content: str,
        recent: list[dict[str, Any]],
    ) -> dict[str, Any]:
        fallback = {
            "decision": "uncertain",
            "confidence": 0.0,
            "reason": "",
            "target_message_id": "",
        }
        try:
            provider = self._get_provider(event)
            if not provider:
                fallback["reason"] = "未配置可用的 AI 提供商"
                return fallback

            system_tpl = self._c("ai_system_prompt", DEFAULT_SYSTEM_PROMPT)
            system_prompt = _render(
                system_tpl, {"group_rules": conf.get("group_rules", "")}
            )

            lines = [
                f"举报方式：{'引用举报' if is_reply else '无引用举报（@被举报人）'}",
                f"被举报人：{target_id}",
            ]
            if is_reply:
                lines.append(f"被举报消息内容：{content or '[非文本消息]'}")
            lines.append(f"该用户最近 {len(recent)} 条消息：")
            if recent:
                for item in recent:
                    lines.append(
                        f"- [{item['time_str']}] (id={item['message_id']}) {item['text']}"
                    )
            else:
                lines.append("- （无缓存记录）")
            lines.append("请严格按照要求输出 JSON 判断结果。")
            prompt = "\n".join(lines)

            timeout = int(self._c("ai_timeout", 30))
            retry = int(self._c("ai_retry_times", 1))
            last_err: Exception | None = None
            for attempt in range(retry + 1):
                try:
                    resp = await asyncio.wait_for(
                        provider.text_chat(
                            system_prompt=system_prompt, prompt=prompt
                        ),
                        timeout=timeout,
                    )
                    raw = getattr(resp, "completion_text", "") or ""
                    data = _extract_json(raw)
                    if not data:
                        last_err = RuntimeError(f"AI 返回无法解析：{raw[:200]}")
                        continue
                    return self._normalize_judgement(data)
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    if attempt < retry:
                        await asyncio.sleep(1)
            logger.warning(f"[{PLUGIN_NAME}] AI 研判失败：{last_err}")
            fallback["reason"] = f"AI 调用/解析失败：{last_err}"
            return fallback
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] AI 研判异常：{e}")
            fallback["reason"] = f"AI 研判异常：{e}"
            return fallback

    @staticmethod
    def _normalize_judgement(data: dict[str, Any]) -> dict[str, Any]:
        raw_decision = str(data.get("decision", "")).strip().lower()
        mapping = {
            "violate": "violate",
            "violation": "violate",
            "违规": "violate",
            "safe": "safe",
            "安全": "safe",
            "uncertain": "uncertain",
            "拿不准": "uncertain",
        }
        decision = mapping.get(raw_decision, "uncertain")
        try:
            confidence = float(data.get("confidence", 0.0) or 0.0)
        except Exception:
            confidence = 0.0
        if confidence > 1:
            confidence = confidence / 100.0
        confidence = max(0.0, min(1.0, confidence))
        return {
            "decision": decision,
            "confidence": confidence,
            "reason": str(data.get("reason", "")),
            "target_message_id": str(data.get("target_message_id", "") or ""),
        }

    def _get_provider(self, event: AstrMessageEvent):
        provider = None
        provider_id = str(self._c("ai_provider_id", "") or "").strip()
        if provider_id:
            try:
                provider = self.context.get_provider_by_id(provider_id)
            except Exception:
                provider = None
        if not provider:
            try:
                provider = self.context.get_using_provider(
                    umo=getattr(event, "unified_msg_origin", None)
                )
            except Exception:
                provider = None
        return provider

    # ------------------------------------------------------------------ #
    # 自动处理
    # ------------------------------------------------------------------ #
    async def _do_auto_punish(
        self,
        event: AiocqhttpMessageEvent,
        conf: dict[str, Any],
        group_id: str,
        target_id: str,
        message_id: str,
        target_report_count: int,
    ) -> int:
        # 1. 撤回
        await event.bot.delete_msg(message_id=int(message_id))

        # 2. 禁言
        mute_duration = int(conf.get("mute_duration", 600))
        await event.bot.set_group_ban(
            group_id=int(group_id), user_id=int(target_id), duration=mute_duration
        )

        # 3. 警告计数 +1
        new_warn_count = self._incr_warn_count(group_id, target_id)

        # 4. 群内艾特警告
        warn_tpl = conf.get("warn_template", DEFAULT_WARN_TEMPLATE)
        warn_ctx = {
            "target": target_id,
            "warn_count": new_warn_count,
            "mute_duration": _fmt_duration(mute_duration),
            "group_id": group_id,
        }
        warn_text = _render(warn_tpl, warn_ctx)
        try:
            await event.send(
                event.chain_result([At(qq=str(target_id)), Plain(" " + warn_text)])
            )
        except Exception:
            # 艾特失败则退化为纯文本
            await self._send_group(event, group_id, warn_text)

        logger.info(
            f"[{PLUGIN_NAME}] 已自动处理：群{group_id} 用户{target_id} "
            f"消息{message_id} 警告{new_warn_count}次 禁言{mute_duration}秒"
        )
        return new_warn_count

    # ------------------------------------------------------------------ #
    # 私聊通知
    # ------------------------------------------------------------------ #
    async def _announce_safe(
        self, event: AstrMessageEvent, conf: dict[str, Any], ctx: dict[str, Any]
    ) -> None:
        """AI 判定安全时，可选地在群内发一条「已核实安全」提示。"""
        tpl = conf.get("safe_group_template") or DEFAULT_SAFE_GROUP_TEMPLATE
        try:
            await event.send(event.plain_result(_render(tpl, ctx)))
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 群内安全提示发送失败：{e}")

    async def _notify_safe(
        self, event: AstrMessageEvent, conf: dict[str, Any], ctx: dict[str, Any]
    ):
        receivers = await self._collect_receivers(event, ctx["group_id"], conf)
        if not receivers:
            return
        tpl = self._c("safe_notify_template", DEFAULT_SAFE_TEMPLATE)
        text = _render(tpl, ctx)
        group_id = str(ctx.get("group_id") or "")
        failed: list[str] = []
        for uid in receivers:
            if not await self._send_private(event, uid, text, group_id):
                failed.append(uid)
        if failed:
            await self._notify_in_group(event, group_id, failed, text)
        self._audit(
            "safe_notified",
            report={
                "report_id": ctx.get("report_id"),
                "group_id": ctx.get("group_id"),
                "target_id": ctx.get("target"),
            },
            extra={"notified": sorted(receivers)},
        )
        self._save_state()

    async def _notify_manual(
        self,
        event: AstrMessageEvent,
        conf: dict[str, Any],
        ctx: dict[str, Any],
        *,
        is_reply: bool,
        reason: str,
        admin_ids: list[str],
    ):
        receivers = await self._collect_receivers(event, ctx["group_id"], conf)
        if not receivers:
            logger.warning(f"[{PLUGIN_NAME}] 举报 {ctx['report_id']} 无人可通知")
            return

        if is_reply:
            tpl = self._c("manual_notify_template", DEFAULT_MANUAL_TEMPLATE)
        else:
            tpl = self._c(
                "manual_notify_template_nocontent", DEFAULT_MANUAL_TEMPLATE_NOCONTENT
            )
        text = _render(tpl, ctx)

        report = {
            "report_id": ctx["report_id"],
            "group_id": ctx["group_id"],
            "group_name": ctx.get("group_name", ""),
            "target_id": ctx.get("target_id", ""),
            "target": ctx.get("target", ""),
            "reporter_id": ctx.get("reporter_id", ""),
            "reporter": ctx.get("reporter", ""),
            "sender": ctx.get("sender", ""),
            "message_id": ctx.get("message_id", ""),
            "is_reply": is_reply,
            "notified": sorted(receivers),
            "admin_ids": admin_ids,
            "receiver_can_handle": bool(self._c("receiver_can_handle", True)),
            "allowed_reply_content": [
                str(self._c("reply_content", "1") or "1"),
                "1",
            ],
            "handled": False,
            "expired": False,
            "created": int(time.time()),
            "expire_minutes": int(self._c("report_expire_minutes", 60)),
            "ai_judgement": ctx.get("ai_judgement", ""),
            "ai_confidence": ctx.get("ai_confidence", ""),
            "target_report_count": ctx.get("target_report_count", 0),
            "warn_count": ctx.get("warn_count", 0),
            "mute_duration": ctx.get("mute_duration", ""),
            "message": ctx.get("message", ""),
            "recent_times": ctx.get("recent_times", ""),
            "recent_count": ctx.get("recent_count", 0),
            "reason": reason,
        }
        self._pending[report["report_id"]] = report
        self._save_state()

        group_id = str(ctx.get("group_id") or "")
        failed: list[str] = []
        for uid in receivers:
            if not await self._send_private(event, uid, text, group_id):
                failed.append(uid)
        if failed:
            await self._notify_in_group(event, group_id, failed, text)

        self._audit("manual_notified", report=report, extra={"reason": reason})

    async def _collect_receivers(
        self, event: AstrMessageEvent, group_id: str, conf: dict[str, Any]
    ) -> set[str]:
        receivers: set[str] = set()
        # 只保留纯数字 QQ 号：AstrBot 全局 admins_id 里可能混有 "astrbot" 这类非 QQ 项
        receivers.update(
            self._qq_list(self._bot_admins(getattr(event, "unified_msg_origin", None)))
        )
        receivers.update(self._qq_list(conf.get("receiver_ids")))
        if conf.get("auto_include_group_admins"):
            receivers.update(self._qq_list(await self._group_admin_ids(event, group_id)))
        receivers.discard(str(event.get_self_id()))
        return receivers

    async def _collect_admins(
        self, event: AstrMessageEvent, group_id: str, conf: dict[str, Any]
    ) -> list[str]:
        admins = set(self._qq_list(self._bot_admins(None)))
        if conf.get("auto_include_group_admins"):
            admins.update(self._qq_list(await self._group_admin_ids(event, group_id)))
        return sorted(admins)

    @staticmethod
    def _qq_list(values: Any) -> list[str]:
        """过滤出合法 QQ 号（纯数字），避免把非 QQ 项当成收件人。"""
        out: list[str] = []
        for value in values or []:
            text = str(value).strip()
            if text.isdigit():
                out.append(text)
        return out

    def _bot_admins(self, umo: str | None = None) -> list[str]:
        for kwargs in ({"umo": umo}, {}):
            try:
                cfg = self.context.get_config(**kwargs)
                admins = cfg.get("admins_id", []) or []
                return [str(x) for x in admins]
            except Exception:
                continue
        return []

    async def _group_admin_ids(
        self, event: AstrMessageEvent, group_id: str
    ) -> set[str]:
        ids: set[str] = set()
        try:
            members = await event.bot.api.call_action(
                "get_group_member_list", group_id=int(group_id)
            )
            for member in members or []:
                uid = str(member.get("user_id") or "")
                name = str(member.get("card") or member.get("nickname") or "").strip()
                if uid and name:
                    self._user_names[uid] = name
                if str(member.get("role")) in ("owner", "admin"):
                    ids.add(uid)
        except Exception as e:
            logger.debug(f"[{PLUGIN_NAME}] 获取群管理员失败：{e}")
        return ids

    # ------------------------------------------------------------------ #
    # 发送封装
    # ------------------------------------------------------------------ #
    async def _send_private(
        self,
        event: AstrMessageEvent,
        user_id: str,
        message: str,
        group_id: str = "",
    ) -> bool:
        """私聊发送。带 group_id 时优先走「群临时会话」，对方未加好友也能送达。"""
        bot = getattr(event, "bot", None)
        if not bot or not str(user_id).strip():
            return False
        if not str(user_id).strip().isdigit():
            logger.debug(f"[{PLUGIN_NAME}] 忽略非 QQ 号的收件人：{user_id}")
            return False
        uid = int(user_id)
        gid = int(group_id) if str(group_id).strip() else 0
        api = getattr(bot, "api", None)

        async def via_api(**kwargs) -> None:
            if api is None:
                raise RuntimeError("bot.api 不可用")
            await api.call_action("send_private_msg", **kwargs)

        async def via_bot() -> None:
            await bot.send_private_msg(user_id=uid, message=message)

        attempts: list[tuple[str, Any]] = []
        if gid:
            attempts.append(
                (
                    "群临时会话",
                    lambda: via_api(user_id=uid, message=message, group_id=gid),
                )
            )
        attempts.append(("私聊", via_bot))
        attempts.append(("私聊(call_action)", lambda: via_api(user_id=uid, message=message)))

        for label, fn in attempts:
            try:
                await fn()
                return True
            except Exception as e:
                logger.debug(f"[{PLUGIN_NAME}] {label} 发送给 {user_id} 失败：{e}")
        logger.warning(f"[{PLUGIN_NAME}] 私聊 {user_id} 所有方式均失败")
        return False

    async def _notify_in_group(
        self, event: AstrMessageEvent, group_id: str, at_ids: list[str], text: str
    ) -> None:
        """私聊不可达（未与机器人建立会话）时，退化为群内艾特提醒，保证通知不丢。"""
        ids = self._qq_list(at_ids)
        if not str(group_id).strip() or not ids:
            return
        bot = getattr(event, "bot", None)
        if not bot:
            return
        hint = "【以下人员未与机器人建立私聊，改为群内提醒】\n"
        segments: list[dict[str, Any]] = []
        for uid in ids:
            segments.append({"type": "at", "data": {"qq": uid}})
            segments.append({"type": "text", "data": {"text": " "}})
        segments.append({"type": "text", "data": {"text": "\n" + hint + text}})
        try:
            await bot.api.call_action(
                "send_group_msg", group_id=int(group_id), message=segments
            )
            logger.info(
                f"[{PLUGIN_NAME}] 私聊不可达，已在群 {group_id} 内艾特提醒：{ids}"
            )
            return
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 群内艾特提醒失败，退化为普通群消息：{e}")
        await self._send_group(event, group_id, hint + text)

    async def _send_group(self, event: AstrMessageEvent, group_id: str, message: str):
        bot = getattr(event, "bot", None)
        if not bot or not str(group_id).strip():
            return
        try:
            await bot.send_group_msg(group_id=int(group_id), message=message)
            return
        except Exception as e:
            logger.debug(f"[{PLUGIN_NAME}] send_group_msg 失败，尝试 call_action：{e}")
        try:
            await bot.api.call_action(
                "send_group_msg", group_id=int(group_id), message=message
            )
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 群 {group_id} 发送失败：{e}")

    # ------------------------------------------------------------------ #
    # 缓存与计数
    # ------------------------------------------------------------------ #
    def _recent_messages(
        self, group_id: str, target_id: str, count: int
    ) -> list[dict[str, Any]]:
        queue = self._msg_cache.get(f"{group_id}:{target_id}")
        if not queue:
            return []
        items = list(queue)[-max(1, int(count)):]
        result = []
        for item in items:
            result.append(
                {
                    "text": item["text"],
                    "message_id": item["message_id"],
                    "time_str": time.strftime("%H:%M:%S", time.localtime(item["time"])),
                }
            )
        return result

    @staticmethod
    def _recent_times(recent: list[dict[str, Any]]) -> str:
        if not recent:
            return "（无记录）"
        return "、".join(item["time_str"] for item in recent)

    def _incr_report_count(self, group_id: str, target_id: str) -> int:
        key = f"{group_id}:{target_id}"
        self._report_counts[key] = self._report_counts.get(key, 0) + 1
        return self._report_counts[key]

    def _incr_warn_count(self, group_id: str, target_id: str) -> int:
        key = f"{group_id}:{target_id}"
        self._warn_counts[key] = self._warn_counts.get(key, 0) + 1
        return self._warn_counts[key]

    # ------------------------------------------------------------------ #
    # 权限与限流
    # ------------------------------------------------------------------ #
    def _can_report(self, sender_id: str, conf: dict[str, Any]) -> bool:
        if bool(self._c("allow_everyone_report", True)):
            return True
        whitelist = [str(x) for x in (self._c("reporters_whitelist", []) or [])]
        return sender_id in whitelist

    def _check_rate(
        self, group_id: str, reporter_id: str, conf: dict[str, Any]
    ) -> tuple[bool, str]:
        now = time.time()
        today = time.strftime("%Y-%m-%d")
        key = f"{group_id}:{reporter_id}"
        state = self._rate.setdefault(key, {"last": 0, "day": today, "count": 0})
        if state.get("day") != today:
            state["day"] = today
            state["count"] = 0

        cooldown = int(conf.get("report_cooldown", 60))
        if cooldown > 0 and now - state.get("last", 0) < cooldown:
            return False, "冷却中"

        daily_limit = int(conf.get("daily_limit", 20))
        if daily_limit > 0 and state.get("count", 0) >= daily_limit:
            return False, "已达每日上限"

        state["last"] = now
        state["count"] = state.get("count", 0) + 1
        return True, ""

    # ------------------------------------------------------------------ #
    # 名称/群名解析
    # ------------------------------------------------------------------ #
    async def _resolve_name(
        self, event: AstrMessageEvent, group_id: str, user_id: str
    ) -> str:
        user_id = str(user_id)
        if not user_id:
            return ""
        cached = self._user_names.get(user_id)
        if cached:
            return cached
        bot = getattr(event, "bot", None)
        if bot and group_id:
            try:
                info = await bot.api.call_action(
                    "get_group_member_info",
                    group_id=int(group_id),
                    user_id=int(user_id),
                    no_cache=False,
                )
                name = str(info.get("card") or info.get("nickname") or "").strip()
                if name:
                    self._user_names[user_id] = name
                    return name
            except Exception:
                pass
        return user_id

    async def _stranger_name(self, event: AstrMessageEvent, user_id: str) -> str:
        """取陌生人（未加好友）的 QQ 昵称，避免显示成「临时会话(xxx)」。"""
        bot = getattr(event, "bot", None)
        if not bot:
            return ""
        try:
            info = await bot.api.call_action("get_stranger_info", user_id=int(user_id))
        except Exception:
            return ""
        if not isinstance(info, dict):
            return ""
        return str(info.get("nickname") or info.get("nick") or "").strip()

    async def _resolve_private_name(self, event: AstrMessageEvent, user_id: str) -> str:
        """私聊发送者的展示名：优先群名片缓存，其次 QQ 昵称，最后才用 QQ 号兜底。"""
        user_id = str(user_id)
        cached = self._user_names.get(user_id)
        if cached:
            return cached
        name = ""
        try:
            name = str(event.get_sender_name() or "").strip()
        except Exception:
            name = ""
        # 未加好友时 get_sender_name 会返回「临时会话(123456)」这类占位名
        if not name or "临时会话" in name or user_id in name:
            nick = await self._stranger_name(event, user_id)
            if nick:
                name = nick
        if not name or "临时会话" in name:
            name = user_id
        return name

    async def _get_group_name(self, event: AstrMessageEvent, group_id: str) -> str:
        group_id = str(group_id)
        if group_id in self._group_names:
            return self._group_names[group_id]
        bot = getattr(event, "bot", None)
        name = group_id
        if bot:
            try:
                info = await bot.api.call_action(
                    "get_group_info", group_id=int(group_id)
                )
                name = str(info.get("group_name") or group_id)
            except Exception:
                pass
        self._group_names[group_id] = name
        return name

    # ------------------------------------------------------------------ #
    # 上下文构建
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_ctx(report: dict[str, Any], handler: str = "") -> dict[str, Any]:
        return {
            "group_name": report.get("group_name", report.get("group_id", "")),
            "group_id": report.get("group_id", ""),
            "sender": report.get("sender", ""),
            "target": report.get("target", ""),
            "reporter": report.get("reporter", ""),
            "message": report.get("message", ""),
            "message_id": report.get("message_id", ""),
            "target_report_count": report.get("target_report_count", 0),
            "warn_count": report.get("warn_count", 0),
            "ai_judgement": report.get("ai_judgement", ""),
            "ai_confidence": report.get("ai_confidence", ""),
            "report_id": report.get("report_id", ""),
            "recent_times": report.get("recent_times", ""),
            "recent_count": report.get("recent_count", 0),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "handler": handler,
            "mute_duration": report.get("mute_duration", ""),
        }

    # ------------------------------------------------------------------ #
    # 超时处理
    # ------------------------------------------------------------------ #
    async def _expire_loop(self):
        while True:
            try:
                await asyncio.sleep(60)
                now = time.time()
                changed = False
                for report in list(self._pending.values()):
                    if report.get("handled") or report.get("expired"):
                        continue
                    expire_at = report.get("created", 0) + int(
                        report.get("expire_minutes", 60)
                    ) * 60
                    if now >= expire_at:
                        report["expired"] = True
                        changed = True
                        logger.info(
                            f"[{PLUGIN_NAME}] 举报 {report['report_id']} 已超时未处理"
                        )
                        self._audit("expired", report=report)
                if changed:
                    self._save_state()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] 超时检查异常：{e}")

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #
    def _load_state(self):
        try:
            if self.counters_file.exists():
                data = json.loads(self.counters_file.read_text("utf-8"))
                self._report_counts = data.get("report_counts", {}) or {}
                self._warn_counts = data.get("warn_counts", {}) or {}
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 读取计数失败：{e}")
        try:
            if self.reports_file.exists():
                data = json.loads(self.reports_file.read_text("utf-8"))
                self._pending = data.get("pending", {}) or {}
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 读取举报记录失败：{e}")

    def _save_state(self):
        try:
            self._atomic_write(
                self.counters_file,
                {
                    "report_counts": self._report_counts,
                    "warn_counts": self._warn_counts,
                },
            )
            # 只保留未处理的举报，避免文件无限增长
            pending = {
                rid: r
                for rid, r in self._pending.items()
                if not r.get("handled") and not r.get("expired")
            }
            self._pending = pending
            self._atomic_write(self.reports_file, {"pending": pending})
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 保存状态失败：{e}")

    @staticmethod
    def _atomic_write(path: Path, data: Any):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, path)

    def _audit(self, action: str, *, report: dict[str, Any], extra: dict | None = None):
        try:
            record = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "action": action,
                "report_id": report.get("report_id", ""),
                "group_id": report.get("group_id", ""),
                "target_id": report.get("target_id", ""),
                "reporter_id": report.get("reporter_id", ""),
                "message_id": report.get("message_id", ""),
                "ai_judgement": report.get("ai_judgement", ""),
                "ai_confidence": report.get("ai_confidence", ""),
                "target_report_count": report.get("target_report_count", 0),
                "reason": report.get("reason", ""),
            }
            if extra:
                record.update(extra)
            with self.audit_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"[{PLUGIN_NAME}] 审计日志写入失败：{e}")
