"""群里谁说了什么（speaker 功能模块）

用户需求（m07200 左右）：
- 「她现在群聊里面能不能知道是谁在说话，然后和谁在聊天，或者说谁单独说过什么」

问题：
- AstrBot 的会话历史只存 role/content（user/assistant），不带发言人名字；
  插件每轮注入的【当前说话人】只说明「这一轮是谁在说」，历史里谁说的她并不知道。

做法（不改框架源码、不写历史）：
1. 自定义 filter 在每个群消息到达时记录一条：时间 / 名字 / ID / 是否主人 / 内容前 N 字，
   按 umo 分开存，内存 + 落盘（plugin_data/astrbot_plugin_dafeiyu_core/speaker_log.json）。
2. on_llm_request 时把「最近这些发言」拼成一段临时 TextPart 注入（mark_as_temp，
   不写入历史、不破坏提示词缓存），告诉她谁在场、谁说了什么、对谁说的。
3. 只在群聊注入（私聊有历史就够）；条数/字数/时间窗都可以在配置里调。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.star import Context
from astrbot.core.agent.message import TextPart
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_ID = 'astrbot_plugin_dafeiyu_core'
TZ = timezone(timedelta(hours=8))

DEFAULTS = {
    'enabled': True,
    'bot_filter': [],
    'only_group': True,
    'record_private': False,
    'max_lines': 8,
    'max_chars': 40,
    'window_minutes': 120,
    'max_log_per_umo': 40,
    'max_umos': 60,
    'show_master_mark': True,
    'master_context_enabled': True,
    'master_context_minutes': 30,
    'master_context_lines': 12,
    'master_context_short_chars': 8,
    'context_on_demand': True,
    'context_short_chars': 6,
    'context_question_chars': 12,
}

_RUNTIME: dict = dict(DEFAULTS)
MARK = '【群聊发言记录】'
MARK_MASTER = '【主人这句话可能没头没尾】'
_DEICTIC = ('这个', '那个', '刚才', '上面', '前面', '楼上', '它', '那', '这', '咋', '怎么', '为什么', '为啥', '啥意思', '什么意思', '你说呢', '你觉得')
_QUESTION = ('？', '?', '吗', '呢', '咋', '怎么', '为什么', '为啥', '啥', '多少', '谁', '哪', '是不是', '有没有')
# 带「回指」的说法：没有上下文根本看不出指的是哪件事/哪个人
_BACKREF = (
    '刚才', '刚刚', '刚说', '刚聊', '上面', '楼上', '前面', '之前', '上次', '他们', '她们',
    '他说', '她说', '那个', '这个', '这件事', '那件事', '继续', '接着', '然后呢', '还有呢',
    '你们在聊', '聊什么', '说什么', '你说呢', '你觉得', '你怎么看', '那你说',
)
# 短句里出现这些，才当作「没头没尾」
_PRONOUN = ('他', '她', '它', '这', '那', '啥', '谁', '哪', '咋', '怎么', '为什么', '吗', '什么')
_EXTRA_RECORDED = 'dafeiyu_speaker_recorded'
_LOG: dict[str, list[dict[str, Any]]] = {}
_STATE_PATH: Path | None = None
_LAST_SAVE = 0.0


def _now() -> float:
    return time.time()


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).replace('，', ',').split(',') if part.strip()]


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _apply_runtime(config) -> None:
    for key, default in DEFAULTS.items():
        try:
            value = config.get(key) if config is not None else None
        except Exception:
            value = None
        if value is None:
            value = default
        if key == 'bot_filter':
            _RUNTIME[key] = _as_list(value)
        elif key in (
            'max_lines',
            'max_chars',
            'window_minutes',
            'max_log_per_umo',
            'max_umos',
            'master_context_minutes',
            'master_context_lines',
            'master_context_short_chars',
            'context_short_chars',
            'context_question_chars',
        ):
            _RUNTIME[key] = max(1, _as_int(value, int(DEFAULTS[key])))
        else:
            _RUNTIME[key] = bool(value)
    if not _RUNTIME.get('bot_filter'):
        _RUNTIME['bot_filter'] = list(DEFAULTS['bot_filter'])


def _bot_match(umo: str) -> bool:
    bot_filter = _RUNTIME.get('bot_filter') or []
    if not bot_filter:
        return True
    return any((token in umo for token in bot_filter))


def _state_path(plugin) -> Path:
    global _STATE_PATH
    if _STATE_PATH is None:
        base = getattr(plugin, '_data_dir', None)
        if base is None:
            base = Path(get_astrbot_plugin_data_path()) / PLUGIN_ID
        _STATE_PATH = Path(base) / 'speaker_log.json'
    return _STATE_PATH


def _load(plugin) -> None:
    global _LOG
    path = _state_path(plugin)
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            groups = data.get('groups') if isinstance(data, dict) else None
            if isinstance(groups, dict):
                _LOG = {
                    str(umo): [entry for entry in (items or []) if isinstance(entry, dict)]
                    for umo, items in groups.items()
                }
    except Exception as exc:
        logger.warning('[dafeiyu_speaker] 读取发言记录失败: %s', exc)
        _LOG = {}


def _save(force: bool = False) -> None:
    global _LAST_SAVE
    now = _now()
    if not force and now - _LAST_SAVE < 5.0:
        return
    _LAST_SAVE = now
    if _STATE_PATH is None:
        return
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'version': 1,
            'updated_at': datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S'),
            'groups': _LOG,
        }
        tmp = _STATE_PATH.with_suffix('.tmp')
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        tmp.replace(_STATE_PATH)
    except Exception as exc:
        logger.debug('[dafeiyu_speaker] 保存发言记录失败: %s', exc)


def _sender_id(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_sender_id() or '')
    except Exception:
        return ''


def _sender_name(event: AstrMessageEvent) -> str:
    try:
        name = str(event.get_sender_name() or '')
    except Exception:
        name = ''
    return name.strip()


def _is_master(event: AstrMessageEvent) -> bool:
    try:
        return bool(event.is_admin())
    except Exception:
        return False


def record(event: AstrMessageEvent, text: str | None = None) -> None:
    """记录一条发言（同一条消息在 30 秒内只记一次）。"""
    try:
        if not _RUNTIME.get('enabled', True):
            return
        umo = getattr(event, 'unified_msg_origin', '') or ''
        if not umo or not _bot_match(umo):
            return
        body = (text if text is not None else (event.message_str or '')).strip()
        if not body:
            return
        try:
            group_id = str(event.get_group_id() or '')
        except Exception:
            group_id = ''
        is_group = bool(group_id) or ':GroupMessage:' in umo
        if not is_group and not _RUNTIME.get('record_private', False):
            return
        sid = _sender_id(event)
        name = _sender_name(event) or ('主人' if _is_master(event) else '群友')
        max_chars = _as_int(_RUNTIME.get('max_chars'), 40)
        body = body.replace('\n', ' ').strip()[:max_chars]
        now = _now()
        bucket = int(now // 30)
        items = _LOG.setdefault(umo, [])
        for entry in reversed(items[-6:]):
            if (
                entry.get('s') == sid
                and entry.get('x') == body
                and int((entry.get('t') or 0) // 30) == bucket
            ):
                return
        items.append({
            't': now,
            'n': name,
            's': sid[-6:] if sid else '',
            'm': 1 if _is_master(event) else 0,
            'g': 1 if is_group else 0,
            'x': body,
        })
        limit = _as_int(_RUNTIME.get('max_log_per_umo'), 40)
        if len(items) > limit:
            del items[: len(items) - limit]
        max_umos = _as_int(_RUNTIME.get('max_umos'), 60)
        if len(_LOG) > max_umos:
            ordered = sorted(_LOG.items(), key=lambda kv: (kv[1][-1].get('t') if kv[1] else 0))
            for old_umo, _items in ordered[: len(_LOG) - max_umos]:
                _LOG.pop(old_umo, None)
        _save()
    except Exception as exc:
        logger.debug('[dafeiyu_speaker] 记录发言失败: %s', exc)


def _fmt_time(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts, TZ).strftime('%H:%M')
    except Exception:
        return '--:--'


def _line_of(entry: dict[str, Any]) -> str:
    name = str(entry.get('n') or '群友')
    if _RUNTIME.get('show_master_mark', True) and entry.get('m'):
        tag = '主人'
    else:
        tag = '群友'
    return '- %s %s（%s）：%s' % (_fmt_time(float(entry.get('t') or 0)), name, tag, entry.get('x') or '')


def _needs_context(text: str) -> bool:
    """这句看不懂、才对得上群聊上下文（回指 / 短疑问句 / 纯问句）。"""
    body = (text or '').strip()
    if not body:
        return False
    short = _as_int(_RUNTIME.get('context_short_chars'), 6)
    qmax = _as_int(_RUNTIME.get('context_question_chars'), 12)
    if any(word in body for word in _BACKREF):
        return True
    if len(body) <= short and any(word in body for word in _QUESTION + _PRONOUN):
        return True
    if body.endswith(('？', '?')) and len(body) <= qmax:
        return True
    return False


def digest_block(event: AstrMessageEvent) -> str:
    """拼出「最近的群聊发言」临时块（排除本轮这条，它已经作为用户消息进了上下文）。"""
    try:
        if not _RUNTIME.get('enabled', True):
            return ''
        umo = getattr(event, 'unified_msg_origin', '') or ''
        if not umo or not _bot_match(umo):
            return ''
        try:
            group_id = str(event.get_group_id() or '')
        except Exception:
            group_id = ''
        if _RUNTIME.get('only_group', True) and not group_id and ':GroupMessage:' not in umo:
            return ''
        items = _LOG.get(umo) or []
        if not items:
            return ''
        if _RUNTIME.get('context_on_demand', True):
            if _is_master(event):
                # 主人的「没头没尾」交给 master_context_block 单独补，避免补两遍。
                return ''
            if not _needs_context(event.message_str or ''):
                logger.info('[dafeiyu_speaker] 这句能看懂，不读群聊上下文: %s', umo)
                return ''
        now = _now()
        window = _as_int(_RUNTIME.get('window_minutes'), 120) * 60
        max_lines = _as_int(_RUNTIME.get('max_lines'), 8)
        sid = _sender_id(event)
        current = (event.message_str or '').strip().replace('\n', ' ')[: _as_int(_RUNTIME.get('max_chars'), 40)]
        picked: list[dict[str, Any]] = []
        for entry in reversed(items):
            if now - float(entry.get('t') or 0) > window:
                continue
            if entry.get('s') == (sid[-6:] if sid else '') and (entry.get('x') or '') == current and not picked:
                continue  # 跳过本轮这条
            picked.append(entry)
            if len(picked) >= max_lines:
                break
        if not picked:
            return ''
        picked.reverse()
        lines = [MARK + '这几条群消息你都在场（按时间先后，含你没回的）：']
        lines.extend(_line_of(entry) for entry in picked)
        lines.append('回答时按这个记录分清是谁说的、在跟谁说话；问的是别人说的内容就照记录答，别把两个人的话搞混。')
        logger.info('[dafeiyu_speaker] 这句看不懂，补了 %s 条群聊上下文: %s', len(picked), umo)
        return '\n'.join(lines)
    except Exception as exc:
        logger.debug('[dafeiyu_speaker] 生成发言记录失败: %s', exc)
        return ''


def master_context_block(event: AstrMessageEvent) -> str:
    """主人这句可能没头没尾时，把刚才别人在聊的补给她，让她自己猜主人在说哪件事。

    判定（只群聊、只主人、非命令）：短句 <= master_context_short_chars，或含指代/疑问词且 <=20 字，
    或问句 <=16 字；并且最近 master_context_minutes 分钟里主人没自己聊过别的。
    """
    try:
        if not _RUNTIME.get('master_context_enabled', True):
            return ''
        if not _is_master(event):
            return ''
        umo = getattr(event, 'unified_msg_origin', '') or ''
        if not umo or not _bot_match(umo):
            return ''
        try:
            group_id = str(event.get_group_id() or '')
        except Exception:
            group_id = ''
        if not group_id and ':GroupMessage:' not in umo:
            return ''
        text = (event.message_str or '').strip()
        if not text or text.startswith('/') or len(text) > 40:
            return ''
        max_chars = _as_int(_RUNTIME.get('max_chars'), 40)
        short_chars = _as_int(_RUNTIME.get('master_context_short_chars'), 8)
        short = len(text) <= short_chars and any(word in text for word in _QUESTION + _PRONOUN)
        deictic = len(text) <= 20 and any(word in text for word in _DEICTIC)
        if not (short or deictic):
            return ''
        items = _LOG.get(umo) or []
        if not items:
            return ''
        now = _now()
        window = _as_int(_RUNTIME.get('master_context_minutes'), 30) * 60
        sid = _sender_id(event)
        mine = sid[-6:] if sid else ''
        current = text.replace('\n', ' ')[:max_chars]
        recent = [entry for entry in items if now - float(entry.get('t') or 0) <= window]
        if not recent:
            return ''
        own_talk = [
            entry
            for entry in recent
            if entry.get('m') and entry.get('s') == mine and (entry.get('x') or '') != current
        ]
        if own_talk:
            return ''
        others = [
            entry
            for entry in recent
            if not entry.get('m') and not (entry.get('s') == mine and (entry.get('x') or '') == current)
        ]
        if not others:
            return ''
        limit = _as_int(_RUNTIME.get('master_context_lines'), 12)
        picked = others[-limit:]
        lines = [
            MARK_MASTER + '主人这句像是接着刚才的聊天说的，但只看到这一句，看不出指的是哪件事/哪个人。',
            '刚才群里在聊：',
        ]
        lines.extend(_line_of(entry) for entry in picked)
        lines.append('主人刚说：「%s」' % current)
        lines.append('先看上面别人在聊什么，猜主人指的是哪件事、哪个人，再顺着接；猜不准就直接说没看懂、让主人说清楚，别装懂、别编。')
        logger.info('[dafeiyu_speaker] 主人没头没尾，补了 %s 条上下文: %s', len(picked), umo)
        return '\n'.join(lines)
    except Exception as exc:
        logger.debug('[dafeiyu_speaker] 生成主人上下文失败: %s', exc)
        return ''


class SpeakerRecordFilter(CustomFilter):
    """每条群消息都记一笔（返回 False，不影响唤醒判定）。"""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        try:
            record(event)
        except Exception:
            pass
        return False


class SpeakerFeature:

    @filter.custom_filter(SpeakerRecordFilter, False)
    async def track_speaker(self, event: AstrMessageEvent):
        try:
            record(event)
        except Exception as exc:
            logger.debug('[dafeiyu_speaker] 记录发言失败: %s', exc)

    @filter.on_llm_request(priority=5045)
    async def inject_speaker_digest(self, event: AstrMessageEvent, req):
        try:
            record(event)
            block = digest_block(event)
            if not block:
                return
            part = TextPart(text=block)
            try:
                part.mark_as_temp()
            except Exception:
                pass
            req.extra_user_content_parts.append(part)
        except Exception as exc:
            logger.warning('[dafeiyu_speaker] 注入群聊发言记录失败: %s', exc)

    @filter.on_llm_request(priority=5043)
    async def inject_master_context(self, event: AstrMessageEvent, req):
        """主人说话没头没尾时，把刚才别人在聊的补上，让她自己猜主人指什么。"""
        try:
            record(event)
            block = master_context_block(event)
            if not block:
                return
            part = TextPart(text=block)
            try:
                part.mark_as_temp()
            except Exception:
                pass
            req.extra_user_content_parts.append(part)
        except Exception as exc:
            logger.warning('[dafeiyu_speaker] 注入主人上下文失败: %s', exc)

