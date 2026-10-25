"""大肥鱼群聊唤醒（astrbot_plugin_dafeiyu_core 的 group_wake 功能模块）。

AstrBot 在群聊里默认只会被 @、被引用、或以唤醒词（默认 "/"）开头时唤醒。
在 aiocqhttp（aiocqhttp）这类能看到群内全部消息的平台上，本插件补三条规则：

1. 关键词唤醒：群消息里提到「大肥鱼 / 肥鱼 / 鲸 / 鱼鱼」等关键词时，当作在叫她；
2. 随机搭话：不相关的群消息按一个很低的概率随机回一句，像真人在群里偶尔插句话。
   带同群冷却时间，避免刷屏、避免 token 乱烧；
3. 续聊（engaged）：她在群里回过一句后，会继续盯这个群一小段时间（默认 180 秒）。
   期间每来一条新消息，先算「这条跟她刚才聊的有没有关系」（叫名字 / 接着她的话 /
   兴趣相关 / 问句 / 接茬 / 指代），相关度够且掷中概率（默认 80% × 相关度）就再回一句；
   连着两条跟她无关、群里没人说话（默认 120 秒）或连续接话到上限（默认 6 句）就退出续聊，
   回到平时「随机搭话」的模式。

实现要点：
- 注册 CustomFilter 事件监听器（EventType.AdapterMessageEvent）。
- 唤醒阶段命中时 handler 被激活（事件 is_wake 被置 True），handler 里再把
  event.is_at_or_wake_command 置 True；这是让后续 LLM 正常回复的关键。
- 未命中时 filter 返回 False，事件不唤醒、不回复。
- 同一个事件只掷一次随机骰子（结果存在 event extra 里），不会因为 filter 被多次
  调用而反复抽。
- filter 内所有逻辑都吞异常返回 False：框架里 filter 抛异常会直接给用户发
  报错消息并终止事件，必须避免。
- on_llm_response 钩子记录「她刚回完的那句话」，续聊判断靠它判断相关性。
"""
from __future__ import annotations

import random
import re
import sys
import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.star import Context, Star, register

DEFAULTS = {
    'enabled': True,
    'bot_filter': [],
    'keywords': ['大肥鱼', '肥鱼', '鲸', '鱼鱼'],
    'cooldown_seconds': 0,
    'only_group': True,
    'random_reply_enabled': True,
    'random_reply_probability': 5,
    'random_reply_cooldown_seconds': 900,
    'random_reply_min_chars': 4,
    'engaged_enabled': True,
    'engaged_probability': 80,
    'engaged_min_score': 0.2,
    'engaged_window_seconds': 240,
    'engaged_idle_seconds': 120,
    'engaged_max_turns': 6,
    'engaged_cooldown_seconds': 10,
    'engaged_grace_seconds': 90,
    'engaged_use_interest': True,
    'lively_boost_enabled': True,
    'lively_window_seconds': 120,
    'lively_min_messages': 6,
    'lively_max_boost': 3.0,
    'master_reply_enabled': True,
    'master_reply_probability': 85,
    'master_reply_cooldown_seconds': 20,
}

