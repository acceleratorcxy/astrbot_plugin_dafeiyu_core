"""协议端内置命令静默词表：群里出现协议端自带的命令时，直接当没看见。

aiocqhttp 2.1.1 自带 statusCommand（默认触发词 #sl），任何人发了都会回一句
「aiocqhttp 状态 / 版本 / 平台 / 运行时长」，等于把协议端信息泄到群里。协议端那边
已经关掉（见文档 20.34），这里再加一道插件侧保险：命中词表、或以 # 开头的短消息时，
所有群聊唤醒 filter 直接返回 False，既不唤醒也不回复。

配置在「群聊唤醒」段：
- silent_prefixes：命中前缀，不区分大小写；默认 ["#sl", "#napcat", "#status", "#version", "#nb"]
- silent_hash_message：以 # 或 ＃ 开头的短消息一律不回（默认开）
- silent_hash_max_chars：上面那条的长度上限（默认 24 字）

注意：filter 里抛异常会被框架当作用户可见错误（往群里发报错并停事件），
所以这里所有分支都吞异常返回 False。
"""
from __future__ import annotations

from typing import Any

from astrbot.api import logger

DEFAULTS = {
    "silent_prefixes": ["#sl", "#napcat", "#status", "#version", "#nb"],
    "silent_hash_message": True,
    "silent_hash_max_chars": 24,
}
DEFAULT_PREFIXES = list(DEFAULTS["silent_prefixes"])

_RUNTIME: dict = dict(DEFAULTS)
_EXTRA_CHECKED = "dafeiyu_silent_checked"
_EXTRA_HIT = "dafeiyu_silent_hit"


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        text = value
        for sep in (",", "，", "、", "|", ";", "；", " ", "\n", "\t"):
            text = text.replace(sep, "\n")
        items = text.split("\n")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    out = []
    for item in items:
        token = str(item).strip()
        if token and token not in out:
            out.append(token)
    return out


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "y", "是", "开"):
        return True
    if text in ("0", "false", "no", "off", "n", "否", "关"):
        return False
    return default


def apply(config) -> None:
    """把插件配置的「群聊唤醒」段同步进来（启动和保存配置时都会调）。"""
    section = config if isinstance(config, dict) else {}
    for key, default in DEFAULTS.items():
        try:
            value = section.get(key)
        except Exception:
            value = None
        if value is None:
            value = default
        if key == "silent_prefixes":
            items = _as_list(value)
            _RUNTIME[key] = items or list(DEFAULT_PREFIXES)
        elif key == "silent_hash_message":
            _RUNTIME[key] = _as_bool(value, bool(default))
        else:
            try:
                _RUNTIME[key] = int(value)
            except Exception:
                _RUNTIME[key] = int(default)


def snapshot() -> dict:
    return {
        "prefixes": list(_RUNTIME.get("silent_prefixes") or []),
        "hash_message": bool(_RUNTIME.get("silent_hash_message", True)),
        "hash_max_chars": int(_RUNTIME.get("silent_hash_max_chars", 24) or 24),
    }


def _text_of(event) -> str:
    try:
        return str(getattr(event, "message_str", "") or "").strip()
    except Exception:
        return ""


def _reason(text: str) -> str:
    lowered = text.lower()
    for token in list(_RUNTIME.get("silent_prefixes") or []):
        if token and lowered.startswith(str(token).lower()):
            return "前缀 " + str(token)
    max_chars = int(_RUNTIME.get("silent_hash_max_chars", 24) or 24)
    if lowered[:1] in ("#", "＃") and len(text) <= max_chars:
        return "井号短消息"
    return ""


def is_silent(event) -> bool:
    """这条消息是不是协议端命令（命中返回 True，调用方应直接 return False）。"""
    try:
        try:
            if event.get_extra(_EXTRA_CHECKED):
                return bool(event.get_extra(_EXTRA_HIT))
        except Exception:
            pass
        text = _text_of(event)
        reason = ""
        if text:
            try:
                if not _RUNTIME.get("silent_hash_message", True) and text[:1] in ("#", "＃"):
                    reason = ""
                else:
                    reason = _reason(text)
            except Exception:
                reason = ""
        hit = bool(reason)
        try:
            event.set_extra(_EXTRA_CHECKED, True)
            event.set_extra(_EXTRA_HIT, hit)
        except Exception:
            pass
        if hit:
            try:
                logger.info(
                    "[dafeiyu] 忽略协议端命令（%s），不唤醒不回复: %s | %s",
                    reason,
                    getattr(event, "unified_msg_origin", ""),
                    text[:40],
                )
            except Exception:
                pass
        return hit
    except Exception:
        return False
