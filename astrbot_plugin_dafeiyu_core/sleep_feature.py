# -*- coding: utf-8 -*-
"""大肥鱼睡眠模式（sleep_feature）。

需求（m08466）：
- 安静期（默认 1:00 起）：群里、别人的私聊都不回，只悄悄记下来；
- 睡眠期（默认 2:00 起，或主人私聊说晚安后立刻）：所有人的消息都不回，
  主人的消息也排队到早上；
- 早上（默认 6:00 + 0~45 分钟随机）自己醒来：先回排队的主人私聊和其他私聊，
  再翻一遍睡着之后的群聊，被叫到、提到名字或命中兴趣的才回；
- 睡眠期间只有主人私聊里的「起床 / 醒醒 / 别睡了 / 强制唤醒」能叫醒；
- 主人说晚安时她只回一个「安」，等主人再回一句「安」才真的去睡（m13907）；
- 夜里不再打扰主人，备份和整理挪到睡眠时间（backup.hour 默认 3 点）。

实现要点：
- 用 CustomFilter（同步）在唤醒阶段调 event.stop_event() 把整条消息拦住，
  这样后面的 LLM / 其它插件都不会回这条消息；
- 拦截时把消息记进 sleep_state.json，早上由后台任务统一处理；
- 早安回话按 umo 取 provider 直接 text_chat 生成，再主动发出去；
- 群聊只回「被 @ / 提到名字 / 兴趣相关」的，其它的只看不回。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import random
import sys
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.message_components import At, AtAll

NL = chr(10)
GROUP_MARK = ':GroupMessage:'
_EXTRA_GATE = 'dafeiyu_sleep_gate'
_EXTRA_WAKE = 'dafeiyu_sleep_wake'

DEFAULTS: dict[str, Any] = {
    'enabled': True,
    'bot_filter': [],
    'quiet_hour': 1,
    'sleep_hour': 2,
    'wake_hour': 6,
    'wake_jitter_minutes': 45,
    'goodnight_reply': '安',
    'goodnight_keywords': ['晚安', '睡了', '去睡', '睡觉', '困了', '我睡', '先睡', '拜拜', '歇了'],
    'wake_keywords': ['起床', '醒醒', '别睡', '不睡', '起来', '强制唤醒', '醒一醒'],
    'sleep_confirm_enabled': True,
    'sleep_confirm_seconds': 600,
    'sleep_confirm_words': ['安'],
    'sleep_confirm_reply': '嗯',
    'other_goodnight_reply': True,
    'wake_other_private': True,
    'catchup_enabled': True,
    'catchup_max_groups': 8,
    'catchup_max_messages': 30,
    'catchup_interest_score': 0.2,
    'morning_report': True,
    'notify_on_auto_sleep': True,
    'max_pending_per_umo': 20,
    'max_group_buffer': 40,
    'check_interval_seconds': 30,
    # 一次睡眠最多睡多久（小时）。白天误触晚安时兜底，不让它一觉睡到明天。
    'max_sleep_hours': 10,
    # 刚醒不到这么久，不接「晚安/困了」的入睡流程，按正常聊天走。
    'min_awake_minutes': 30,
    'reply_style': '短句、像 QQ 聊天、不要解释自己在睡觉或刚醒',
}

_RUNTIME: dict[str, Any] = dict(DEFAULTS)
_PLUGIN: Any = None
_STATE: dict[str, Any] = {}
_SAVED_AT = 0.0
_LAST_TICK = 0.0
_TASK: Any = None
_WAKING = False


# --------------------------------------------------------------- 小工具
def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).replace('，', ',').split(',') if part.strip()]


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _apply_runtime(config: Any) -> None:
    for key, default in DEFAULTS.items():
        try:
            value = config.get(key) if config is not None else None
        except Exception:
            value = None
        if value is None:
            value = default
        if key in ('bot_filter', 'goodnight_keywords', 'wake_keywords', 'sleep_confirm_words'):
            _RUNTIME[key] = _as_list(value) or list(default)  # type: ignore[arg-type]
        elif isinstance(default, bool):
            _RUNTIME[key] = bool(value)
        elif isinstance(default, float):
            _RUNTIME[key] = _as_float(value, float(default))
        elif isinstance(default, int):
            _RUNTIME[key] = int(_as_float(value, float(default)))
        else:
            _RUNTIME[key] = str(value or '').strip() or str(default)


def _bot_match(umo: str) -> bool:
    ucfirst = _RUNTIME.get('bot_filter') or []
    if not ucfirst:
        return True
    return any((str(token) in umo) for token in ucfirst)


def _sibling(name: str) -> Any:
    for key, mod in list(sys.modules.items()):
        if key.endswith(name) and mod is not None:
            return mod
    return None


def _is_master(event: AstrMessageEvent) -> bool:
    mod = _sibling('owner_feature')
    if mod is not None and hasattr(mod, '_is_master'):
        try:
            if mod._is_master(event):
                return True
        except Exception:
            pass
    try:
        return bool(event.is_admin())
    except Exception:
        return False


def _interest_store() -> Any:
    mod = _sibling('interest_feature')
    return getattr(mod, '_STORE', None) if mod is not None else None


def _wake_words() -> list[str]:
    mod = _sibling('group_wake_feature')
    runtime = getattr(mod, '_RUNTIME', None) if mod is not None else None
    if isinstance(runtime, dict):
        words = runtime.get('keywords')
        if isinstance(words, list) and words:
            return [str(w) for w in words if str(w).strip()]
    return ['大肥鱼', '肥鱼', '鲸', '鱼鱼']


def _persona_prompt() -> str:
    plugin = _PLUGIN
    if plugin is None:
        return ''
    try:
        manager = plugin.context.persona_manager
    except Exception:
        return ''
    for getter in ('get_persona_v3_by_id', 'get_persona_by_id'):
        fn = getattr(manager, getter, None)
        if fn is None:
            continue
        try:
            persona = fn('大肥鱼')
        except Exception:
            persona = None
        if isinstance(persona, dict) and persona.get('prompt'):
            return str(persona.get('prompt') or '')
    try:
        for persona in manager.get_all_personas() or []:
            data = persona if isinstance(persona, dict) else {}
            name = str(data.get('name') or data.get('persona_id') or '')
            if '大肥鱼' in name and data.get('prompt'):
                return str(data.get('prompt') or '')
    except Exception:
        pass
    return ''


# --------------------------------------------------------------- 状态
def _empty_state() -> dict[str, Any]:
    return {
        'version': 1,
        'sleeping': False,
        'phase': 'awake',
        'sleep_at': 0.0,
        'wake_at': 0.0,
        'sleep_date': '',
        'wake_date': '',
        'last_wake_at': 0.0,
        'reason': '',
        'pending_master': {},
        'pending_other': {},
        'group_buffer': {},
        'stats': {'quiet_skipped': 0, 'sleep_skipped': 0, 'wake_runs': 0, 'catchup_replies': 0, 'last_wake': 0},
    }


def _state_path() -> Path:
    base = getattr(_PLUGIN, '_data_dir', None) if _PLUGIN is not None else None
    return Path(base or '.') / 'sleep_state.json'


def _load_state(plugin: Any) -> None:
    global _PLUGIN, _STATE, _LAST_TICK
    _PLUGIN = plugin
    state = _empty_state()
    path = _state_path()
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding='utf-8-sig'))
            if isinstance(raw, dict):
                state.update(raw)
    except Exception as exc:
        logger.warning('[dafeiyu_sleep] 读取睡眠状态失败: %s', exc)
    for key in ('pending_master', 'pending_other', 'group_buffer'):
        if not isinstance(state.get(key), dict):
            state[key] = {}
    if not isinstance(state.get('stats'), dict):
        state['stats'] = _empty_state()['stats']
    _STATE = state
    _LAST_TICK = time.time()
    _save(force=True)


def _save(force: bool = False) -> None:
    global _SAVED_AT
    now = time.time()
    if not force and now - _SAVED_AT < 5:
        return
    _SAVED_AT = now
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name('sleep_state.json.tmp')
        tmp.write_text(json.dumps(_STATE, ensure_ascii=False, indent=1), encoding='utf-8')
        tmp.replace(path)
        try:
            path.chmod(0o644)
        except Exception:
            pass
    except Exception as exc:
        logger.warning('[dafeiyu_sleep] 保存睡眠状态失败: %s', exc)


# --------------------------------------------------------------- 时段判断
def _hour_window(now_hour: float, start: int, end: int) -> bool:
    start = int(start) % 24
    end = int(end) % 24
    if start == end:
        return False
    if start < end:
        return start <= now_hour < end
    return now_hour >= start or now_hour < end


def _current_phase() -> str:
    if not _RUNTIME.get('enabled', True):
        return 'awake'
    if _STATE.get('sleeping'):
        return 'sleep'
    now = _dt.datetime.now()
    hour = now.hour + now.minute / 60.0
    if _hour_window(hour, int(_RUNTIME.get('quiet_hour', 1)), int(_RUNTIME.get('sleep_hour', 2))):
        return 'quiet'
    return 'awake'


def _next_wake_ts(now: _dt.datetime) -> float:
    hour = max(0, min(23, int(_RUNTIME.get('wake_hour', 6))))
    jitter = max(0, int(_RUNTIME.get('wake_jitter_minutes', 0)))
    minute = random.randint(0, jitter) if jitter > 0 else 0
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0) + _dt.timedelta(minutes=minute)
    if target <= now:
        target += _dt.timedelta(days=1)
    # 兜底：不管上面算出多久，都不许一次睡超过 max_sleep_hours。
    # 否则 6:00~6:45（wake_hour..wake_hour+jitter）之间误触晚安，
    # 算出来的 target 已经过点、要 +1 天，等于将近 24 小时不说话。
    try:
        max_hours = _as_float(_RUNTIME.get('max_sleep_hours'), 10.0)
        if max_hours <= 0:
            max_hours = 10.0
        cap = now + _dt.timedelta(hours=max_hours)
        if target > cap:
            logger.info('[dafeiyu_sleep] 这次要睡 %s 小时，按上限压到 %s 小时',
                        round((target - now).total_seconds() / 3600.0, 1), max_hours)
            target = cap
    except Exception as exc:
        logger.warning('[dafeiyu_sleep] 计算睡眠时长上限失败: %s', exc)
    return target.timestamp()


def _enter_sleep(reason: str, notify_master: bool = False) -> dict[str, Any]:
    if _STATE.get('sleeping'):
        return _STATE
    now = _dt.datetime.now()
    _STATE['sleeping'] = True
    _STATE['phase'] = 'sleep'
    _STATE['sleep_at'] = now.timestamp()
    _STATE['sleep_date'] = now.strftime('%Y-%m-%d')
    _STATE['reason'] = reason
    _STATE['wake_at'] = _next_wake_ts(now)
    _save(force=True)
    wake_text = _dt.datetime.fromtimestamp(float(_STATE['wake_at'])).strftime('%H:%M')
    logger.info('[dafeiyu_sleep] 睡着了（%s），预计 %s 醒', reason, wake_text)
    if notify_master:
        _spawn(_send_to_master(str(_RUNTIME.get('goodnight_reply') or '安')))
    return _STATE


def _mark_awake() -> None:
    now = _dt.datetime.now()
    _STATE['sleeping'] = False
    _STATE['phase'] = 'awake'
    _STATE['wake_date'] = now.strftime('%Y-%m-%d')
    _STATE['last_wake_at'] = now.timestamp()
    stats = _STATE.setdefault('stats', {})
    stats['wake_runs'] = int(stats.get('wake_runs') or 0) + 1
    stats['last_wake'] = now.timestamp()
    _save(force=True)


def _last_wake_at() -> float:
    """上一次真正清醒的时间戳；老状态文件里没有就退回 stats.last_wake。"""
    value = _STATE.get('last_wake_at')
    if not value:
        stats = _STATE.get('stats')
        if isinstance(stats, dict):
            value = stats.get('last_wake')
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _min_awake_ok() -> bool:
    """距离上次清醒不足 min_awake_minutes 时，不接「晚安/困了」的入睡流程。

    白天随口说一句「困了」不该让她直接睡到明天；没有清醒记录时按满足处理。
    """
    last = _last_wake_at()
    if last <= 0:
        return True
    try:
        minutes = _as_float(_RUNTIME.get('min_awake_minutes'), 30.0)
    except Exception:
        minutes = 30.0
    if minutes <= 0:
        return True
    return (time.time() - last) >= minutes * 60.0


def _spawn(coro: Any) -> None:
    try:
        asyncio.ensure_future(coro)
    except Exception as exc:
        logger.warning('[dafeiyu_sleep] 后台任务启动失败: %s', exc)


# --------------------------------------------------------------- 文本判断
def _text(event: AstrMessageEvent) -> str:
    try:
        return str(getattr(event, 'message_str', '') or '').strip()
    except Exception:
        return ''


_COMMAND_NAMES = (
    '睡眠', '备份', '上下文', '压缩', '报告', '费用', '兴趣', '主人',
    '群档案', '群知识', '命令', '菜单', '指令',
    'help', '触发',
)
# 注意：'起床' 不是注册命令（只在 /睡眠 起床 的参数里出现），
# 放在命令表里会让群里的「起床」被当成命令放行、在睡眠期直接回话，
# 所以这里按 wake_keywords 走，私聊才叫醒。


def _is_command(text: str) -> bool:
    if not text:
        return False
    if text.startswith('/') or text.startswith('／'):
        return True
    # AstrBot 在唤醒阶段会把唤醒前缀（默认 /）从 message_str 上剥掉，
    # 所以睡眠拦截里必须按命令名再认一次，否则 /睡眠 醒 会被当成普通消息排队。
    parts = text.split()
    head = parts[0] if parts else ''
    return head in _COMMAND_NAMES


def _matches(text: str, words: list[str]) -> bool:
    lowered = text.lower()
    for word in words:
        low = str(word or '').strip().lower()
        if low and low in lowered:
            return True
    return False


def _is_goodnight(text: str) -> bool:
    if not text or _is_command(text):
        return False
    if len(text) > 12:
        return False
    return _matches(text, list(_RUNTIME.get('goodnight_keywords') or []))


def _norm_short(text: str) -> str:
    drop = ' \t\r\n，。！？!?~～…、·.\u3002\u3001\u3010\u3011\u201c\u201d\u2018\u2019"\'‘’“”'
    return ''.join(ch for ch in str(text or '') if ch not in drop).strip()


def _is_confirm_text(text: str) -> bool:
    got = _norm_short(text).lower()
    if not got:
        return False
    words = [str(w).strip().lower() for w in (_RUNTIME.get('sleep_confirm_words') or ['安']) if str(w).strip()]
    return got in words


def _confirm_map() -> dict:
    data = _STATE.get('sleep_confirm')
    if not isinstance(data, dict):
        data = {}
        _STATE['sleep_confirm'] = data
    return data


def _set_confirm(umo: str) -> None:
    if not umo:
        return
    seconds = max(30.0, _as_float(_RUNTIME.get('sleep_confirm_seconds'), 600.0))
    _confirm_map()[umo] = time.time() + seconds
    _save(force=True)


def _clear_confirm(umo: str) -> None:
    data = _confirm_map()
    if umo and data.pop(umo, None) is not None:
        _save()


def _confirm_pending(umo: str) -> bool:
    if not umo:
        return False
    data = _confirm_map()
    deadline = data.get(umo)
    if not deadline:
        return False
    try:
        left = float(deadline) - time.time()
    except Exception:
        data.pop(umo, None)
        return False
    if left <= 0:
        data.pop(umo, None)
        _save()
        logger.info('[dafeiyu_sleep] 说晚安后的等待超时，不再等「安」了: %s', umo)
        return False
    return True


def _maybe_clear_confirm(event: AstrMessageEvent, umo: str) -> None:
    """主人说晚安后，只要下一条不是单回「安」，就当没这回事。"""
    if not _confirm_pending(umo):
        return
    try:
        if not (event.is_private_chat() and _is_master(event)):
            return
    except Exception:
        return
    if _is_confirm_text(_text(event)):
        return
    _clear_confirm(umo)
    logger.info('[dafeiyu_sleep] 主人没说「安」，先不睡，按正常聊天走: %s', umo)


def _is_wake_request(text: str) -> bool:
    if not text:
        return False
    if len(text) > 16:
        return False
    return _matches(text, list(_RUNTIME.get('wake_keywords') or []))


def _is_group_event(event: AstrMessageEvent, umo: str) -> bool:
    if GROUP_MARK in umo:
        return True
    try:
        return bool(event.get_group_id())
    except Exception:
        return False


def _is_at_bot(event: AstrMessageEvent) -> bool:
    try:
        self_id = str(event.get_self_id() or '')
    except Exception:
        self_id = ''
    try:
        messages = event.get_messages() or []
    except Exception:
        messages = []
    for comp in messages:
        if isinstance(comp, AtAll):
            return True
        if isinstance(comp, At):
            try:
                if str(comp.qq) == self_id:
                    return True
            except Exception:
                continue
    return _matches(_text(event), _wake_words())


def _sender_name(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_sender_name() or '')
    except Exception:
        return ''


def _sender_id(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_sender_id() or '')
    except Exception:
        return ''


def _item_of(event: AstrMessageEvent) -> dict[str, Any]:
    return {
        't': time.time(),
        'id': _sender_id(event),
        'name': _sender_name(event),
        'text': _text(event)[:200],
    }


def _push(bucket: dict[str, Any], umo: str, item: dict[str, Any], cap: int) -> None:
    items = bucket.get(umo)
    if not isinstance(items, list):
        items = []
        bucket[umo] = items
    items.append(item)
    if cap > 0 and len(items) > cap:
        del items[:len(items) - cap]


def _buffer_group(event: AstrMessageEvent, umo: str) -> None:
    buf = _STATE.setdefault('group_buffer', {})
    entry = buf.get(umo)
    if not isinstance(entry, dict):
        entry = {'name': str(_sender_name(event) or umo), 'items': []}
        buf[umo] = entry
    items = entry.get('items')
    if not isinstance(items, list):
        items = []
        entry['items'] = items
    item = _item_of(event)
    item['at'] = bool(_is_at_bot(event))
    items.append(item)
    cap = max(1, int(_RUNTIME.get('max_group_buffer', 40)))
    if len(items) > cap:
        del items[:len(items) - cap]
    if len(buf) > 30:
        for key in list(buf.keys())[:len(buf) - 30]:
            buf.pop(key, None)


def _stop(event: AstrMessageEvent) -> None:
    try:
        event.stop_event()
    except Exception:
        pass


# --------------------------------------------------------------- 拦截逻辑
def _maybe_block(event: AstrMessageEvent) -> bool:
    """安静期 / 睡眠期拦截消息；返回是否拦下了。"""
    if not _RUNTIME.get('enabled', True):
        return False
    umo = str(getattr(event, 'unified_msg_origin', '') or '')
    if not umo or not _bot_match(umo):
        return False
    try:
        if str(event.get_sender_id() or '') == str(event.get_self_id() or ''):
            return False
    except Exception:
        pass
    _maybe_clear_confirm(event, umo)
    phase = _current_phase()
    if phase == 'awake':
        return False
    text = _text(event)
    # 这条消息本身就是确认词（默认「安」）时不能清掉刚 arm 的确认，
    # 要留给后面的 SleepConfirmFilter 处理；否则「说晚安 → 她回安 → 你回安」
    # 会在拦截阶段把确认清掉，永远进不了睡眠。
    if not _is_confirm_text(text):
        _clear_confirm(umo)
    master = _is_master(event)
    group = _is_group_event(event, umo)
    try:
        private = bool(event.is_private_chat())
    except Exception:
        private = False
    if master:
        # 主人的命令、以及私聊里的「起床/醒醒」不拦，其它消息睡眠期先排队。
        # 「叫醒」只认私聊：群里说这类话按普通消息排队，和 /睡眠 状态 里写的说明一致。
        if _is_command(text) or (_is_wake_request(text) and private):
            return False
        if phase == 'quiet':
            return False
        _push(_STATE.setdefault('pending_master', {}), umo, _item_of(event),
              int(_RUNTIME.get('max_pending_per_umo', 20)))
        stats = _STATE.setdefault('stats', {})
        stats['sleep_skipped'] = int(stats.get('sleep_skipped') or 0) + 1
        event.set_extra(_EXTRA_GATE, 'sleep')
        _save()
        _stop(event)
        logger.info('[dafeiyu_sleep] 睡着了，主人的消息先排队，早上再回: %s', umo)
        return True
    if phase == 'quiet':
        # 安静期群里只记不回；但醒来时 _group_candidates 只取 t >= sleep_at，
        # 这期间记下的群消息本来就永远用不上，还占 group_buffer 的名额，所以不记。
        if not group:
            _push(_STATE.setdefault('pending_other', {}), umo, _item_of(event),
                  int(_RUNTIME.get('max_pending_per_umo', 20)))
        stats = _STATE.setdefault('stats', {})
        stats['quiet_skipped'] = int(stats.get('quiet_skipped') or 0) + 1
        event.set_extra(_EXTRA_GATE, 'quiet')
        _save()
        _stop(event)
        return True
    # 睡眠期：别人的私聊说晚安，回一个「安」
    if (not group) and bool(_RUNTIME.get('other_goodnight_reply', True)) and _is_goodnight(text):
        return False
    if group:
        _buffer_group(event, umo)
    else:
        _push(_STATE.setdefault('pending_other', {}), umo, _item_of(event),
              int(_RUNTIME.get('max_pending_per_umo', 20)))
    stats = _STATE.setdefault('stats', {})
    stats['sleep_skipped'] = int(stats.get('sleep_skipped') or 0) + 1
    event.set_extra(_EXTRA_GATE, 'sleep')
    _save()
    _stop(event)
    return True


def _should_goodnight(event: AstrMessageEvent) -> bool:
    if not _RUNTIME.get('enabled', True):
        return False
    if _current_phase() == 'sleep':
        return False
    umo = str(getattr(event, 'unified_msg_origin', '') or '')
    if not umo or not _bot_match(umo):
        return False
    if not _is_master(event):
        return False
    try:
        if not event.is_private_chat():
            return False
    except Exception:
        return False
    text = _text(event)
    if not text or _is_command(text) or _is_wake_request(text):
        return False
    # 刚醒没多久（默认 30 分钟）不接「困了/晚安」，避免白天随口一句就直接睡过去。
    if not _min_awake_ok():
        logger.info('[dafeiyu_sleep] 刚醒没多久，这句晚安先不接，按正常聊天走: %s', umo)
        return False
    return _is_goodnight(text)


def _should_other_goodnight(event: AstrMessageEvent) -> bool:
    if not _RUNTIME.get('enabled', True) or not bool(_RUNTIME.get('other_goodnight_reply', True)):
        return False
    if _current_phase() != 'sleep':
        return False
    umo = str(getattr(event, 'unified_msg_origin', '') or '')
    if not umo or not _bot_match(umo):
        return False
    try:
        if not event.is_private_chat():
            return False
    except Exception:
        return False
    if _is_master(event):
        return False
    return _is_goodnight(_text(event))


def _should_sleep_confirm(event: AstrMessageEvent) -> bool:
    """主人回了「安」：这才真的去睡（m13907）。"""
    if not _RUNTIME.get('enabled', True):
        return False
    if not _RUNTIME.get('sleep_confirm_enabled', True):
        return False
    if _current_phase() == 'sleep':
        return False
    umo = str(getattr(event, 'unified_msg_origin', '') or '')
    if not umo or not _bot_match(umo):
        return False
    if not _is_master(event):
        return False
    try:
        if not event.is_private_chat():
            return False
    except Exception:
        return False
    if not _confirm_pending(umo):
        return False
    return _is_confirm_text(_text(event))


def _should_wake_request(event: AstrMessageEvent) -> bool:
    if not _RUNTIME.get('enabled', True):
        return False
    if _current_phase() == 'awake':
        return False
    umo = str(getattr(event, 'unified_msg_origin', '') or '')
    if not umo or not _bot_match(umo):
        return False
    if not _is_master(event):
        return False
    # 只有主人私聊能叫醒：群里说「不想起床 / 别睡了你」不该在群里被硬叫醒并回话，
    # 这类消息在 _maybe_block 里按普通消息排队。
    try:
        if not event.is_private_chat():
            return False
    except Exception:
        return False
    text = _text(event)
    if not text:
        return False
    return _is_wake_request(text)


# --------------------------------------------------------------- 早安回话
def _fmt_items(items: list[dict[str, Any]]) -> str:
    lines = []
    for item in items:
        try:
            clock = _dt.datetime.fromtimestamp(float(item.get('t') or 0)).strftime('%H:%M')
        except Exception:
            clock = '--:--'
        name = str(item.get('name') or item.get('id') or '某人')
        lines.append(f'{clock} {name}：{str(item.get("text") or "")}')
    return NL.join(lines)


async def _compose_reply(umo: str, items: list[dict[str, Any]], kind: str) -> str:
    plugin = _PLUGIN
    if plugin is None or not items:
        return ''
    provider = None
    try:
        provider = await plugin.context.get_using_provider_async(umo)
    except Exception as exc:
        logger.warning('[dafeiyu_sleep] 取模型失败 %s: %s', umo, exc)
    if provider is None:
        return ''
    style = str(_RUNTIME.get('reply_style') or '')
    if kind == 'group':
        instruction = (
            '你刚睡醒，现在是早上。昨晚你睡着以后，这个群里有人说了下面这些话：'
        )
        tail = (
            '挑一到两条需要你接的，用你自己的口吻回一句（不超过 30 个字，纯文本，'
            '不要括号旁白，不要说你刚睡醒或睡觉的事）。如果没什么好回的，只输出 [SKIP]。'
            '只输出要发的那句话本身。'
        )
    else:
        who = '主人' if kind == 'master' else '别人'
        instruction = f'你刚睡醒，现在是早上。昨晚你睡着以后，{who}给你发了下面这些消息：'
        tail = (
            '用你自己的口吻挑最该回的一条回过去（不超过 40 个字，纯文本，不要括号旁白，'
            '不要解释自己在睡觉或刚醒）。只输出要发的那句话本身。'
        )
    prompt = NL.join([instruction, _fmt_items(items), '', tail, f'语气要求：{style}'])
    text = ''
    try:
        resp = await provider.text_chat(
            prompt=prompt,
            contexts=[],
            system_prompt=_persona_prompt(),
            func_tool=None,
            model=None,
        )
        text = str(getattr(resp, 'completion_text', '') or '').strip()
        if not text:
            text = str(getattr(getattr(resp, 'result', None), 'completion_text', '') or '').strip()
    except Exception as exc:
        logger.warning('[dafeiyu_sleep] 早上生成回复失败 %s: %s', umo, exc)
        return ''
    text = text.strip().strip('"').strip('“').strip('”').strip()
    if not text:
        return ''
    if text.upper().startswith('[SKIP]') or text.startswith('SKIP'):
        return ''
    first = text.split(NL)[0].strip()
    if not first:
        return ''
    return first[:120]


async def _send_to_umo(umo: str, text: str, master: bool = False) -> bool:
    plugin = _PLUGIN
    text = str(text or '').strip()
    if plugin is None or not text or not umo:
        return False
    try:
        sender = getattr(plugin, 'send_to_umo', None)
        if callable(sender):
            ok = await sender(umo, text)
        else:
            ok = await plugin.context.send_message(umo, MessageChain().message(text))
        if ok:
            logger.info('[dafeiyu_sleep] 早上补回了一条: %s', umo)
            return True
    except Exception as exc:
        logger.warning(
            '[dafeiyu_sleep] 早上发消息失败 %s: %s(%r)', umo, type(exc).__name__, exc
        )
    if master:
        try:
            sent = await plugin.send_private_to_master(text, None)
            if sent:
                return True
        except Exception as exc:
            logger.warning('[dafeiyu_sleep] 兜底私聊主人失败: %s', exc)
    return False


async def _send_to_master(text: str) -> None:
    plugin = _PLUGIN
    if plugin is None or not str(text or '').strip():
        return
    try:
        await plugin.send_private_to_master(str(text), None)
    except Exception as exc:
        logger.warning('[dafeiyu_sleep] 私聊主人失败: %s', exc)


def _interest_related(text: str, min_score: float) -> bool:
    store = _interest_store()
    if store is None or not text:
        return False
    try:
        return bool(store.related(text, min_score=min_score, limit=1))
    except Exception:
        return False


def _group_candidates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sleep_at = float(_STATE.get('sleep_at') or 0)
    min_score = float(_RUNTIME.get('catchup_interest_score') or 0.2)
    picked: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            if float(item.get('t') or 0) < sleep_at:
                continue
        except Exception:
            pass
        text = str(item.get('text') or '')
        if not text:
            continue
        if item.get('at') or _matches(text, _wake_words()):
            picked.append(item)
            continue
        if _interest_related(text, min_score):
            picked.append(item)
    limit = max(1, int(_RUNTIME.get('catchup_max_messages', 30)))
    return picked[-limit:]


async def _wake_routine(manual: bool = False) -> None:
    global _WAKING
    if _WAKING:
        return
    _WAKING = True
    try:
        now = _dt.datetime.now()
        wake_at = float(_STATE.get('wake_at') or 0)
        stale = (not manual) and bool(wake_at) and (now.timestamp() - wake_at) > 4 * 3600
        pending_master = {k: list(v) for k, v in (_STATE.get('pending_master') or {}).items() if v}
        pending_other = {k: list(v) for k, v in (_STATE.get('pending_other') or {}).items() if v}
        groups = {}
        for umo, entry in (_STATE.get('group_buffer') or {}).items():
            if isinstance(entry, dict) and entry.get('items'):
                groups[umo] = {'name': entry.get('name') or '', 'items': list(entry.get('items') or [])}
        master_count = sum(len(v) for v in pending_master.values())
        other_count = sum(len(v) for v in pending_other.values())
        group_count = sum(len(v['items']) for v in groups.values())
        _mark_awake()
        _STATE['pending_master'] = {}
        _STATE['pending_other'] = {}
        _STATE['group_buffer'] = {}
        _save(force=True)
        logger.info(
            '[dafeiyu_sleep] 醒了（%s）：主人 %d 条 / 别的私聊 %d 条 / 群 %d 个 %d 条',
            '手动叫醒' if manual else '自然醒', master_count, other_count, len(groups), group_count,
        )
        if stale:
            logger.info('[dafeiyu_sleep] 醒来时间过去太久了，这一轮不补回消息')
            return
        replied_private = 0
        for umo, items in pending_master.items():
            text = await _compose_reply(umo, items, 'master')
            if text and await _send_to_umo(umo, text, master=True):
                replied_private += 1
            await asyncio.sleep(1.0)
        if bool(_RUNTIME.get('wake_other_private', True)):
            for umo, items in pending_other.items():
                text = await _compose_reply(umo, items, 'other')
                if text and await _send_to_umo(umo, text):
                    replied_private += 1
                await asyncio.sleep(1.0)
        replied_groups = 0
        if bool(_RUNTIME.get('catchup_enabled', True)):
            limit = max(1, int(_RUNTIME.get('catchup_max_groups', 8)))
            for umo, entry in list(groups.items())[:limit]:
                picked = _group_candidates(entry.get('items') or [])
                if not picked:
                    logger.info('[dafeiyu_sleep] 这个群睡着以后没什么要回的: %s', umo)
                    continue
                text = await _compose_reply(umo, picked, 'group')
                if text and await _send_to_umo(umo, text):
                    replied_groups += 1
                await asyncio.sleep(2.0)
        stats = _STATE.setdefault('stats', {})
        stats['catchup_replies'] = int(stats.get('catchup_replies') or 0) + replied_groups
        _save(force=True)
        if bool(_RUNTIME.get('morning_report', True)):
            blocks = [f'🐋 早上好，我醒了（{now.strftime("%H:%M")}）']
            if master_count or other_count or group_count:
                blocks.append(
                    '【夜里攒下】\n'
                    f'  · 主人 {master_count} 条 · 别人私聊 {other_count} 条\n'
                    f'  · 群里 {group_count} 条（{len(groups)} 个群）'
                )
            else:
                blocks.append('【夜里攒下】\n  · 没攒下什么，安安静静的')
            if replied_private or replied_groups:
                blocks.append(
                    '【已经回了】\n'
                    f'  · 私聊 {replied_private} 条 · 群 {replied_groups} 个'
                )
            else:
                blocks.append('【已经回了】\n  · 没什么要回的，就安安静静起来了')
            await _send_to_master((NL + NL).join(blocks))
        logger.info('[dafeiyu_sleep] 早安流程结束：私聊 %d 条 / 群 %d 个', replied_private, replied_groups)
    except Exception as exc:
        logger.warning('[dafeiyu_sleep] 早安流程出错: %s', exc)
    finally:
        _WAKING = False


# --------------------------------------------------------------- 后台循环
def _tick() -> None:
    global _LAST_TICK
    if not _RUNTIME.get('enabled', True):
        _LAST_TICK = time.time()
        return
    now = _dt.datetime.now()
    now_ts = now.timestamp()
    prev = _LAST_TICK or now_ts
    _LAST_TICK = now_ts
    if _STATE.get('sleeping'):
        wake_at = float(_STATE.get('wake_at') or 0)
        if wake_at and now_ts >= wake_at:
            _spawn(_wake_routine(manual=False))
        return
    sleep_hour = max(0, min(23, int(_RUNTIME.get('sleep_hour', 2))))
    boundary = now.replace(hour=sleep_hour, minute=0, second=0, microsecond=0).timestamp()
    today = now.strftime('%Y-%m-%d')
    slept_today = _STATE.get('sleep_date') == today
    if slept_today or _STATE.get('wake_date') == today:
        return
    # 固定 900 秒窗口太窄：巡检间隔配到 >=900 秒、或者进程卡了十几分钟，
    # 这一次跨越就漏掉了，当天再也不会自动入睡。放宽到至少 1 小时
    # （间隔调得更大时按间隔的 2 倍），重启时 _LAST_TICK 会重置成当前时间，
    # 所以不会因为重启而误判成「刚跨过入睡点」。
    try:
        interval = _as_float(_RUNTIME.get('check_interval_seconds'), 30.0)
    except Exception:
        interval = 30.0
    window = max(3600.0, interval * 2.0)
    crossed = prev < boundary <= now_ts and (now_ts - boundary) <= window
    if crossed:
        _enter_sleep('auto', notify_master=bool(_RUNTIME.get('notify_on_auto_sleep', True)))


async def _loop() -> None:
    global _LAST_TICK
    try:
        await asyncio.sleep(10)
        _LAST_TICK = time.time()
        while True:
            try:
                _tick()
            except Exception as exc:
                logger.warning('[dafeiyu_sleep] 睡眠巡检异常: %s', exc)
            await asyncio.sleep(max(10, int(_RUNTIME.get('check_interval_seconds', 30))))
    except asyncio.CancelledError:
        raise


def start_background() -> None:
    global _TASK
    if not bool(_RUNTIME.get('enabled', True)):
        return
    if _TASK is not None and not _TASK.done():
        return
    try:
        _TASK = asyncio.get_running_loop().create_task(_loop())
        logger.info('[dafeiyu_sleep] 已启动睡眠模式巡检（安静 %s 点 / 睡 %s 点 / 醒 %s 点 +%s 分）',
                    _RUNTIME.get('quiet_hour'), _RUNTIME.get('sleep_hour'),
                    _RUNTIME.get('wake_hour'), _RUNTIME.get('wake_jitter_minutes'))
    except Exception as exc:
        logger.warning('[dafeiyu_sleep] 启动睡眠巡检失败: %s', exc)
    # 插件重载时如果正处在睡眠时段，直接补上睡眠状态（不发消息）
    try:
        now = _dt.datetime.now()
        hour = now.hour + now.minute / 60.0
        today = now.strftime('%Y-%m-%d')
        if (_hour_window(hour, int(_RUNTIME.get('sleep_hour', 2)), int(_RUNTIME.get('wake_hour', 6)))
                and not _STATE.get('sleeping')
                and _STATE.get('sleep_date') != today):
            _enter_sleep('startup', notify_master=False)
    except Exception as exc:
        logger.warning('[dafeiyu_sleep] 启动时进入睡眠失败: %s', exc)


def stop_background() -> None:
    global _TASK
    task = _TASK
    _TASK = None
    if task is not None and not task.done():
        try:
            task.cancel()
        except Exception:
            pass


# --------------------------------------------------------------- 状态给页面
def _status_text() -> str:
    status = sleep_status()
    lines = ['🐋 睡眠模式：' + ('开着' if status['enabled'] else '关着')]
    lines.append(
        f'安静期 {status["quiet_hour"]}:00 起（群里和别人私聊不回），'
        f'睡 {status["sleep_hour"]}:00 起（连主人消息也排队），'
        f'早上 {status["wake_hour"]}:00 后随机 {status["wake_jitter_minutes"]} 分钟内醒。'
    )
    now = _dt.datetime.now().strftime('%H:%M')
    lines.append(f'现在 {now}，状态：{status["phase_text"]}')
    if status['sleeping'] and status['wake_at']:
        lines.append(f'预计醒来：{status["wake_at"]}')
    lines.append(
        f'排队中：主人 {status["pending_master"]} 条 / 别的私聊 {status["pending_other"]} 条 / '
        f'群 {status["groups"]} 个共 {status["group_messages"]} 条'
    )
    lines.append('叫醒我：私聊发「起床」「醒醒」「别睡了」或 /睡眠 醒')
    return NL.join(lines)


def sleep_status() -> dict[str, Any]:
    phase = _current_phase()
    phase_text = {'awake': '醒着', 'quiet': '安静期（只记不回）', 'sleep': '睡着了'}.get(phase, phase)
    wake_at = ''
    try:
        if _STATE.get('wake_at'):
            wake_at = _dt.datetime.fromtimestamp(float(_STATE['wake_at'])).strftime('%Y-%m-%d %H:%M')
    except Exception:
        wake_at = ''
    pending_master = sum(len(v) for v in (_STATE.get('pending_master') or {}).values())
    pending_other = sum(len(v) for v in (_STATE.get('pending_other') or {}).values())
    groups = _STATE.get('group_buffer') or {}
    group_messages = 0
    for entry in groups.values():
        if isinstance(entry, dict) and isinstance(entry.get('items'), list):
            group_messages += len(entry['items'])
    return {
        'enabled': bool(_RUNTIME.get('enabled', True)),
        'phase': phase,
        'phase_text': phase_text,
        'sleeping': bool(_STATE.get('sleeping')),
        'quiet_hour': int(_RUNTIME.get('quiet_hour', 1)),
        'sleep_hour': int(_RUNTIME.get('sleep_hour', 2)),
        'wake_hour': int(_RUNTIME.get('wake_hour', 6)),
        'wake_jitter_minutes': int(_RUNTIME.get('wake_jitter_minutes', 0)),
        'sleep_at_text': _dt.datetime.fromtimestamp(float(_STATE['sleep_at'])).strftime('%Y-%m-%d %H:%M')
        if _STATE.get('sleep_at') else '',
        'wake_at': wake_at,
        'reason': str(_STATE.get('reason') or ''),
        'pending_master': pending_master,
        'pending_other': pending_other,
        'groups': len(groups),
        'group_messages': group_messages,
        'stats': dict(_STATE.get('stats') or {}),
    }


# --------------------------------------------------------------- Filters
class SleepGateFilter(CustomFilter):
    """安静期 / 睡眠期把消息拦下来（只记不回）。"""

    def filter(self, event: AstrMessageEvent, cfg: Any) -> bool:
        try:
            _maybe_block(event)
        except Exception as exc:
            logger.warning('[dafeiyu_sleep] 拦截判断出错: %s', exc)
        return False


class SleepGoodnightFilter(CustomFilter):
    """主人私聊说晚安：进入睡眠，只回一个「安」。"""

    def filter(self, event: AstrMessageEvent, cfg: Any) -> bool:
        try:
            return _should_goodnight(event)
        except Exception:
            return False


class SleepConfirmFilter(CustomFilter):
    """主人回了那个「安」：正式进入睡眠。"""

    def filter(self, event: AstrMessageEvent, cfg: Any) -> bool:
        try:
            return _should_sleep_confirm(event)
        except Exception:
            return False


class SleepOtherGoodnightFilter(CustomFilter):
    """睡着时别人私聊说晚安：也回一个「安」。"""

    def filter(self, event: AstrMessageEvent, cfg: Any) -> bool:
        try:
            return _should_other_goodnight(event)
        except Exception:
            return False


class SleepWakeFilter(CustomFilter):
    """睡眠期间主人用「起床/醒醒/强制唤醒」把她叫起来。"""

    def filter(self, event: AstrMessageEvent, cfg: Any) -> bool:
        try:
            return _should_wake_request(event)
        except Exception:
            return False


class SleepFeature:
    """睡眠模式处理器集合（由 main.Main 继承）。"""

    @filter.custom_filter(SleepGateFilter, False)
    async def sleep_gate(self, event: AstrMessageEvent):
        """这个 handler 只是挂 SleepGateFilter，正常不会被执行。"""
        if False:
            yield event.plain_result('')

    @filter.custom_filter(SleepGoodnightFilter, False)
    async def sleep_on_goodnight(self, event: AstrMessageEvent):
        reply = '安'
        try:
            reply = str(_RUNTIME.get('goodnight_reply') or '安')
            umo = str(getattr(event, 'unified_msg_origin', '') or '')
            if _RUNTIME.get('sleep_confirm_enabled', True):
                _set_confirm(umo)
                logger.info('[dafeiyu_sleep] 主人说晚安，先回「%s」；等主人下一条只发「安」才睡: %s',
                            reply, umo)
            else:
                _enter_sleep('goodnight')
                logger.info('[dafeiyu_sleep] 主人说晚安，去睡了: %s', umo)
        except Exception as exc:
            logger.warning('[dafeiyu_sleep] 处理晚安失败: %s', exc)
        _stop(event)
        yield event.plain_result(reply)

    @filter.custom_filter(SleepConfirmFilter, False)
    async def sleep_on_confirm(self, event: AstrMessageEvent):
        reply = '嗯'
        try:
            reply = str(_RUNTIME.get('sleep_confirm_reply') or '嗯')
            umo = str(getattr(event, 'unified_msg_origin', '') or '')
            _clear_confirm(umo)
            _enter_sleep('goodnight_confirm')
            logger.info('[dafeiyu_sleep] 主人回了「安」，这下真去睡了: %s', umo)
        except Exception as exc:
            logger.warning('[dafeiyu_sleep] 确认睡眠失败: %s', exc)
        _stop(event)
        yield event.plain_result(reply)

    @filter.custom_filter(SleepOtherGoodnightFilter, False)
    async def sleep_other_goodnight(self, event: AstrMessageEvent):
        _stop(event)
        yield event.plain_result(str(_RUNTIME.get('goodnight_reply') or '安'))

    @filter.custom_filter(SleepWakeFilter, False)
    async def sleep_wake_up(self, event: AstrMessageEvent):
        try:
            _spawn(_wake_routine(manual=True))
            logger.info('[dafeiyu_sleep] 主人把她叫醒了: %s', getattr(event, 'unified_msg_origin', ''))
            reply = '……醒了醒了，这就起来。'
        except Exception as exc:
            logger.warning('[dafeiyu_sleep] 叫醒失败: %s', exc)
            reply = '醒了。'
        _stop(event)
        yield event.plain_result(reply)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('睡眠')
    async def cmd_sleep(self, event: AstrMessageEvent):
        """看睡眠状态 / 手动睡 / 手动醒（仅主人）。"""
        try:
            raw = str(getattr(event, 'message_str', '') or '').strip()
            parts = raw.split()
            arg = parts[1].strip() if len(parts) > 1 else ''
        except Exception:
            arg = ''
        try:
            if arg in ('醒', '醒来', '唤醒', '起床', '起来'):
                _spawn(_wake_routine(manual=True))
                yield event.plain_result('醒了醒了，去看看有没有人找我。')
            elif arg in ('睡', '睡觉', '晚安', '去睡'):
                _enter_sleep('manual')
                yield event.plain_result(str(_RUNTIME.get('goodnight_reply') or '安'))
            else:
                yield event.plain_result(_status_text())
        except Exception as exc:
            logger.warning('[dafeiyu_sleep] /睡眠 出错: %s', exc)
            yield event.plain_result('睡眠状态查不出来，看看日志。')