_RUNTIME: dict = dict(DEFAULTS)
_LAST_WAKE: dict[str, float] = {}
_LAST_RANDOM: dict[str, float] = {}
_LAST_ENGAGED: dict[str, float] = {}
_LAST_MASTER: dict[str, float] = {}
_ENGAGED: dict[str, dict[str, Any]] = {}
_RECENT_USER: dict[str, list[dict[str, Any]]] = {}
_ACTIVITY: dict[str, list[float]] = {}
_EXTRA_ROLL = 'dafeiyu_group_wake_random_roll'
_EXTRA_REASON = 'dafeiyu_group_wake_reason'
_EXTRA_ENGAGED = 'dafeiyu_group_wake_engaged'
_EXTRA_ENGAGED_SCORE = 'dafeiyu_group_wake_engaged_score'
_EXTRA_ENGAGED_WHY = 'dafeiyu_group_wake_engaged_why'
_EXTRA_ACTIVITY = 'dafeiyu_group_wake_activity_count'
_QUESTION_WORDS = ('？', '?', '吗', '呢', '咋', '怎么', '为什么', '为啥', '啥', '多少', '谁', '哪', '是不是', '有没有')
_DEICTIC_WORDS = ('你', '鲸', '大肥鱼', '鱼鱼', '它', '这个', '那个', '刚才', '楼上', '上面', '前面')
_BACKCHANNEL_WORDS = ('哈', '草', '笑', '6', '对', '确实', '是吧', '好', '行', '可以', '真的', '牛')
_CJK_RE = re.compile('[一-鿿]+')
_ASCII_RE = re.compile(r'[a-z][a-z0-9_\-\.]{1,}')


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).replace('，', ',').split(',') if part.strip()]


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _apply_runtime(config) -> None:
    """把插件配置同步到模块级 _RUNTIME，供 CustomFilter 实例读取。"""
    for key, default in DEFAULTS.items():
        try:
            value = config.get(key) if config is not None else None
        except Exception:
            value = None
        if value is None:
            value = default
        if key in ('bot_filter', 'keywords'):
            _RUNTIME[key] = _as_list(value)
        elif key in (
            'cooldown_seconds',
            'random_reply_probability',
            'random_reply_cooldown_seconds',
            'random_reply_min_chars',
            'engaged_probability',
            'engaged_min_score',
            'engaged_window_seconds',
            'engaged_idle_seconds',
            'engaged_max_turns',
            'engaged_cooldown_seconds',
            'engaged_grace_seconds',
            'master_reply_probability',
            'master_reply_cooldown_seconds',
            'lively_window_seconds',
            'lively_min_messages',
            'lively_max_boost',
        ):
            _RUNTIME[key] = _as_float(value, 0.0)
        else:
            _RUNTIME[key] = bool(value)
    if not _RUNTIME['bot_filter']:
        _RUNTIME['bot_filter'] = list(DEFAULTS['bot_filter'])
    if not _RUNTIME['keywords']:
        _RUNTIME['keywords'] = list(DEFAULTS['keywords'])


def _bot_match(umo: str) -> bool:
    bot_filter = _RUNTIME.get('bot_filter') or []
    if not bot_filter:
        return True
    return any((token in umo for token in bot_filter))


def _group_key(event, umo: str) -> str:
    try:
        return str(event.get_group_id() or umo)
    except Exception:
        return umo


def _is_group(event, umo: str) -> bool:
    try:
        if str(event.get_group_id() or ''):
            return True
    except Exception:
        pass
    return ':GroupMessage:' in (umo or '')


def _cooldown_ok(store: dict, key: str, cooldown: float) -> bool:
    if cooldown <= 0:
        return True
    now = time.time()
    last = store.get(key)
    if last is not None and now - last < cooldown:
        return False
    store[key] = now
    return True


def _master_ids() -> list[str]:
    """主人 ID 直接从 owner_feature 的运行时配置读，配置热重载后自动跟着变。"""
    for name, mod in list(sys.modules.items()):
        if name == __name__ or not name.endswith('owner_feature'):
            continue
        runtime = getattr(mod, '_RUNTIME', None)
        if isinstance(runtime, dict):
            try:
                return [str(item).strip() for item in (runtime.get('master_ids') or []) if str(item).strip()]
            except Exception:
                return []
    return []


def _is_master_event(event) -> bool:
    """这条消息是不是主人发的：先看框架 role，再按 owner 段配置的 ID 兜底。"""
    try:
        if event.is_admin():
            return True
    except Exception:
        pass
    try:
        sid = str(event.get_sender_id() or '')
    except Exception:
        sid = ''
    return bool(sid) and sid in _master_ids()


def _random_roll(event, boost: float = 1.0) -> bool:
    """同一个事件只掷一次骰子，结果缓存在 event extra 里。

    boost 是「群里正热闹」的加成倍数（1.0 = 不加成），只在第一次掷骰子时生效。
    """
    try:
        cached = event.get_extra(_EXTRA_ROLL)
    except Exception:
        cached = None
    if cached is not None:
        return bool(cached)
    probability = _as_float(_RUNTIME.get('random_reply_probability'), 0.0) * max(1.0, boost)
    probability = min(100.0, probability)
    hit = probability > 0 and random.random() * 100.0 < probability
    try:
        event.set_extra(_EXTRA_ROLL, hit)
    except Exception:
        pass
    return hit


