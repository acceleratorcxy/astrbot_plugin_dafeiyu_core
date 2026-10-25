"""大肥鱼联网搜索能力（search_feature）

需求（用户 m06103 / m06170）：
- 群聊/私聊里有人问她不知道的、或者有时效性的东西（比赛、新闻、天气、价格、
  版本、人物近况……），她应该自己去搜，而不是让用户把信息贴上来、更不是瞎编。
- 搜索走 DeepSeek 官方 Search / Fetch API（api.deepseek.com/v1/tools/search 与
  /v1/tools/fetch，2026-10 上线，用与模型相同的 Bearer Key，每 1000 次成功请求 5 元），
  Key 默认自动从 AstrBot 的 DeepSeek provider 配置里取；失败时兜底百度 AI 搜索。

本模块只做插件能力，不改 AstrBot 源码：
1. LLM 工具 web_search / web_fetch —— 任何说话人都能用（搜索/抓网页是只读操作）。
2. 每轮临时上下文注入「该搜就搜」的提醒（TextPart 标记为 temp，不写历史、不破坏缓存）。
3. 频率保护：同一会话冷却 + 每日总次数上限，避免乱烧额度。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from astrbot.api import logger, llm_tool
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.agent.message import TextPart

DEFAULTS = {
    "enabled": True,
    "provider": "deepseek",          # deepseek | baidu
    "count": 5,                      # 默认返回条数
    "need_content": False,           # 是否让 DeepSeek 返回网页正文
    "fallback_baidu": True,
    "timeout_seconds": 20,
    "daily_limit": 80,               # 每日搜索总次数上限（0=不限）
    "cooldown_seconds": 6,           # 同一会话两次搜索最小间隔
    "hint_enabled": True,            # 每轮注入「该搜就搜」提示
    "brand_hint": "",                # 额外追加给她的搜索提示（留空用内置）
}

DEEPSEEK_SEARCH_URL = "https://api.deepseek.com/v1/tools/search"
DEEPSEEK_FETCH_URL = "https://api.deepseek.com/v1/tools/fetch"
BAIDU_SEARCH_URL = "https://qianfan.baidubce.com/v2/ai_search/web_search"
HINT_MARK = "【联网搜索】"

_RUNTIME: dict = dict(DEFAULTS)
_KEY_CACHE: dict = {"key": "", "at": 0.0}
_STATE: dict = {"day": "", "count": 0, "last": {}}
_STATE_PATH: Path | None = None


# --------------------------------------------------------------------- helpers
def _apply_runtime(section) -> None:
    global _RUNTIME
    merged = dict(DEFAULTS)
    if isinstance(section, dict):
        for key in DEFAULTS:
            if section.get(key) is not None:
                merged[key] = section.get(key)
    _RUNTIME = merged


def _as_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def _state_path(plugin) -> Path | None:
    global _STATE_PATH
    if _STATE_PATH is not None:
        return _STATE_PATH
    try:
        data_dir = Path(str(plugin._data_dir))
        data_dir.mkdir(parents=True, exist_ok=True)
        _STATE_PATH = data_dir / "search_state.json"
    except Exception as exc:
        logger.warning("[dafeiyu_search] 取数据目录失败: %s", exc)
        return None
    return _STATE_PATH


def _load_state(plugin) -> None:
    global _STATE
    path = _state_path(plugin)
    if path is None or not path.exists():
        return
    try:
        raw = json.loads(path.read_text("utf-8"))
        if isinstance(raw, dict):
            _STATE.update(raw)
    except Exception as exc:
        logger.warning("[dafeiyu_search] 读取搜索状态失败: %s", exc)


def _save_state() -> None:
    if _STATE_PATH is None:
        return
    try:
        _STATE_PATH.write_text(json.dumps(_STATE, ensure_ascii=False, indent=1), "utf-8")
    except Exception as exc:
        logger.warning("[dafeiyu_search] 保存搜索状态失败: %s", exc)


def _reset_day_if_needed() -> None:
    today = _today()
    if str(_STATE.get("day") or "") != today:
        _STATE["day"] = today
        _STATE["count"] = 0


def _rate_ok(umo: str) -> tuple[bool, str]:
    """同会话冷却 + 每日上限。"""
    _reset_day_if_needed()
    limit = _as_int(_RUNTIME.get("daily_limit"), 80)
    if limit > 0 and _as_int(_STATE.get("count"), 0) >= limit:
        return False, "今天搜得够多了"
    cooldown = _as_float(_RUNTIME.get("cooldown_seconds"), 6.0)
    last = _STATE.setdefault("last", {})
    now = time.time()
    if cooldown > 0 and umo and now - _as_float(last.get(umo), 0.0) < cooldown:
        return False, "刚搜过"
    if umo:
        last[umo] = now
        if len(last) > 500:
            for key in sorted(last, key=lambda k: _as_float(last.get(k), 0.0))[:-200]:
                last.pop(key, None)
    _STATE["count"] = _as_int(_STATE.get("count"), 0) + 1
    _save_state()
    return True, ""


def _config_dict(plugin) -> dict:
    try:
        cfg = plugin.context.get_config()
        return cfg if isinstance(cfg, dict) else {}
    except Exception as exc:
        logger.debug("[dafeiyu_search] 读全局配置失败: %s", exc)
        return {}


def _deepseek_key(plugin) -> str:
    """优先用插件里手填的 key，其次自动取 DeepSeek provider 的 key。"""
    global _KEY_CACHE
    own = str(_RUNTIME.get("api_key") or "").strip()
    if own:
        return own
    now = time.time()
    if _KEY_CACHE.get("key") and now - _as_float(_KEY_CACHE.get("at"), 0.0) < 300:
        return str(_KEY_CACHE["key"])
    key = ""
    cfg = _config_dict(plugin)
    sources = cfg.get("provider_sources") or []
    if isinstance(sources, list):
        for item in sources:
            if not isinstance(item, dict):
                continue
            tag = " ".join(str(item.get(k) or "") for k in ("provider", "id", "api_base", "type"))
            if "deepseek" not in tag.lower():
                continue
            raw = item.get("key")
            if isinstance(raw, list):
                raw = raw[0] if raw else ""
            key = str(raw or "").strip()
            if key:
                break
    _KEY_CACHE = {"key": key, "at": now}
    return key


async def _deepseek_search(plugin, query: str, count: int = 5, need_content: bool = False) -> list[dict]:
    key = _deepseek_key(plugin)
    if not key:
        return []
    payload = {
        "query": query[:128],
        "count": max(1, min(50, count)),
        "need_content": bool(need_content),
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    timeout = _as_float(_RUNTIME.get("timeout_seconds"), 20.0)
    try:
        import aiohttp
    except Exception:
        return []
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(DEEPSEEK_SEARCH_URL, json=payload, headers=headers) as resp:
                body = await resp.text()
                if resp.status != 200:
                    logger.warning("[dafeiyu_search] DeepSeek 搜索失败 HTTP %s: %s", resp.status, body[:200])
                    return []
                data = json.loads(body)
    except Exception as exc:
        logger.warning("[dafeiyu_search] DeepSeek 搜索异常: %s", exc)
        return []
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return []
    cleaned = []
    for item in results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        if not url and not title:
            continue
        cleaned.append({
            "title": title,
            "url": url,
            "snippet": str(item.get("snippet") or "").strip(),
            "summary": str(item.get("summary") or "").strip(),
            "content": str(item.get("content") or "").strip(),
            "site_name": str(item.get("site_name") or "").strip(),
            "date": str(item.get("date") or "").strip(),
            "authority": item.get("authority"),
        })
    return cleaned


async def _deepseek_fetch(plugin, url: str) -> dict:
    key = _deepseek_key(plugin)
    if not key:
        return {}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    timeout = _as_float(_RUNTIME.get("timeout_seconds"), 20.0)
    try:
        import aiohttp
    except Exception:
        return {}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(DEEPSEEK_FETCH_URL, json={"url": url}, headers=headers) as resp:
                body = await resp.text()
                if resp.status != 200:
                    logger.warning("[dafeiyu_search] DeepSeek 抓取失败 HTTP %s: %s", resp.status, body[:200])
                    return {}
                data = json.loads(body)
    except Exception as exc:
        logger.warning("[dafeiyu_search] DeepSeek 抓取异常: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


async def _baidu_search(plugin, query: str, top_k: int = 5) -> list[dict]:
    """兜底：百度 AI 搜索（用全局 provider_settings 里的 app builder key）。"""
    cfg = _config_dict(plugin)
    provider_settings = cfg.get("provider_settings") or {}
    key = str(provider_settings.get("websearch_baidu_app_builder_key", "") or "").strip()
    if not key:
        return []
    try:
        import aiohttp
    except Exception:
        return []
    payload = {
        "messages": [{"role": "user", "content": query[:72]}],
        "search_source": "baidu_search_v2",
        "resource_type_filter": [{"type": "web", "top_k": max(1, min(10, top_k))}],
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "X-Appbuilder-Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    timeout = _as_float(_RUNTIME.get("timeout_seconds"), 20.0)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(BAIDU_SEARCH_URL, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning("[dafeiyu_search] 百度搜索失败 HTTP %s", resp.status)
                    return []
                data = await resp.json(content_type=None)
    except Exception as exc:
        logger.warning("[dafeiyu_search] 百度搜索异常: %s", exc)
        return []
    refs = data.get("references")
    if not isinstance(refs, list):
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        refs = inner.get("references") if isinstance(inner, dict) else []
    out = []
    for ref in refs or []:
        if not isinstance(ref, dict):
            continue
        url = str(ref.get("url") or "").strip()
        if not url:
            continue
        out.append({
            "title": str(ref.get("title") or "").strip(),
            "url": url,
            "snippet": str(ref.get("content") or ref.get("snippet") or "").strip(),
            "summary": "",
            "content": "",
            "site_name": str(ref.get("site_name") or "").strip(),
            "date": str(ref.get("date") or "").strip(),
            "authority": None,
        })
    return out


def _remember_topic(query: str) -> None:
    """搜过的话题不算兴趣，只丢进「临时观察」，聊得多才升级（兴趣短期化）。"""
    try:
        import sys as _sys
        mod = _sys.modules.get(__name__.rsplit(".", 1)[0] + ".interest_feature")
        store = getattr(mod, "_STORE", None) if mod is not None else None
        if store is None:
            return
        topic = _clean_topic(query)
        if not topic:
            return
        if store.get_by_name(topic) is not None:
            return
        store.remember_candidate(topic, source="search")
    except Exception:
        pass


def _clean_topic(query: str) -> str:
    """把搜索词收拾成像个话题：去掉日期、今天/今日之类的时效词，最长 20 字。"""
    import re as _re
    text = str(query or "").strip()
    text = _re.sub(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日?", " ", text)
    text = _re.sub(r"\d{4}\s*年\s*\d{1,2}\s*月?", " ", text)
    text = _re.sub(r"\d{1,2}\s*月\s*\d{1,2}\s*日", " ", text)
    text = _re.sub(r"\d{1,2}\s*月", " ", text)
    text = _re.sub(r"\b(?:today|now)\b", " ", text, flags=_re.IGNORECASE)
    for word in ("今天", "今日", "今晚", "现在", "目前", "最新", "最近", "怎么样", "什么", "多少"):
        text = text.replace(word, " ")
    text = _re.sub(r"[?？!！。，,]+", " ", text)
    text = _re.sub(r"\s+", " ", text).strip()
    return text[:20].strip()


def _clean(text: str, limit: int = 260) -> str:
    import re as _re
    return _re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _format_results(results: list[dict], limit: int = 5) -> str:
    lines = []
    for idx, item in enumerate(results[:limit], 1):
        title = _clean(item.get("title"), 80)
        site = _clean(item.get("site_name"), 20)
        date = _clean(item.get("date"), 20)[:10]
        url = item.get("url") or ""
        body = _clean(item.get("summary") or item.get("snippet") or item.get("content"), 300)
        extra = " · ".join(x for x in (site, date) if x)
        head = f"{idx}. {title}" + (f"（{extra}）" if extra else "")
        lines.append(head)
        if body:
            lines.append(f"   {body}")
        if url:
            lines.append(f"   {url}")
    return "\n".join(lines)


def _hint_block() -> str:
    brand = str(_RUNTIME.get("brand_hint") or "").strip()
    block = (
        HINT_MARK + " 遇到你不知道、记不准、或者有时效性的东西（比赛赛程/比分、新闻、"
        "天气、价格、版本更新、新番、人物近况、某个东西是什么）先调用 web_search 搜一下再回答，"
        "需要看整篇文章再调用 web_fetch。搜不到就直说没找到，不要编，也不要把问题丢回给对方让他自己贴。"
        "用你自己的话讲重点，别念链接清单，别提你用了什么工具。"
        "interest_search 只用来查你自己兴趣库里记着的东西，不要拿它查临时新闻或比赛。"
    )
    if brand:
        block += brand
    return block


# --------------------------------------------------------------------- feature
class SearchFeature:
    """联网搜索：工具 + 每轮提示注入。"""

    @filter.on_llm_request(priority=5030)
    async def inject_search_hint(self, event: AstrMessageEvent, req):
        try:
            if not bool(_RUNTIME.get("enabled", True)) or not bool(_RUNTIME.get("hint_enabled", True)):
                return
            if not (event.message_str or "").strip():
                return
            if req is None:
                return
            block = _hint_block()
            for part in getattr(req, "extra_user_content_parts", []) or []:
                if HINT_MARK in str(getattr(part, "text", "") or ""):
                    return
            part = TextPart(text=block)
            try:
                part.mark_as_temp()
            except Exception:
                pass
            req.extra_user_content_parts.append(part)
        except Exception as exc:
            logger.warning("[dafeiyu_search] 注入搜索提示失败: %s", exc)

    @llm_tool(name="web_search")
    async def web_search(self, event: AstrMessageEvent, query: str, count: int = 0) -> str:
        """上网搜索实时信息或你不确定的内容，返回标题、摘要和链接。

        Args:
            query(string): 搜索词，尽量具体，比如「英雄联盟 S16 今天赛程」「上海今天天气」。
            count(number): 返回几条，1~10，留空默认 5。
        """
        if not bool(_RUNTIME.get("enabled", True)):
            return "联网搜索没开，先按你知道的说，不确定就说不确定，别编。"
        query = str(query or "").strip()
        if not query:
            return "要搜什么？给个关键词。"
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        ok, reason = _rate_ok(umo)
        if not ok:
            return f"{reason}，先按你知道的说；不确定就直说不确定，别编。"
        want = _as_int(count, 0)
        if want <= 0:
            want = _as_int(_RUNTIME.get("count"), 5)
        want = max(1, min(10, want))
        results: list[dict] = []
        if str(_RUNTIME.get("provider") or "deepseek").lower() == "deepseek":
            results = await _deepseek_search(self, query, want, bool(_RUNTIME.get("need_content", False)))
        if not results and bool(_RUNTIME.get("fallback_baidu", True)):
            results = await _baidu_search(self, query, want)
        if not results:
            logger.info("[dafeiyu_search] 没搜到：%s", query)
            return f"搜「{query}」没搜到东西，可能是网络或搜索服务的问题；先照你知道的答，不确定就说不确定。"
        logger.info("[dafeiyu_search] 搜索「%s」→ %s 条", query, len(results))
        _remember_topic(query)
        return f"「{query}」的搜索结果：\n" + _format_results(results, want)

    @llm_tool(name="web_fetch")
    async def web_fetch(self, event: AstrMessageEvent, url: str) -> str:
        """抓取一个网页的正文（Markdown），用来细看搜索结果里某一篇。

        Args:
            url(string): 要抓取的网页地址（http/https）。
        """
        if not bool(_RUNTIME.get("enabled", True)):
            return "联网功能没开。"
        url = str(url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return "网址不对，要 http/https 开头的完整链接。"
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        ok, reason = _rate_ok(umo)
        if not ok:
            return f"{reason}，先别抓了。"
        page = await _deepseek_fetch(self, url)
        title = str(page.get("title") or "").strip()
        content = str(page.get("content") or "").strip()
        if not content:
            if bool(_RUNTIME.get("fallback_baidu", True)):
                results = await _baidu_search(self, url, 3)
                if results:
                    return "直接抓正文没成功，这是搜索到的摘要：\n" + _format_results(results, 3)
            return "这个页面抓不到正文，可能是站点限制；换个来源或直接回答。"
        logger.info("[dafeiyu_search] 抓取 %s（%s 字）", url, len(content))
        clipped = content[:4000]
        head = f"《{title}》" if title else url
        return f"{head}\n（正文前 4000 字）\n{clipped}"
