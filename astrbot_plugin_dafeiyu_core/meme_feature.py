# -*- coding: utf-8 -*-
"""主动发表情包（接 meme_manager 的图包）

背景：meme_manager 的配图路径是「情感 LLM 选分类 + 装饰阶段自动加图」，
本机语义检索没启用，它没有「按需发图」的工具；人格被问「发张图 / 来个表情包」
时手里没工具，就会回「发不了图」。

本模块给 LLM 一个 send_meme 工具：
- 只读 meme_manager 的 plugin_data（selection_rules / registry / packs），不改它的配置；
- 按「心情词」匹配当前生效图包的分类，分类里随机取一张；
- 直接发到当前会话（event.send），不写进回复正文。

配置段：meme（enabled / max_per_hour）。
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path

from astrbot.api import logger, llm_tool
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

DEFAULTS = {"enabled": True, "max_per_hour": 20}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

_RUNTIME: dict = dict(DEFAULTS)
_RECENT: dict[str, list[float]] = {}
_LAST_PICK: dict[str, str] = {}


def _apply_runtime(section: dict | None) -> None:
    global _RUNTIME
    merged = dict(DEFAULTS)
    if isinstance(section, dict):
        for key in DEFAULTS:
            if section.get(key) is not None:
                merged[key] = section.get(key)
    _RUNTIME = merged


def _meme_root() -> Path:
    try:
        return Path(get_astrbot_plugin_data_path()) / "meme_manager"
    except Exception:
        return Path("")


def _read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _resolve_pack() -> tuple[str, Path | None]:
    root = _meme_root()
    if not root or not root.exists():
        return "", None
    rules = _read_json(root / "selection_rules.json", {}) or {}
    chosen = ""
    for rule in rules.get("rules") or []:
        if str(rule.get("scope") or "") == "default":
            chosen = str(rule.get("pack_id") or "")
    registry = _read_json(root / "registry.json", {}) or {}
    installed = {
        str(pack.get("id") or ""): pack
        for pack in (registry.get("installed_packs") or [])
        if isinstance(pack, dict)
    }
    pack_id = chosen if chosen in installed else ""
    if not pack_id:
        for pid, pack in installed.items():
            if bool(pack.get("enabled")):
                pack_id = pid
                break
    if not pack_id and installed:
        pack_id = next(iter(installed))
    if not pack_id:
        return "", None
    pack_dir = root / "packs" / pack_id
    return pack_id, (pack_dir if pack_dir.is_dir() else None)


def _load_categories(pack_dir: Path) -> dict:
    manifest = _read_json(pack_dir / "manifest.json", {}) or {}
    categories = manifest.get("categories")
    if isinstance(categories, dict) and categories:
        result = {}
        for key, value in categories.items():
            desc = ""
            if isinstance(value, dict):
                desc = str(value.get("description") or "")
            elif isinstance(value, str):
                desc = value
            result[str(key)] = desc
        return result
    data = _read_json(pack_dir / "memes_data.json", {}) or {}
    return {str(k): str(v) for k, v in data.items()}


def _mood_category(mood: str, categories: dict) -> str:
    names = list(categories)
    if not names:
        return ""
    mood = (mood or "").strip().lower()
    if not mood:
        return random.choice(names)
    tokens = {mood}
    for i in range(len(mood) - 1):
        tokens.add(mood[i : i + 2])
    best, best_score = "", 0
    for name in names:
        desc = f"{name} {categories.get(name, '')}".lower()
        score = 0
        for token in tokens:
            if not token:
                continue
            if token == name or token in desc:
                score += len(token)
        if score > best_score:
            best, best_score = name, score
    return best or random.choice(names)


def _pick_image(pack_dir: Path, category: str, umo: str = "") -> Path | None:
    memes_dir = pack_dir / "memes"
    candidates: list[Path] = []
    folder = memes_dir / category
    if category and folder.is_dir():
        candidates = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    if not candidates and memes_dir.is_dir():
        candidates = [p for p in memes_dir.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    if not candidates:
        return None
    last = _LAST_PICK.get(umo)
    pool = [p for p in candidates if str(p) != last] or candidates
    return random.choice(pool)


def _allow(umo: str) -> bool:
    try:
        limit = int(_RUNTIME.get("max_per_hour") or 20)
    except Exception:
        limit = 20
    now = time.time()
    hits = [t for t in _RECENT.get(umo, []) if now - t < 3600]
    _RECENT[umo] = hits
    return len(hits) < max(1, limit)


def _record(umo: str, path: Path) -> None:
    _RECENT.setdefault(umo, []).append(time.time())
    _LAST_PICK[umo] = str(path)


class MemeFeature:
    """主动发图 mixin（由 main.Main 多继承）。"""

    def _meme_cfg(self, key: str, default=None):
        try:
            section = self._section("meme")
        except Exception:
            section = {}
        if isinstance(section, dict) and section.get(key) is not None:
            return section.get(key)
        return DEFAULTS.get(key, default)

    @llm_tool(name="send_meme")
    async def send_meme(self, event: AstrMessageEvent, mood: str = "") -> str:
        """给当前聊天发一张表情包/梗图。

        想发表情、被要求发图、或者光靠文字不够表达情绪时调用。一次回复最多发一张。

        Args:
            mood(string): 想表达的心情或场景，一两个词就够，比如 开心、无语、困了、吃瓜、震惊、撒娇、生气；不确定就留空。
        """
        try:
            if not bool(self._meme_cfg("enabled", True)):
                return "表情包功能关着，直接说话就好。"
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if not _allow(umo):
                return "一小时里发太多图了，先聊聊别的。"
            pack_id, pack_dir = _resolve_pack()
            if pack_dir is None:
                return "表情包库没找到，先正常回复。"
            categories = _load_categories(pack_dir)
            category = _mood_category(str(mood or ""), categories)
            path = _pick_image(pack_dir, category, umo)
            if path is None:
                return "这会儿没找到合适的图，先正常回复。"
            await event.send(MessageChain([Image.fromFileSystem(str(path))]))
            _record(umo, path)
            logger.info(
                "[dafeiyu_core] 已发表情包 %s (%s/%s)",
                path.name,
                pack_id,
                category,
            )
            return "图已经发出去了，接着用你自己的语气回一句就好。"
        except Exception as exc:
            logger.warning("[dafeiyu_core] send_meme 失败: %s(%r)", type(exc).__name__, exc)
            return "发图的时候卡住了，先正常回复。"