def _random_reply_should_wake(event, text: str, umo: str) -> bool:
    if not _RUNTIME.get('random_reply_enabled', True):
        return False
    min_chars = int(_as_float(_RUNTIME.get('random_reply_min_chars'), 4))
    if len(text) < min_chars:
        return False
    if text.startswith('/'):
        return False
    boost = _boost_for(event, umo)
    if boost > 1.0:
        logger.info(
            '[dafeiyu_group_wake] 群里正热闹，主动搭话概率 ×%.1f: %s',
            boost, _group_key(event, umo),
        )
    if not _random_roll(event, boost):
        return False
    cooldown = _as_float(_RUNTIME.get('random_reply_cooldown_seconds'), 900.0)
    if not _cooldown_ok(_LAST_RANDOM, _group_key(event, umo), cooldown):
        return False
    return True


def _push_user_text(umo: str, text: str) -> None:
    try:
        bucket = _RECENT_USER.setdefault(umo, [])
        bucket.append({'t': time.time(), 'x': text[:120]})
        if len(bucket) > 6:
            del bucket[: len(bucket) - 6]
    except Exception:
        pass


def _note_activity(event, umo: str) -> int:
    """把这条群消息记进活跃度窗口（同一事件只记一次），返回窗口内的条数。"""
    try:
        cached = event.get_extra(_EXTRA_ACTIVITY)
    except Exception:
        cached = None
    if cached is not None:
        try:
            return int(cached)
        except (TypeError, ValueError):
            return 0
    now = time.time()
    window = max(10.0, _as_float(_RUNTIME.get('lively_window_seconds'), 120.0))
    bucket = _ACTIVITY.setdefault(umo, [])
    bucket.append(now)
    cutoff = now - window
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    count = len(bucket)
    try:
        event.set_extra(_EXTRA_ACTIVITY, count)
    except Exception:
        pass
    return count


def activity_boost(count: int) -> float:
    """窗口内消息越多，主动回话的概率倍数越高（1.0 = 不加成）。"""
    if not _RUNTIME.get('lively_boost_enabled', True):
        return 1.0
    min_messages = max(1, int(_as_float(_RUNTIME.get('lively_min_messages'), 6)))
    max_boost = max(1.0, _as_float(_RUNTIME.get('lively_max_boost'), 3.0))
    if count <= min_messages:
        return 1.0
    boost = 1.0 + (count - min_messages) / float(min_messages)
    return min(max_boost, boost)


def _boost_for(event, umo: str) -> float:
    try:
        return activity_boost(_note_activity(event, umo))
    except Exception:
        return 1.0


def activity_boost_for(umo: str) -> float:
    """给别的模块（比如兴趣唤醒）读这个群现在有多热闹。"""
    try:
        now = time.time()
        window = max(10.0, _as_float(_RUNTIME.get('lively_window_seconds'), 120.0))
        bucket = _ACTIVITY.get(umo) or []
        cutoff = now - window
        bucket = [ts for ts in bucket if ts >= cutoff]
        _ACTIVITY[umo] = bucket
        return activity_boost(len(bucket))
    except Exception:
        return 1.0


def _lively_snapshot(max_items: int = 5) -> dict[str, Any]:
    """给页面看：现在哪些群正热闹、加成多少。"""
    now = time.time()
    window = max(10.0, _as_float(_RUNTIME.get('lively_window_seconds'), 120.0))
    items: list[dict[str, Any]] = []
    for umo, bucket in list(_ACTIVITY.items()):
        bucket = [ts for ts in bucket if now - ts <= window]
        if bucket:
            _ACTIVITY[umo] = bucket
        else:
            _ACTIVITY.pop(umo, None)
            continue
        boost = activity_boost(len(bucket))
        if boost <= 1.0:
            continue
        items.append({'umo': umo, 'count': len(bucket), 'boost': round(boost, 2)})
    items.sort(key=lambda item: -item['count'])
    top = '、'.join(
        '%s %d 条 ×%.1f' % (str(item['umo']).rsplit(':', 1)[-1], item['count'], item['boost'])
        for item in items[:3]
    )
    return {
        'enabled': bool(_RUNTIME.get('lively_boost_enabled', True)),
        'window_seconds': int(window),
        'min_messages': int(_as_float(_RUNTIME.get('lively_min_messages'), 6)),
        'max_boost': round(max(1.0, _as_float(_RUNTIME.get('lively_max_boost'), 3.0)), 1),
        'count': len(items),
        'top': top,
    }


