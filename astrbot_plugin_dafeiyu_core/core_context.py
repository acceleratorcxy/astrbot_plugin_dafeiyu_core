# -*- coding: utf-8 -*-
"""上下文守护：快满提醒 + 主人确认后压缩 + 压缩后恢复人格资料。

设计要点：
- 每次 LLM 请求前估算上下文 token（复用 AstrBot 的 EstimateTokenCounter 估算规则），
  超过 warn_ratio（默认 0.75）且冷却已过时，主动给用户发一条提醒，让用户回 /压缩。
- /压缩：把旧对话交给 LLM 总结成一段「压缩记忆」，只保留最近 keep_recent_ratio 的
  对话写回 DB，压缩记忆与人格资料复原块在后续请求里以 system prompt 片段注入。
- 压缩完成后读取最新的人格资料备份（BackupManager.latest），把人格设定、主人设定、
  兴趣备忘拼成「人格资料复原」块，接下来若干轮以临时 TextPart 注入。
- 群聊（umo 含 :GroupMessage:）不需要主人确认：后台巡检会在三种情况下静默自动压缩，
  分别是「消息暂停后上下文余量已经很高」「群聊安静了一段时间」「每天到点做一次日常整理」。
- 状态文件 context_state.json 存 summaries / warn / persona_restore / groups，插件重启不丢。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.core.agent.message import TextPart
from astrbot.core.agent.context.token_counter import EstimateTokenCounter
from astrbot.core.message.message_event_result import MessageEventResult

SUMMARY_MARK = "【压缩记忆】"
RESTORE_MARK = "【人格资料复原】"
GROUP_MARK = ":GroupMessage:"
DEFAULT_MAX_CONTEXT = 128000


def _now() -> float:
    return time.time()


def _load_json(path: Path, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _label_of_umo(umo: str) -> str:
    """把 unified_msg_origin 变成人看的标签，例如「aiocqhttp 群聊 987654321」。"""
    parts = str(umo or "").split(":")
    platform = parts[0] if parts else "未知平台"
    mtype = parts[1] if len(parts) > 1 else ""
    target = ":".join(parts[2:]) if len(parts) > 2 else ""
    if "Group" in mtype:
        kind = "群聊"
    elif "Friend" in mtype or "Private" in mtype:
        kind = "私聊"
    else:
        kind = mtype or "会话"
    return f"{platform} {kind}" + (f" {target[:14]}" if target else "")


# ---------------------------------------------- 会话名（群名 / 私聊昵称）
_NAME_CACHE: dict[str, tuple[float, str]] = {}
_NAME_TTL = 600.0


def _pretty_label(umo: str, name: str = "") -> str:
    """有名字就用名字（群名 / 私聊昵称），没名字才退回编号。"""
    name = str(name or "").strip()
    parts = str(umo or "").split(":")
    target = ":".join(parts[2:]) if len(parts) > 2 else ""
    if not name or name == target or (name.isdigit() and len(name) > 8):
        return _label_of_umo(umo)
    mtype = parts[1] if len(parts) > 1 else ""
    platform = parts[0] if parts else "未知平台"
    if "Group" in mtype:
        kind = "群聊"
    elif "Friend" in mtype or "Private" in mtype:
        kind = "私聊"
    else:
        kind = mtype or "会话"
    return f"{platform} {kind}「{name}」"


async def umo_names(plugin: Any, umos: list[str]) -> dict[str, str]:
    """批量取会话名（AstrBot 的 umo_aliases.auto_name / user_alias），带缓存。"""
    result: dict[str, str] = {}
    wanted = [str(u or "") for u in (umos or []) if u]
    if not wanted:
        return result
    now = time.time()
    missing = [
        u
        for u in wanted
        if u not in _NAME_CACHE or now - _NAME_CACHE[u][0] > _NAME_TTL
    ]
    if missing:
        try:
            from sqlalchemy import text

            db = plugin.context.get_db()
            for start in range(0, len(missing), 40):
                chunk = missing[start:start + 40]
                params = {f"u{i}": value for i, value in enumerate(chunk)}
                holes = ",".join(f":u{i}" for i in range(len(chunk)))
                async with db.get_db() as session:
                    rows = await session.execute(
                        text(
                            "SELECT umo, auto_name, user_alias FROM umo_aliases "
                            f"WHERE umo IN ({holes})"
                        ),
                        params,
                    )
                    for umo, auto_name, user_alias in rows.fetchall():
                        _NAME_CACHE[str(umo)] = (
                            now,
                            str(user_alias or auto_name or "").strip(),
                        )
        except Exception as exc:
            logger.warning("[dafeiyu_core] 读取会话名失败: %s", exc)
        for umo in missing:
            if umo not in _NAME_CACHE:
                _NAME_CACHE[umo] = (now, "")
    for umo in wanted:
        result[umo] = _NAME_CACHE.get(umo, (0.0, ""))[1]
    return result


# ---------------------------------------------- 配置档案（汇报只算自己的会话）
_OWN_PROFILE_CACHE: dict[str, set[str]] = {}
DEFAULT_OWN_PROFILES: tuple = ()


def _section_cfg(plugin: Any, name: str) -> dict[str, Any]:
    try:
        cfg = plugin._section(name)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _profile_of_umo(plugin: Any, umo: str) -> str:
    """这个会话属于哪个配置档案（uuid；找不到返回空串）。"""
    if not umo:
        return ""
    acm = getattr(getattr(plugin, "context", None), "astrbot_config_mgr", None)
    if acm is None:
        return ""
    try:
        info = acm._load_conf_mapping(str(umo))
    except Exception:
        return ""
    return str((info or {}).get("id") or "")


def profile_name(plugin: Any, conf_id: str) -> str:
    """配置档案 uuid -> 名字（v4pro / 大肥鱼 …）。"""
    acm = getattr(getattr(plugin, "context", None), "astrbot_config_mgr", None)
    data = getattr(acm, "abconf_data", None) or {}
    meta = data.get(conf_id) if isinstance(data, dict) else None
    if isinstance(meta, dict) and meta.get("name"):
        return str(meta["name"])
    return str(conf_id or "")[:8]


def own_profile_ids(plugin: Any) -> set[str]:
    """鲸鲸自己的配置档案 id。

    notify.report_profile_ids 优先；留空时按机器人平台（bot_filter 前缀）在
    umop_config_routing 里自动找对应档案，实在找不到就用默认的两个。
    """
    cfg = _section_cfg(plugin, "notify")
    if not bool(cfg.get("report_only_own_profile", True)):
        return set()
    raw = cfg.get("report_profile_ids") or []
    if isinstance(raw, str):
        raw = list(raw.replace("，", ",").split(","))
    ids = {str(item).strip() for item in raw if str(item).strip()}
    if ids:
        return ids
    cached = _OWN_PROFILE_CACHE.get("auto")
    if cached:
        return set(cached)
    found: set[str] = set()
    prefixes: set[str] = set()
    for name in ("sleep", "group_wake", "speaker", "owner", "humanizer"):
        for item in _section_cfg(plugin, name).get("bot_filter") or []:
            text = str(item).strip()
            if text:
                prefixes.add(text)
    acm = getattr(getattr(plugin, "context", None), "astrbot_config_mgr", None)
    table = getattr(getattr(acm, "ucr", None), "umop_to_conf_id", None) or {}
    for pattern, conf_id in dict(table).items():
        platform = str(pattern).split(":", 1)[0]
        if platform in prefixes and conf_id:
            found.add(str(conf_id))
    _OWN_PROFILE_CACHE["auto"] = set(found)
    return found



def _text_of_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in ("text", "plain") and part.get("text"):
                    parts.append(str(part["text"]))
                elif part.get("text"):
                    parts.append(str(part["text"]))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return str(content or "")


class ContextGuard:
    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self._state_path = Path(plugin._data_dir) / "context_state.json"
        self._state: dict[str, Any] = {
            "summaries": {},
            "warn": {},
            "persona_restore": None,
            "groups": {},
        }
        self._counter = EstimateTokenCounter()
        self._task = None
        self._load()

    # ------------------------------------------------------------------ state
    def _load(self) -> None:
        data = _load_json(self._state_path, None)
        if isinstance(data, dict):
            for key in ("summaries", "warn", "groups"):
                if not isinstance(data.get(key), dict):
                    data[key] = {}
            if not isinstance(data.get("persona_restore"), dict):
                data["persona_restore"] = None
            self._state = data

    def _save(self) -> None:
        try:
            _save_json(self._state_path, self._state)
        except Exception as exc:
            logger.warning("[dafeiyu_core] 保存上下文状态失败: %s", exc)

    @property
    def cfg(self) -> dict:
        return self.plugin._section("context_guard")

    # ------------------------------------------------------------- estimates
    def _estimate_text(self, text: str) -> int:
        try:
            return self._counter._estimate_tokens(text or "")
        except Exception:
            return len(text or "") // 2

    def _estimate_messages(self, messages: Any) -> int:
        total = 0
        if not isinstance(messages, list):
            return 0
        for msg in messages:
            try:
                if isinstance(msg, dict):
                    total += self._estimate_text(_text_of_content(msg.get("content")))
                    total += 4
                else:
                    total += self._estimate_text(
                        _text_of_content(getattr(msg, "content", ""))
                    )
                    total += 4
            except Exception:
                total += 8
        return total

    def _context_limit(self, provider: Any) -> int:
        try:
            cfg = getattr(provider, "provider_config", None) or {}
            limit = int(cfg.get("max_context_tokens") or 0)
        except Exception:
            limit = 0
        if limit <= 0:
            try:
                from astrbot.core.provider.entities import LLM_METADATAS

                meta = LLM_METADATAS.get(getattr(provider, "get_model", lambda: "")())
                limit = int((meta or {}).get("limit", {}).get("context") or 0)
            except Exception:
                limit = 0
        if limit <= 0:
            try:
                acm_conf = self.plugin.context.get_config()
                flow = acm_conf.get("provider_settings", {}) or {}
                limit = int(flow.get("fallback_max_context_tokens") or 0)
            except Exception:
                limit = 0
        return limit if limit > 0 else DEFAULT_MAX_CONTEXT

    def _usage(self, req: Any, provider: Any) -> tuple[int, int, float]:
        used = self._estimate_messages(getattr(req, "contexts", []) or [])
        used += self._estimate_text(getattr(req, "system_prompt", "") or "")
        used += self._estimate_text(getattr(req, "prompt", "") or "")
        limit = self._context_limit(provider)
        return used, limit, (used / limit if limit else 0.0)

    # ---------------------------------------------------------- current conv
    async def _conversation(self, umo: str):
        cm = self.plugin.context.conversation_manager
        cid = await cm.get_curr_conversation_id(umo)
        if not cid:
            return None, None
        conv = await cm.get_conversation(umo, cid)
        try:
            history = json.loads(conv.history or "[]") if conv else []
        except Exception:
            history = []
        if not isinstance(history, list):
            history = []
        return conv, history

    # -------------------------------------------------------- request hook
    async def before_llm_request(self, event, req) -> None:
        if not bool(self.cfg.get("enabled", True)):
            return
        try:
            umo = getattr(event, "unified_msg_origin", "") or ""
            provider = None
            try:
                provider = await self.plugin.context.get_using_provider_async(umo)
            except Exception:
                provider = None
            used, limit, ratio = self._usage(req, provider)
            conv, history = await self._conversation(umo)
            cid = getattr(conv, "cid", None) if conv else None

            # 1) 注入压缩记忆（稳定块，追加到 system prompt 末尾）
            summary = self._state["summaries"].get(cid) if cid else None
            if summary and summary.get("text"):
                block = SUMMARY_MARK + str(summary.get("text")).strip()
                sp = req.system_prompt or ""
                if SUMMARY_MARK not in sp:
                    req.system_prompt = (sp.rstrip() + "\n\n" + block) if sp.strip() else block

            # 2) 注入人格资料复原块（临时，不写历史）
            restore = self._state.get("persona_restore")
            if isinstance(restore, dict) and restore.get("text"):
                part = TextPart(text=RESTORE_MARK + restore["text"])
                try:
                    part.mark_as_temp()
                except Exception:
                    pass
                req.extra_user_content_parts.append(part)
                try:
                    restore["turns"] = int(restore.get("turns") or 0) - 1
                    if restore["turns"] <= 0:
                        self._state["persona_restore"] = None
                    self._save()
                except Exception:
                    pass

            # 3) 快满提醒（只对私聊：群聊由后台自动压缩，不打扰主人）
            warn_ratio = float(self.cfg.get("warn_ratio", 0.75) or 0.75)
            cooldown = float(self.cfg.get("warn_cooldown_minutes", 30) or 30) * 60
            if ratio >= warn_ratio and cid and GROUP_MARK not in umo:
                last = float((self._state["warn"].get(cid) or {}).get("at") or 0)
                if _now() - last >= cooldown:
                    pct = ratio * 100
                    pct_text = f"{pct:.1f}%" if pct >= 0.1 else "不到 1%"
                    msg = (
                        f"（上下文快满了，现在大概用了 {pct_text}。"
                        "要我把前面聊的压缩成一段记忆吗？回复 /压缩 我就整理，"
                        "不想现在压也可以继续聊。）"
                    )
                    self._state["warn"][cid] = {"at": _now(), "ratio": round(ratio, 3)}
                    self._save()
                    # 只私聊发给主人：上下文用量是隐私，绝不往群聊里发
                    sent = []
                    try:
                        sent = await self.plugin.send_private_to_master(msg, event)
                    except Exception as exc:
                        logger.warning("[dafeiyu_core] 发送上下文提醒失败: %s", exc)
                    if sent:
                        logger.info("[dafeiyu_core] 上下文提醒已私聊发给主人: %s", ",".join(sent))
        except Exception as exc:
            logger.warning("[dafeiyu_core] 上下文检查失败: %s", exc)

    # ------------------------------------------------------------ compress
    async def _summarize(
        self, event, old_messages: list, provider, prev_summary: str = ""
    ) -> str:
        lines = []
        for msg in old_messages:
            role = "用户" if str((msg or {}).get("role")) == "user" else "大肥鱼"
            text = _text_of_content((msg or {}).get("content")).strip()
            if not text:
                continue
            if len(text) > 800:
                text = text[:800] + "…"
            lines.append(f"{role}：{text}")
        transcript = "\n".join(lines)
        if len(transcript) > 12000:
            transcript = transcript[:6000] + "\n……（中间略）……\n" + transcript[-6000:]

        persona_prompt = ""
        try:
            persona = self.plugin.context.persona_manager.get_persona_v3_by_id("大肥鱼")
            persona_prompt = (persona or {}).get("prompt", "") or ""
        except Exception:
            persona_prompt = ""
        instruction = str(self.cfg.get("compress_instruction") or "").strip()
        if not instruction:
            instruction = (
                "你在帮一个叫大肥鱼的聊天人格做长期记忆整理。请把下面这段聊天压缩成"
                "不超过 300 字的记忆摘要：保留人物关系、发生过的事、用户的偏好和约定、"
                "大肥鱼答应过的事、重要的情绪与梗；删掉寒暄和重复内容。用第三人称陈述，"
                "不要写成给用户看的回复，直接输出摘要正文。"
            )
        prev_block = ""
        if prev_summary:
            prev_block = (
                "\n\n之前已经压缩过的记忆（请把新内容并进去，不要丢掉里面的事实）：\n"
                + prev_summary.strip()[:2000]
            )
        prompt = f"{instruction}{prev_block}\n\n聊天记录：\n{transcript}"
        summary = ""
        try:
            if provider is not None:
                resp = await provider.text_chat(
                    prompt=prompt,
                    contexts=[],
                    system_prompt=persona_prompt,
                    func_tool=None,
                    model=None,
                )
                summary = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:
            logger.warning("[dafeiyu_core] LLM 总结失败，回退为关键词摘要: %s", exc)
        if not summary:
            user_bits = [
                _text_of_content((m or {}).get("content"))[:60]
                for m in old_messages
                if str((m or {}).get("role")) == "user"
            ]
            summary = "之前聊到过：" + "；".join([b for b in user_bits if b][:8]) + "。"
        return summary

    async def _compress_umo(
        self, umo: str, conv, history: list, apply_restore: bool = True
    ) -> dict[str, Any]:
        """把旧消息压成记忆摘要，只保留最近的一部分写回 DB。"""
        keep_ratio = float(self.cfg.get("compress_keep_recent_ratio", 0.15) or 0.15)
        keep_ratio = max(0.05, min(0.5, keep_ratio))
        keep_count = max(2, int(len(history) * keep_ratio))
        if keep_count % 2 == 1:
            keep_count += 1
        old_messages = history[:-keep_count]
        recent = history[-keep_count:]

        prev = self._state["summaries"].get(conv.cid) or {}
        prev_text = str(prev.get("text") or "").strip()
        provider = None
        try:
            provider = await self.plugin.context.get_using_provider_async(umo)
        except Exception:
            provider = None
        summary = await self._summarize(None, old_messages, provider, prev_text)

        cm = self.plugin.context.conversation_manager
        await cm.update_conversation(
            umo,
            conv.cid,
            history=recent,
            token_usage=0,
        )
        self._state["summaries"][conv.cid] = {
            "text": summary,
            "at": _now(),
            "compressed_messages": int(prev.get("compressed_messages") or 0)
            + len(old_messages),
        }
        self._state["warn"].pop(conv.cid, None)

        restore_note = ""
        if apply_restore and bool(self.cfg.get("auto_reload_persona", True)):
            try:
                restore_note = await self._apply_persona_restore()
            except Exception as exc:
                logger.warning("[dafeiyu_core] 读取人格备份失败: %s", exc)
        self._save()
        return {
            "cid": conv.cid,
            "old": len(old_messages),
            "recent": len(recent),
            "summary_len": len(summary),
            "restore_note": restore_note,
        }

    async def compress_conversation(self, event) -> str:
        umo = getattr(event, "unified_msg_origin", "") or ""
        conv, history = await self._conversation(umo)
        if not conv or not history:
            return "这边还没有聊天记录，不用压缩。"
        min_messages = int(self.cfg.get("min_messages", 6) or 6)
        if len(history) < min_messages:
            return f"才聊了 {len(history)} 条，还不用压。"
        res = await self._compress_umo(umo, conv, history, apply_restore=True)
        reply = (
            f"压好了：把前面 {res['old']} 条聊成了 {res['summary_len']} 字的记忆，"
            f"最近 {res['recent']} 条原样留着。"
        )
        if res.get("restore_note"):
            reply += res["restore_note"]
        return reply

    # ------------------------------------------------ group auto compress
    def _group_cfg(self, key: str, default):
        """读群聊自动压缩配置：0 与 false 是有效值，只有 None/空串才回退默认。"""
        value = self.cfg.get(key, default)
        if value is None or value == "":
            return default
        return value

    async def _auto_compress_group(
        self, umo: str, conv, history: list, reason: str
    ) -> dict[str, Any] | None:
        try:
            res = await self._compress_umo(umo, conv, history, apply_restore=False)
        except Exception as exc:
            logger.warning("[dafeiyu_core] 群聊自动压缩失败 %s: %s", umo, exc)
            return None
        cid = res.get("cid") or getattr(conv, "cid", "") or ""
        state = self._state["groups"].get(cid) or {}
        state.update(
            {
                "umo": umo,
                "last_auto": _now(),
                "last_reason": reason,
                "last_old": res["old"],
                "last_recent": res["recent"],
            }
        )
        if reason == "daily":
            state["daily"] = time.strftime("%Y-%m-%d")
        self._state["groups"][cid] = state
        self._save()
        logger.info(
            "[dafeiyu_core] 群聊自动压缩(%s): %s 压了 %d 条，留 %d 条，摘要 %d 字",
            reason,
            umo,
            res["old"],
            res["recent"],
            res["summary_len"],
        )
        return res

    async def scan_groups_once(self) -> list[dict]:
        """巡检所有群聊会话，按「余量高 / 安静了 / 到点日常整理」自动压缩。"""
        if not bool(self.cfg.get("enabled", True)):
            return []
        if not bool(self._group_cfg("group_auto_compress", True)):
            return []
        cm = getattr(self.plugin.context, "conversation_manager", None)
        if cm is None:
            return []
        page_size = max(10, int(self._group_cfg("group_scan_limit", 200)))
        try:
            convs, _total = await cm.get_filtered_conversations(
                page=1, page_size=page_size
            )
        except Exception as exc:
            logger.warning("[dafeiyu_core] 扫描群聊会话失败: %s", exc)
            return []
        min_messages = max(4, int(self._group_cfg("group_min_messages", 20)))
        pause = max(0, float(self._group_cfg("group_pause_seconds", 45)))
        idle_need = max(60, float(self._group_cfg("group_idle_minutes", 30)) * 60)
        ratio_need = float(self._group_cfg("group_auto_ratio", 0.7))
        daily_hour = int(self._group_cfg("group_daily_hour", 4))
        now = _now()
        today = time.strftime("%Y-%m-%d")
        local_hour = time.localtime().tm_hour
        done: list[dict] = []
        for conv in convs or []:
            umo = getattr(conv, "user_id", "") or ""
            if GROUP_MARK not in umo:
                continue
            cid = getattr(conv, "cid", None) or ""
            try:
                history = json.loads(getattr(conv, "history", "") or "[]")
            except Exception:
                history = []
            if not isinstance(history, list) or len(history) < min_messages:
                continue
            updated = float(getattr(conv, "updated_at", 0) or 0)
            idle = (now - updated) if updated > 0 else idle_need
            if idle < pause:
                # 还在聊，等消息停下来再动，免得把进行中的对话压掉
                continue
            provider = None
            try:
                provider = await self.plugin.context.get_using_provider_async(umo)
            except Exception:
                provider = None
            limit = self._context_limit(provider)
            used = self._estimate_messages(history)
            ratio = (used / limit) if limit else 0.0
            state = self._state["groups"].get(cid) or {}
            daily_due = (
                daily_hour >= 0
                and local_hour >= daily_hour
                and state.get("daily") != today
            )
            if ratio >= ratio_need:
                reason = "ratio"
            elif idle >= idle_need:
                reason = "idle"
            elif daily_due:
                reason = "daily"
            else:
                continue
            res = await self._auto_compress_group(umo, conv, history, reason)
            if res:
                row = {"umo": umo, "cid": cid, "reason": reason}
                row.update({k: v for k, v in res.items() if k != "restore_note"})
                done.append(row)
        return done

    def start_background(self) -> None:
        if getattr(self, "_task", None) is not None:
            return
        if not bool(self.cfg.get("enabled", True)):
            return
        if not bool(self._group_cfg("group_auto_compress", True)):
            logger.info("[dafeiyu_core] 群聊自动压缩已关闭")
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._auto_loop())
        logger.info(
            "[dafeiyu_core] 已启动群聊自动压缩（每 %s 秒巡检一次）",
            max(30, int(self._group_cfg("group_check_interval_seconds", 120))),
        )

    async def _auto_loop(self) -> None:
        try:
            await asyncio.sleep(20)
            while True:
                try:
                    await self.scan_groups_once()
                except Exception as exc:
                    logger.warning("[dafeiyu_core] 群聊自动压缩巡检异常: %s", exc)
                interval = max(30, int(self._group_cfg("group_check_interval_seconds", 120)))
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[dafeiyu_core] 群聊自动压缩循环退出: %s", exc)

    def stop_background(self) -> None:
        task = getattr(self, "_task", None)
        if task is not None and not task.done():
            try:
                task.cancel()
            except Exception:
                pass
        self._task = None

    # ------------------------------------------------- persona restore block
    async def _apply_persona_restore(self) -> str:
        data, meta = await self.plugin._backup.latest()
        if not data:
            return "（不过还没找到人格备份，先用现有设定继续。）"
        parts = [f"（已从 {meta.get('filename', '最新备份')} 重新装载人格资料。）"]
        persona_text = ""
        for item in data.get("personas") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("persona_id")) == "大肥鱼":
                persona_text = str(item.get("system_prompt") or "")
                break
        if persona_text:
            parts.append(
                "人格设定要点：" + persona_text[:400].replace("\n", " ").strip()
            )
        owner_cfg = ((data.get("plugin_configs") or {}).get("self") or {}).get("config") or {}
        owner_sec = owner_cfg.get("owner") if isinstance(owner_cfg, dict) else None
        if isinstance(owner_sec, dict) and owner_sec.get("master_ids"):
            name = owner_sec.get("master_name") or "主人"
            parts.append(f"主人是 {name}（{'、'.join(map(str, owner_sec['master_ids']))}），只认这几个 ID。")
        interests = data.get("interests") or {}
        items = interests.get("interests") if isinstance(interests, dict) else None
        if isinstance(items, list) and items:
            pinned = []
            for item in items[:4]:
                if not isinstance(item, dict):
                    continue
                notes = item.get("notes") or []
                note_text = ""
                if notes and isinstance(notes[0], dict):
                    note_text = str(notes[0].get("text") or "")[:60]
                pinned.append(
                    f"{item.get('name')}（喜欢程度 {item.get('affinity')}）"
                    + (f"：{note_text}" if note_text else "")
                )
            if pinned:
                parts.append("她惦记的兴趣：" + "；".join(pinned))
        text = "\n".join(parts).strip()
        turns = int(self.cfg.get("persona_restore_turns", 6) or 6)
        self._state["persona_restore"] = {
            "text": text,
            "turns": max(1, turns),
            "at": _now(),
            "source": meta.get("filename"),
        }
        return "人格资料也从最新备份重新过了一遍。"

    # ------------------------------------------------------ context report
    async def conversation_rows(
        self, page_size: int = 40, own_only: bool = False
    ) -> dict[str, Any]:
        """每个会话的上下文用量（结构化），页面和 /报告 共用。

        own_only=True 时只留鲸鲸自己配置档案的会话（别人档案的机器人不列）。
        """
        cm = getattr(self.plugin.context, "conversation_manager", None)
        if cm is None:
            return {"rows": [], "total": 0}
        try:
            convs, total = await cm.get_filtered_conversations(
                page=1, page_size=max(1, int(page_size or 40))
            )
        except Exception as exc:
            logger.warning("[dafeiyu_core] 读取会话列表失败: %s", exc)
            return {"rows": [], "total": 0}
        rows: list[dict[str, Any]] = []
        for conv in convs or []:
            umo = getattr(conv, "user_id", "") or ""
            cid = getattr(conv, "cid", None) or ""
            try:
                history = json.loads(getattr(conv, "history", "") or "[]")
            except Exception:
                history = []
            if not isinstance(history, list):
                history = []
            provider = None
            try:
                provider = await self.plugin.context.get_using_provider_async(umo)
            except Exception:
                provider = None
            limit = self._context_limit(provider)
            used = self._estimate_messages(history)
            summary = self._state["summaries"].get(cid) or {}
            group_state = (self._state.get("groups") or {}).get(cid) or {}
            try:
                updated_at = float(getattr(conv, "updated_at", 0) or 0)
            except Exception:
                updated_at = 0.0
            rows.append(
                {
                    "umo": umo,
                    "cid": cid,
                    "profile_id": _profile_of_umo(self.plugin, umo),
                    "label": _label_of_umo(umo),
                    "kind": "group" if GROUP_MARK in umo else "private",
                    "messages": len(history),
                    "used": used,
                    "limit": limit,
                    "ratio": (used / limit) if limit else 0.0,
                    "summary": bool(summary),
                    "summary_at": summary.get("at") or 0,
                    "summary_len": len(str(summary.get("text") or "")),
                    "summary_text": str(summary.get("text") or "")[:160],
                    "updated_at": updated_at,
                    "group_auto_at": group_state.get("last_auto") or 0,
                    "group_auto_reason": group_state.get("last_reason") or "",
                    "has_group_state": bool(group_state),
                }
            )
        if own_only:
            owned = own_profile_ids(self.plugin)
            if owned:
                rows = [row for row in rows if row["profile_id"] in owned]
                total = len(rows)
        names = await umo_names(self.plugin, [row["umo"] for row in rows])
        for row in rows:
            row["name"] = names.get(row["umo"], "")
            row["label"] = _pretty_label(row["umo"], row["name"])
        rows.sort(key=lambda row: row["ratio"], reverse=True)
        return {"rows": rows, "total": total if total else len(rows)}

    @staticmethod
    def _report_lines(
        data: dict[str, Any], limit: int = 40, own_only: bool = False
    ) -> list[str]:
        rows = (data or {}).get("rows") or []
        count = (data or {}).get("total") or len(rows)
        scope = "只算鲸鲸自己的档案" if own_only else "全部档案"
        lines = [f"【每个对话的上下文】{scope} · {count} 个"]
        if not rows:
            lines.append("  · 暂时还没有对话")
            return lines
        for row in rows[:max(1, limit)]:
            pct = row["ratio"] * 100
            if pct >= 1:
                pct_text = f"{pct:.0f}%"
            elif pct >= 0.1:
                pct_text = f"{pct:.1f}%"
            else:
                pct_text = "不到 0.1%"
            extra = " · 已压缩" if row["summary"] else ""
            lines.append(f"  · {row['label']}：{row['messages']} 条 · {pct_text}{extra}")
        if len(rows) > limit:
            lines.append(f"  · …还有 {len(rows) - limit} 个会话（页面「上下文」tab 看全部）")
        return lines

    async def context_report(
        self, page_size: int = 40, own_only: bool = False
    ) -> list[str]:
        """每个会话的上下文用量，给主人看的（可只算鲸鲸自己的配置档案）。"""
        try:
            data = await self.conversation_rows(page_size, own_only=own_only)
        except Exception as exc:
            logger.warning("[dafeiyu_core] 生成会话上下文列表失败: %s", exc)
            return ["【每个对话的上下文】这次没读到会话列表，等会儿再看。"]
        return self._report_lines(data, own_only=own_only)

    # ----------------------------------------------------------- status api
    async def _latest_umo(self) -> str | None:
        """页面用：没指定 umo 时挑最近活跃的那个会话。"""
        cm = getattr(self.plugin.context, "conversation_manager", None)
        if cm is None:
            return None
        try:
            convs, _total = await cm.get_filtered_conversations(page=1, page_size=1)
        except Exception:
            return None
        if not convs:
            return None
        return getattr(convs[0], "user_id", None)

    async def status(self, umo: str | None = None) -> dict:
        out: dict[str, Any] = {
            "enabled": bool(self.cfg.get("enabled", True)),
            "warn_ratio": self.cfg.get("warn_ratio", 0.75),
            "summaries": dict(self._state["summaries"]),
            "summary_count": len(self._state["summaries"]),
            "persona_restore": self._state.get("persona_restore"),
            "group_auto": {
                "enabled": bool(self._group_cfg("group_auto_compress", True)),
                "daily_hour": self._group_cfg("group_daily_hour", 4),
                "idle_minutes": self._group_cfg("group_idle_minutes", 30),
                "pause_seconds": self._group_cfg("group_pause_seconds", 45),
                "min_messages": self._group_cfg("group_min_messages", 20),
                "auto_ratio": self._group_cfg("group_auto_ratio", 0.7),
                "check_interval_seconds": self._group_cfg(
                    "group_check_interval_seconds", 120
                ),
            },
            "group_states": dict(self._state.get("groups") or {}),
        }
        if not umo:
            umo = await self._latest_umo()
            out["auto_umo"] = umo
        if umo:
            conv, history = await self._conversation(umo)
            provider = None
            try:
                provider = await self.plugin.context.get_using_provider_async(umo)
            except Exception:
                provider = None
            limit = self._context_limit(provider)
            used = self._estimate_messages(history)
            cid = getattr(conv, "cid", None) if conv else None
            out.update(
                {
                    "umo": umo,
                    "cid": cid,
                    "messages": len(history or []),
                    "estimated_tokens": used,
                    "max_tokens": limit,
                    "ratio": round(used / limit, 3) if limit else 0,
                    "has_summary": bool(cid and self._state["summaries"].get(cid)),
                }
            )
        return out