def _bigrams(text: str) -> set[str]:
    cleaned = re.sub(r'[^0-9A-Za-z一-鿿]', '', text or '')
    if len(cleaned) < 2:
        return set()
    return {cleaned[index:index + 2] for index in range(len(cleaned) - 1)}


def _overlap(a: str, b: str) -> float:
    left = _bigrams(a)
    right = _bigrams(b)
    if not left or not right:
        return 0.0
    return min(1.0, len(left & right) / float(min(len(left), len(right))))


def _interest_related(text: str) -> float:
    try:
        for name, mod in sys.modules.items():
            if not name.endswith('interest_feature'):
                continue
            store = getattr(mod, '_STORE', None)
            if store is None:
                continue
            hits = store.related(text, min_score=0.2, limit=3)
            if hits:
                return float(hits[0][0])
    except Exception:
        pass
    return 0.0


def _engagement_score(event, text: str, recent_user: list[str]) -> tuple[float, str]:
    """这条新消息跟她刚才聊的相关度（0~1）和理由。"""
    keywords = _RUNTIME.get('keywords') or []
    lowered = (text or '').lower()
    if any((k in text or k.lower() in lowered) for k in keywords):
        return 1.0, '叫名字'
    score = 0.0
    why: list[str] = []
    ref = ''
    try:
        ref = str(event.get_extra('dafeiyu_group_wake_last_reply') or '')
    except Exception:
        ref = ''
    if ref:
        ov = _overlap(text, ref)
        if ov > 0:
            score += min(0.5, ov * 0.9)
            why.append('接话')
    # 跟群里刚才那几句（含别人叫她时说的话）的重合：权重比「接她的话」低一点，
    # 但比原来高，好让「换个说法接着聊同一个话题」也能续上。
    joined = ' '.join((recent_user or [])[-3:])
    if joined:
        ov = _overlap(text, joined)
        if ov > 0:
            score += min(0.45, ov * 0.9)
            why.append('接着聊')
    if _RUNTIME.get('engaged_use_interest', True):
        interest = _interest_related(text)
        if interest > 0:
            score += min(0.5, interest)
            why.append('兴趣')
    if any(q in text for q in _QUESTION_WORDS):
        score += 0.3
        why.append('问句')
    if len(text) <= 8 and any(k in text for k in _BACKCHANNEL_WORDS):
        score += 0.25
        why.append('接茬')
    if any(k in text for k in _DEICTIC_WORDS):
        score += 0.2
        why.append('指代')
    return min(1.0, score), '+'.join(why)


def _engaged_clear(key: str, reason: str) -> None:
    state = _ENGAGED.pop(key, None)
    if state:
        logger.info('[dafeiyu_group_wake] 退出续聊（%s）: %s', reason, key)


def _engaged_active(event, umo: str) -> bool:
    """这个群是不是还在续聊关注期里（在的话随机搭话先让位给续聊判断）。"""
    try:
        state = _ENGAGED.get(_group_key(event, umo))
        if not state:
            return False
        window = _as_float(_RUNTIME.get('engaged_window_seconds'), 180.0)
        return (time.time() - _as_float(state.get('last_reply_at'), 0.0)) <= window
    except Exception:
        return False


def _engaged_should_wake(event) -> bool:
    """续聊判定：她刚回过这个群，新消息跟她刚才聊的相关度足够且掷中概率时才接话。"""
    try:
        if not _RUNTIME.get('engaged_enabled', True):
            return False
        try:
            # 注意：不能用 event.is_wake 判断，别的插件只要有个 filter 通过框架就会
            # 把 is_wake 置 True（比如 meme_manager 的记录器），那样续聊永远进不来。
            if event.is_at_or_wake_command:
                return False
            if event.get_extra(_EXTRA_REASON) or event.get_extra('dafeiyu_interest_wake'):
                return False
        except Exception:
            pass
        umo = getattr(event, 'unified_msg_origin', '') or ''
        if not umo or not _bot_match(umo):
            return False
        if not _is_group(event, umo):
            return False
        text = (event.message_str or '').strip()
        if not text or text.startswith('/'):
            return False
        key = _group_key(event, umo)
        state = _ENGAGED.get(key)
        if not state:
            return False
        count = _note_activity(event, umo)
        now = time.time()
        window = _as_float(_RUNTIME.get('engaged_window_seconds'), 240.0)
        idle = _as_float(_RUNTIME.get('engaged_idle_seconds'), 120.0)
        grace = _as_float(_RUNTIME.get('engaged_grace_seconds'), 90.0)
        last_reply = _as_float(state.get('last_reply_at'), 0.0)
        last_msg = _as_float(state.get('last_msg_at'), last_reply)
        late = 0.0
        if now - last_reply > window:
            late = now - last_reply - window
            # 刚过期的一点时间里，如果这句跟刚才聊的很相关，还是允许再跟一句（治「慢半拍」）
            if grace <= 0 or late > grace:
                _engaged_clear(key, '超过关注时间')
                return False
        if late <= 0 and now - last_msg > idle:
            _engaged_clear(key, '没人说话了')
            return False
        turns = int(state.get('turns') or 0)
        max_turns = int(_as_float(_RUNTIME.get('engaged_max_turns'), 6))
        if turns >= max_turns:
            _engaged_clear(key, '接够了')
            return False
        cooldown = _as_float(_RUNTIME.get('engaged_cooldown_seconds'), 10.0)
        last_engaged = _LAST_ENGAGED.get(key)
        if cooldown > 0 and last_engaged is not None and now - last_engaged < cooldown:
            return False
        recent_user = [str(item.get('x') or '') for item in (_RECENT_USER.get(umo) or [])[-3:]]
        score, why = _engagement_score(event, text, recent_user)
        min_score = _as_float(_RUNTIME.get('engaged_min_score'), 0.2)
        if late > 0:
            # 已经出了正常窗口，要求跟刚才的话题更相关才接
            min_score = max(min_score, 0.45)
        if score < min_score:
            misses = int(state.get('misses') or 0) + 1
            state['misses'] = misses
            logger.debug('[dafeiyu_group_wake] 这条跟她无关（%.2f < %.2f，%s）: %s', score, min_score, text[:40], key)
            if misses >= 2:
                _engaged_clear(key, '话题跟她无关')
            return False
        state['misses'] = 0
        boost = activity_boost(count)
        probability = _as_float(_RUNTIME.get('engaged_probability'), 80.0) * min(1.0, max(0.5, score))
        probability = min(100.0, probability * max(1.0, boost))
        if probability <= 0 or random.random() * 100.0 >= probability:
            return False
        if late > 0:
            logger.info('[dafeiyu_group_wake] 续聊刚过期 %.0f 秒，这条还挺相关（%.2f），再跟一句: %s', late, score, key)
        _LAST_ENGAGED[key] = now
        state['last_msg_at'] = now
        state['recent_user'] = recent_user[-2:] + [text[:120]]
        try:
            event.set_extra(_EXTRA_REASON, 'engaged')
            event.set_extra(_EXTRA_ENGAGED, True)
            event.set_extra(_EXTRA_ENGAGED_SCORE, round(score, 2))
            event.set_extra(_EXTRA_ENGAGED_WHY, why)
        except Exception:
            pass
        return True
    except Exception as exc:
        logger.debug('[dafeiyu_group_wake] 续聊判定失败，按不唤醒处理: %s', exc)
        return False


def _should_wake(event) -> bool:
    """判断这条群消息要不要唤醒大肥鱼（关键词命中，或随机搭话命中）。"""
    try:
        if not _RUNTIME.get('enabled', True):
            return False
        umo = getattr(event, 'unified_msg_origin', '') or ''
        if not umo or not _bot_match(umo):
            return False
        if _RUNTIME.get('only_group', True) and not _is_group(event, umo):
            return False
        try:
            text = (event.message_str or '').strip()
        except Exception:
            text = ''
        if not text:
            return False
        _note_activity(event, umo)
        lowered = text.lower()
        keywords = _RUNTIME.get('keywords') or []
        hit_keyword = any((k in text or k.lower() in lowered) for k in keywords)
        _push_user_text(umo, text)
        if hit_keyword:
            cooldown = _as_float(_RUNTIME.get('cooldown_seconds'), 0.0)
            if not _cooldown_ok(_LAST_WAKE, _group_key(event, umo), cooldown):
                return False
            try:
                event.set_extra(_EXTRA_REASON, 'keyword')
            except Exception:
                pass
            return True
        if text.startswith(('/', '／')):
            return False
        if _is_master_event(event) and _RUNTIME.get('master_reply_enabled', True):
            key = _group_key(event, umo)
            now_ts = time.time()
            cooldown = _as_float(_RUNTIME.get('master_reply_cooldown_seconds'), 20.0)
            last_master = _LAST_MASTER.get(key)
            if cooldown > 0 and last_master is not None and now_ts - last_master < cooldown:
                return False
            boost = _boost_for(event, umo)
            probability = min(100.0, _as_float(_RUNTIME.get('master_reply_probability'), 85.0) * max(1.0, boost))
            if probability <= 0 or random.random() * 100.0 >= probability:
                logger.debug('[dafeiyu_group_wake] 主人说话但这次没掷中（%.0f%%）: %s', probability, key)
                return False
            _LAST_MASTER[key] = now_ts
            try:
                event.set_extra(_EXTRA_REASON, 'master')
            except Exception:
                pass
            logger.info('[dafeiyu_group_wake] 主人说话，接一句（概率 %.0f%%）: %s', probability, key)
            return True
        if _engaged_active(event, umo):
            # 刚回过这个群、还在盯后续：随机搭话先别抢，交给续聊判断决定要不要接。
            return False
        if _random_reply_should_wake(event, text, umo):
            try:
                event.set_extra(_EXTRA_REASON, 'random')
            except Exception:
                pass
            return True
        return False
    except Exception as exc:
        logger.debug(f'[dafeiyu_group_wake] 判定失败，按不唤醒处理: {exc}')
        return False


def _record_my_reply(event, reply_text: str = '') -> None:
    """她回完一句后：记下这句话，并开始（或继续）盯这个群。"""
    try:
        if not _RUNTIME.get('engaged_enabled', True):
            return
        umo = getattr(event, 'unified_msg_origin', '') or ''
        if not umo or not _bot_match(umo) or not _is_group(event, umo):
            return
        text = str(reply_text or '').strip()
        if not text:
            try:
                result = event.get_result()
                if result is not None:
                    text = result.get_plain_text() if hasattr(result, 'get_plain_text') else ''
            except Exception:
                text = ''
        if not text:
            return
        text = text.replace('\n', ' ').strip()[:200]
        key = _group_key(event, umo)
        now = time.time()
        window = _as_float(_RUNTIME.get('engaged_window_seconds'), 180.0)
        state = _ENGAGED.get(key) or {}
        fresh = bool(state) and (now - _as_float(state.get('last_reply_at'), 0.0)) <= window
        turns = (int(state.get('turns') or 0) + 1) if fresh else 1
        max_turns = int(_as_float(_RUNTIME.get('engaged_max_turns'), 6))
        if turns > max_turns:
            _ENGAGED.pop(key, None)
            logger.info('[dafeiyu_group_wake] 已经连着接了 %d 句，先停下等她主动叫: %s', turns - 1, key)
            return
        _ENGAGED[key] = {
            'armed_at': now,
            'last_reply_at': now,
            'last_msg_at': now,
            'last_reply_text': text,
            'turns': turns,
            'misses': 0,
            'recent_user': [str(item.get('x') or '') for item in (_RECENT_USER.get(umo) or [])[-3:]],
        }
        try:
            event.set_extra('dafeiyu_group_wake_last_reply', text)
        except Exception:
            pass
        logger.info(
            '[dafeiyu_group_wake] 她回完了，继续盯这个群 %d 秒（第 %d/%d 句）: %s',
            int(window), turns, max_turns, key,
        )
    except Exception as exc:
        logger.debug('[dafeiyu_group_wake] 记录回复失败: %s', exc)


def engaged_status() -> dict[str, Any]:
    """给页面看：现在正在续聊的群。"""
    now = time.time()
    window = _as_float(_RUNTIME.get('engaged_window_seconds'), 180.0)
    max_turns = int(_as_float(_RUNTIME.get('engaged_max_turns'), 6))
    active: dict[str, Any] = {}
    for key, state in list(_ENGAGED.items()):
        last_reply = _as_float(state.get('last_reply_at'), 0.0)
        if now - last_reply > window or int(state.get('turns') or 0) >= max_turns:
            _ENGAGED.pop(key, None)
            continue
        active[key] = {
            'turns': int(state.get('turns') or 0),
            'left_seconds': int(max(0, window - (now - last_reply))),
        }
    return {
        'enabled': bool(_RUNTIME.get('engaged_enabled', True)),
        'lively': _lively_snapshot(),
        'count': len(active),
        'groups': active,
        'max_turns': max_turns,
        'window_seconds': int(window),
        'probability': _as_float(_RUNTIME.get('engaged_probability'), 80.0),
    }


class GroupRelatedWakeFilter(CustomFilter):
    """群聊中提到大肥鱼关键词、或随机命中时唤醒事件。"""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        return _should_wake(event)


class EngagedContinueFilter(CustomFilter):
    """她刚回完这个群时，相关的新消息按概率继续接话。"""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        return _engaged_should_wake(event)


class GroupWakeFeature:

    @filter.custom_filter(GroupRelatedWakeFilter, False)
    async def wake_on_related(self, event: AstrMessageEvent):
        """被 CustomFilter 激活后，把事件升级为「真正唤醒」，让 LLM 正常回复。"""
        try:
            if not event.is_at_or_wake_command:
                event.is_at_or_wake_command = True
            event.is_wake = True
            try:
                reason = event.get_extra(_EXTRA_REASON) or 'keyword'
            except Exception:
                reason = 'keyword'
            event.set_extra('dafeiyu_group_wake', reason)
            logger.info(f'[dafeiyu_group_wake] 群聊唤醒（{reason}）: {event.unified_msg_origin} | {(event.message_str or "")[:60]}')
        except Exception as exc:
            logger.warning(f'[dafeiyu_group_wake] 唤醒事件时出错: {exc}')

    @filter.custom_filter(EngagedContinueFilter, False)
    async def continue_engaged(self, event: AstrMessageEvent):
        """续聊判定通过：不用叫名字也接一句。"""
        try:
            if not event.is_at_or_wake_command:
                event.is_at_or_wake_command = True
            event.is_wake = True
            event.set_extra('dafeiyu_group_wake', 'engaged')
            try:
                score = event.get_extra(_EXTRA_ENGAGED_SCORE)
                why = event.get_extra(_EXTRA_ENGAGED_WHY)
            except Exception:
                score, why = None, None
            logger.info(
                '[dafeiyu_group_wake] 续聊接话（相关度 %s，%s）: %s | %s',
                score, why or '相关', getattr(event, 'unified_msg_origin', ''), (event.message_str or '')[:60],
            )
        except Exception as exc:
            logger.warning(f'[dafeiyu_group_wake] 续聊唤醒时出错: {exc}')

    @filter.on_llm_response(priority=1000)
    async def remember_my_reply(self, event: AstrMessageEvent, response):
        """她每回一句，记下来并进入/续上「盯群」状态。"""
        try:
            text = ''
            if response is not None:
                text = str(getattr(response, 'completion_text', '') or '')
            if not text:
                try:
                    text = str(getattr(response, 'result', '') or '')
                except Exception:
                    text = ''
            _record_my_reply(event, text)
        except Exception as exc:
            logger.debug(f'[dafeiyu_group_wake] on_llm_response 记录失败: {exc}')
