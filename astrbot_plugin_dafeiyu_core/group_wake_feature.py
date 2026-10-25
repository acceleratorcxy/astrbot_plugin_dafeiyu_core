"""大肥鱼群聊唤醒（astrbot_plugin_dafeiyu_core 的 group_wake 功能模块）。

AstrBot 在群聊里默认只会被 @、被引用、或以唤醒词（默认 "/"）开头时唤醒。
在 aiocqhttp（aiocqhttp）这类能看到群内全部消息的平台上，本插件补三条规则：

1. 关键词唤醒：群消息里提到「大肥鱼 / 肥鱼 / 鲸 / 鱼鱼」等关键词时，当作在叫她；
2. 随机搭话：不相关的群消息按一个很低的概率随机回一句，像真人在群里偶尔插句话。
   带同群冷却时间，避免刷屏、避免 token 乱烧；
3. 续聊（engaged）：她在群里回过一句后，会继续盯这个群一小段时间（默认 240 秒）。
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

try:  # 协议端内置命令静默词表（例如 aiocqhttp 的 #sl），加载失败不影响主流程
    from . import silent_guard
except Exception:
    silent_guard = None

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
    'engaged_min_score': 0.3,
    'engaged_window_seconds': 240,
    'engaged_idle_seconds': 120,
    'engaged_max_turns': 6,
    'engaged_cooldown_seconds': 12,
    'engaged_grace_seconds': 90,
    'engaged_same_sender_seconds': 20,
    'engaged_use_interest': True,
    'lively_boost_enabled': True,
    'lively_window_seconds': 120,
    'lively_min_messages': 6,
    'lively_max_boost': 3.0,
    'master_reply_enabled': True,
    'master_reply_probability': 100,
    'master_reply_cooldown_seconds': 6,
    # 社交分寸（治「自作多情」）：不是冲她来的定向发言不随机插话、她说太多就自动让一让、
    # 被群里嫌两句默认不管（m12630：不用设安静期）；开启 retort_silence_enabled 才回退到旧的安静逻辑。
    'random_skip_directed': True,
    'engaged_directed_min_score': 0.6,
    'self_share_enabled': True,
    'self_share_window_seconds': 300,
    'self_share_min_messages': 10,
    'self_share_max_ratio': 0.35,
    'retort_silence_enabled': False,
    'retort_silence_minutes': 10,
}

_RUNTIME: dict = dict(DEFAULTS)
_LAST_WAKE: dict[str, float] = {}
_LAST_RANDOM: dict[str, float] = {}
_LAST_ENGAGED: dict[str, float] = {}
_LAST_ENGAGED_SENDER: dict[str, tuple[str, float]] = {}
_LAST_MASTER: dict[str, float] = {}
_ENGAGED: dict[str, dict[str, Any]] = {}
_RECENT_USER: dict[str, list[dict[str, Any]]] = {}
_ACTIVITY: dict[str, list[float]] = {}
_MY_REPLIES: dict[str, list[float]] = {}   # umo -> 她最近在这个群发言的时间戳
_RETORT_UNTIL: dict[str, float] = {}       # 群 -> 被嫌弃后安静到什么时候
# 上面这些字典按群 key 只增不删：每 _STORE_CLEANUP_INTERVAL 秒顺手扫一遍，
# 扔掉超过 _STORE_TTL_SECONDS 没更新的群，单个字典超过 _STORE_MAX_KEYS 时再删最旧的。
_STORE_CLEANUP_INTERVAL = 600.0
_STORE_TTL_SECONDS = 7 * 86400.0
_STORE_MAX_KEYS = 500
_last_store_cleanup = 0.0
# 活跃度明细（每条消息一个时间戳）最多保留一小时，防止有人把统计窗口配得特别大
_ACTIVITY_KEEP_MAX = 3600.0
# 群友明说她插嘴 / 自作多情时用的话术（命中就闭嘴一阵子，别再顶上去招人烦）
_RETORT_WORDS = (
    '没跟你说话', '没在说你', '没说你', '别插嘴', '别乱插', '不用艾特', '不用@',
    '自作多情', '没跟你聊', '不是跟你说话', '没问你', '别回了', '不用回', '别搭话',
)
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
            'engaged_same_sender_seconds',
            'master_reply_probability',
            'master_reply_cooldown_seconds',
            'lively_window_seconds',
            'lively_min_messages',
            'lively_max_boost',
            'engaged_directed_min_score',
            'self_share_window_seconds',
            'self_share_min_messages',
            'self_share_max_ratio',
            'retort_silence_minutes',
        ):
            _RUNTIME[key] = _as_float(value, 0.0)
        else:
            _RUNTIME[key] = bool(value)
    if not _RUNTIME['bot_filter']:
        _RUNTIME['bot_filter'] = list(DEFAULTS['bot_filter'])
    if not _RUNTIME['keywords']:
        _RUNTIME['keywords'] = list(DEFAULTS['keywords'])
    if silent_guard is not None:
        try:
            silent_guard.apply(config)
        except Exception:
            pass


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


def _message_parts(event) -> list:
    try:
        return list(event.get_messages() or [])
    except Exception:
        return []


def _has_image(event) -> bool:
    """这条消息里有没有图片/表情（图片消息的 message_str 是空的）。"""
    for comp in _message_parts(event):
        if type(comp).__name__ == 'Image':
            return True
    return False


def _quotes_me(event) -> bool:
    """这条消息是不是「引用回复」——引用别人的消息时，多半就是在接话。"""
    for comp in _message_parts(event):
        if type(comp).__name__ == 'Reply':
            return True
    return False


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


def _store_stamp(value) -> float:
    """从状态字典里存的值估一个「最后更新时间」，用来决定能不能清掉这个群。"""
    try:
        if isinstance(value, dict):
            return max(
                _as_float(value.get('last_reply_at'), 0.0),
                _as_float(value.get('last_msg_at'), 0.0),
                _as_float(value.get('armed_at'), 0.0),
            )
        if isinstance(value, (tuple, list)):
            stamps: list[float] = []
            for item in value:
                if isinstance(item, dict):
                    stamps.append(_as_float(item.get('t'), 0.0))
                elif isinstance(item, (int, float)):
                    stamps.append(_as_float(item, 0.0))
            return max(stamps) if stamps else 0.0
        return _as_float(value, 0.0)
    except Exception:
        return 0.0


def _cleanup_stores(now: float | None = None) -> None:
    """给模块级状态字典做轻量清理：很久没动静的群扔掉，单个字典最多留 _STORE_MAX_KEYS 个。

    这些字典以前按群 key 只增不删，插件跑久了会慢慢涨内存；清理只动超过 7 天的 key，
    最近有动静的群一个都不碰，不影响续聊 / 冷却 / 社交分寸的判定。
    每 _STORE_CLEANUP_INTERVAL 秒最多扫一次，避免每条消息都全量遍历。
    """
    global _last_store_cleanup
    try:
        ts = time.time() if now is None else float(now)
        if ts - _last_store_cleanup < _STORE_CLEANUP_INTERVAL:
            return
        _last_store_cleanup = ts
        stores = (
            _LAST_WAKE, _LAST_RANDOM, _LAST_ENGAGED, _LAST_ENGAGED_SENDER,
            _LAST_MASTER, _ENGAGED, _RETORT_UNTIL, _ACTIVITY, _MY_REPLIES, _RECENT_USER,
        )
        for store in stores:
            try:
                if not store:
                    continue
                if store is _RETORT_UNTIL:
                    # 安静期存的是「到什么时候」，过点就删，别当时间戳算
                    for key, until in list(store.items()):
                        if _as_float(until, 0.0) <= ts:
                            store.pop(key, None)
                    continue
                for key, value in list(store.items()):
                    stamp = _store_stamp(value)
                    if stamp > 0 and ts - stamp > _STORE_TTL_SECONDS:
                        store.pop(key, None)
                if len(store) > _STORE_MAX_KEYS:
                    ordered = sorted(store.items(), key=lambda item: _store_stamp(item[1]))
                    for key, _value in ordered[: len(store) - _STORE_MAX_KEYS]:
                        store.pop(key, None)
            except Exception:
                continue
    except Exception:
        pass


def _activity_window() -> float:
    """_ACTIVITY 实际保留多久的窗口。

    「热闹加成」只看 lively_window_seconds，但算她在这个群说了多少（self_share）要用
    self_share_window_seconds。以前 _ACTIVITY 被截到 lively 窗口，分子按 self_share
    窗口筛、分母只有 120 秒的消息 → 占比被系统性放大（甚至会 >1），白白拦掉续聊/搭话。
    所以保留窗口取两者的较大值（上限 _ACTIVITY_KEEP_MAX，别无限长），算加成时再按
    lively 窗口数。
    """
    try:
        lively = max(10.0, _as_float(_RUNTIME.get('lively_window_seconds'), 120.0))
        share = max(30.0, _as_float(_RUNTIME.get('self_share_window_seconds'), 300.0))
        return min(_ACTIVITY_KEEP_MAX, max(lively, share))
    except Exception:
        return 120.0


def _self_share_window() -> float:
    """算她说话占比用的窗口：跟 _ACTIVITY 实际保留的窗口取交集。

    有人把 self_share_window_seconds 配得比保留窗口还大时，自动缩到保留窗口，
    保证分子（她的回复）分母（群消息）永远按同一段时间数，不会又把占比算虚高。
    """
    try:
        window = max(30.0, _as_float(_RUNTIME.get('self_share_window_seconds'), 300.0))
        return min(window, max(30.0, _activity_window()))
    except Exception:
        return 300.0


def _note_activity(event, umo: str) -> int:
    """把这条群消息记进活跃度窗口（同一事件只记一次），返回 lively 窗口内的条数。"""
    _cleanup_stores()
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
    lively = max(10.0, _as_float(_RUNTIME.get('lively_window_seconds'), 120.0))
    window = _activity_window()
    bucket = _ACTIVITY.setdefault(umo, [])
    bucket.append(now)
    cutoff = now - window
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    lively_cutoff = now - lively
    count = len([ts for ts in bucket if ts >= lively_cutoff])
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
        lively = max(10.0, _as_float(_RUNTIME.get('lively_window_seconds'), 120.0))
        # 保留更长的窗口（self_share 要用），但加成只数 lively 窗口内的消息
        cutoff = now - _activity_window()
        bucket = [ts for ts in (_ACTIVITY.get(umo) or []) if ts >= cutoff]
        _ACTIVITY[umo] = bucket
        lively_cutoff = now - lively
        return activity_boost(len([ts for ts in bucket if ts >= lively_cutoff]))
    except Exception:
        return 1.0


def _lively_snapshot(max_items: int = 5) -> dict[str, Any]:
    """给页面看：现在哪些群正热闹、加成多少。"""
    now = time.time()
    window = max(10.0, _as_float(_RUNTIME.get('lively_window_seconds'), 120.0))
    keep = _activity_window()
    items: list[dict[str, Any]] = []
    for umo, bucket in list(_ACTIVITY.items()):
        # 存储按更长窗口留（self_share 要用），但展示/加成只数 lively 窗口
        bucket = [ts for ts in bucket if now - ts <= keep]
        if bucket:
            _ACTIVITY[umo] = bucket
        else:
            _ACTIVITY.pop(umo, None)
            continue
        fresh = [ts for ts in bucket if now - ts <= window]
        boost = activity_boost(len(fresh))
        if boost <= 1.0:
            continue
        items.append({'umo': umo, 'count': len(fresh), 'boost': round(boost, 2)})
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


def _mentions_me(text: str) -> bool:
    """这句话里有没有叫她（名字/外号）。"""
    try:
        lowered = (text or '').lower()
        return any((k in text or k.lower() in lowered) for k in (_RUNTIME.get('keywords') or []))
    except Exception:
        return False


def _directed_elsewhere(event) -> bool:
    """这条消息是在 @ 别人 / 引用别人 —— 压根不是在跟她说话。

    实测（2026-10-01~10-25，合工大桀桀厕所 1991 条）：群里 @别人/引用别人的定向发言
    占 5%，她插进去的几乎都会挨骂（「没跟你说话」「别插嘴」「自作多情」）。
    """
    try:
        text = event.message_str or ''
    except Exception:
        text = ''
    if _mentions_me(text):
        # 一边 @ 别人一边提她名字的，还是算在说她
        return False
    try:
        parts = _message_parts(event)
    except Exception:
        return False
    for comp in parts:
        name = type(comp).__name__
        if name == 'At':
            qq = ''
            try:
                qq = str(getattr(comp, 'qq', '') or '')
            except Exception:
                qq = ''
            if qq and qq != 'all':
                return True
        elif name == 'Reply':
            return True
    return False


def _directed_to_other(event) -> bool:
    """这条消息是不是明确冲着群里别人去的（@ 了别人 / 引用了别人的话）。

    跟 _directed_elsewhere 的区别：这里不管话里有没有提她的名字。
    关键词分支需要这个口径——关键词本来就是她的名字，_directed_elsewhere 一看见名字
    就直接返回 False（那是给随机搭话分支用的），拿它当守卫等于没守。

    @ 她自己、@ 全体、引用她自己的话都不算「别人」；拿不到被引用者 ID 时也不算，
    宁可多回一句也别把直呼漏掉。
    """
    try:
        self_id = str(event.get_self_id() or '')
    except Exception:
        self_id = ''
    try:
        parts = _message_parts(event)
    except Exception:
        return False
    for comp in parts:
        name = type(comp).__name__
        if name == 'At':
            try:
                qq = str(getattr(comp, 'qq', '') or '')
            except Exception:
                qq = ''
            if qq and qq != 'all' and qq != self_id:
                return True
        elif name == 'Reply':
            try:
                sender = str(getattr(comp, 'sender_id', '') or '')
            except Exception:
                sender = ''
            if sender and self_id and sender != self_id:
                return True
    return False


def _self_share_ratio(umo: str) -> float:
    """这个群最近一段时间里，她的话占了多少（0~1）。

    分子分母都按 self_share_window_seconds 筛；_ACTIVITY 的保留窗口由 _activity_window()
    保证不小于这个值，两边窗口一致，占比才不会被放大。
    """
    try:
        window = _self_share_window()
        now = time.time()
        mine = [ts for ts in (_MY_REPLIES.get(umo) or []) if now - ts <= window]
        _MY_REPLIES[umo] = mine
        total = [ts for ts in (_ACTIVITY.get(umo) or []) if now - ts <= window]
        if not total:
            return 0.0
        return min(1.0, len(mine) / float(len(total)))
    except Exception:
        return 0.0


def _self_share_blocked(umo: str) -> bool:
    """她在这个群说得太多了就先让一让，别把群聊变成单人播报。"""
    if not _RUNTIME.get('self_share_enabled', True):
        return False
    try:
        window = _self_share_window()
        now = time.time()
        # 跟 _self_share_ratio 用同一个窗口数消息，别一个 300 秒一个 120 秒
        total = len([ts for ts in (_ACTIVITY.get(umo) or []) if now - ts <= window])
        min_messages = int(_as_float(_RUNTIME.get('self_share_min_messages'), 10))
        if total < min_messages:
            return False
        ratio = _self_share_ratio(umo)
        max_ratio = _as_float(_RUNTIME.get('self_share_max_ratio'), 0.35)
        if ratio >= max_ratio:
            logger.info(
                '[dafeiyu_group_wake] 她在这个群说得有点多（%.0f%%），先让一让: %s',
                ratio * 100.0, umo,
            )
            return True
        return False
    except Exception:
        return False


def _retort_silenced(key: str) -> bool:
    """刚被群友嫌弃过，还在安静期里。"""
    try:
        until = _RETORT_UNTIL.get(key)
        if not until:
            return False
        if time.time() >= until:
            _RETORT_UNTIL.pop(key, None)
            return False
        return True
    except Exception:
        return False


def _note_retort(text: str, key: str) -> bool:
    """群友明说她插嘴：这句不回；默认不设安静期（m12630）。"""
    try:
        if not any(word in (text or '') for word in _RETORT_WORDS):
            return False
        if _RUNTIME.get('retort_silence_enabled', False):
            minutes = _as_float(_RUNTIME.get('retort_silence_minutes'), 10.0)
            if minutes > 0:
                _RETORT_UNTIL[key] = time.time() + minutes * 60.0
                _ENGAGED.pop(key, None)
                logger.info(
                    '[dafeiyu_group_wake] 群友说「%s」，这个群先安静 %.0f 分钟: %s',
                    (text or '')[:20], minutes, key,
                )
                return True
        logger.debug(
            '[dafeiyu_group_wake] 群友说「%s」，这句不回（不设安静期）: %s',
            (text or '')[:20], key,
        )
        return True
    except Exception:
        return False


# 这些词太常见，撞字不算「在聊同一件事」（不然「今天吃什么」也会被判成接话）
_STOP_WORDS = (
    '今天', '明天', '昨天', '现在', '刚才', '时候', '什么', '怎么', '为什么', '咱们', '你们',
    '我们', '他们', '她们', '这个', '那个', '就是', '真的', '可以', '还有', '没有', '不是',
    '觉得', '知道', '一下', '这么', '那么', '因为', '所以', '但是', '已经', '感觉', '东西',
    '地方', '一下', '一个', '自己', '其实', '反正', '应该', '还是', '可能', '估计', '有点',
)


def _strip_stop_words(text: str) -> str:
    cleaned = text or ''
    for word in _STOP_WORDS:
        cleaned = cleaned.replace(word, ' ')
    return cleaned


def _bigrams(text: str) -> set[str]:
    cleaned = re.sub(r'[^0-9A-Za-z一-鿿]', '', _strip_stop_words(text))
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


def _engagement_score(event, text: str, recent_user: list[str], ref: str | None = None) -> tuple[float, str, float]:
    """这条新消息跟她刚才聊的相关度。

    返回 (分数 0~1, 理由, 硬相关 core)。
    core 只算「真的在说她 / 在接她的话 / 在聊她刚聊的事」：叫名字、引用她、跟她上一句或有
    实际内容重合、命中她的兴趣。问句 / 接茬 / 指代这些只是软加分，core 为 0 就当她没被说到，
    一句都不接（免得群里随便一句问话她都插嘴）。
    """
    keywords = _RUNTIME.get('keywords') or []
    lowered = (text or '').lower()
    if any((k in text or k.lower() in lowered) for k in keywords):
        return 1.0, '叫名字', 1.0
    core = 0.0
    soft = 0.0
    why: list[str] = []
    if ref is None:
        try:
            ref = str(event.get_extra('dafeiyu_group_wake_last_reply') or '')
        except Exception:
            ref = ''
    if _quotes_me(event):
        core += 0.4
        why.append('引用回复')
    if ref:
        ov = _overlap(text, ref)
        if ov > 0:
            core += min(0.5, ov)
            why.append('接话')
    # 跟群里刚才那几句（含别人叫她时说的话）的重合：权重比「接她的话」低一点，
    # 但比原来高，好让「换个说法接着聊同一个话题」也能续上。
    joined = ' '.join((recent_user or [])[-3:])
    if joined:
        ov = _overlap(text, joined)
        if ov > 0:
            core += min(0.45, ov)
            why.append('接着聊')
    if _RUNTIME.get('engaged_use_interest', True):
        interest = _interest_related(text)
        if interest > 0:
            core += min(0.5, interest)
            why.append('兴趣')
    if any(q in text for q in _QUESTION_WORDS):
        soft += 0.3
        why.append('问句')
    if len(text) <= 8 and any(k in text for k in _BACKCHANNEL_WORDS):
        soft += 0.25
        why.append('接茬')
    if any(k in text for k in _DEICTIC_WORDS):
        soft += 0.2
        why.append('指代')
    # 软加分只当一点点辅助，不能单靠自己把分数顶过门槛
    score = min(1.0, core + soft * 0.35)
    return score, '+'.join(why), core


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
        if silent_guard is not None and silent_guard.is_silent(event):
            return False
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
        if text.startswith('/'):
            return False
        image_only = False
        if not text:
            if not _has_image(event):
                return False
            text = '[图片]'
            image_only = True
        key = _group_key(event, umo)
        state = _ENGAGED.get(key)
        if not state:
            return False
        if _retort_silenced(key):
            return False
        if _note_retort(text, key):
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
        try:
            sid = str(event.get_sender_id() or '')
        except Exception:
            sid = ''
        same_gap = _as_float(_RUNTIME.get('engaged_same_sender_seconds'), 20.0)
        last_sender = _LAST_ENGAGED_SENDER.get(key)
        if same_gap > 0 and last_sender and last_sender[0] == sid and now - last_sender[1] < same_gap:
            # 同一个人刚说过就别连着接，免得她跟一个人来回刷屏
            return False
        recent_user = [str(item.get('x') or '') for item in (_RECENT_USER.get(umo) or [])[-3:]]
        score, why, core = _engagement_score(event, text, recent_user, str(state.get('last_reply_text') or ''))
        if image_only:
            score = min(1.0, score + 0.07)
            why = (why + '+发图') if why else '发图'
        if core <= 0:
            score = 0.0
            why = (why + '+没在说她') if why else '没在说她'
        min_score = _as_float(_RUNTIME.get('engaged_min_score'), 0.2)
        if _RUNTIME.get('random_skip_directed', True) and _directed_elsewhere(event):
            # 别人在 @ 别人 / 引用别人：除非明显在接她的话，否则不插嘴
            min_score = max(min_score, _as_float(_RUNTIME.get('engaged_directed_min_score'), 0.6))
        if _self_share_blocked(umo):
            return False
        if late > 0:
            # 已经出了正常窗口，要求跟刚才的话题更相关才接
            min_score = max(min_score, 0.45)
        if score < min_score:
            if image_only:
                # 纯图片消息没有文字，_overlap / 兴趣 / 引用全是 0，本来就比不出相关度；
                # 别把它当一次「无关 miss」（连续两次就会清掉续聊），群里有人发图本身就是接话。
                logger.debug('[dafeiyu_group_wake] 纯图片消息算不出相关度，这次不接也不记 miss: %s', key)
                return False
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
        _LAST_ENGAGED_SENDER[key] = (sid, now)
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
        if silent_guard is not None and silent_guard.is_silent(event):
            return False
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
            # 图片/表情消息的 message_str 是空的：主人发的图也要接（不然截图问他她永远不说话）。
            if _has_image(event) and _is_master_event(event) and _RUNTIME.get('master_reply_enabled', True):
                text = '[图片]'
            else:
                return False
        key = _group_key(event, umo)
        if _retort_silenced(key):
            logger.debug('[dafeiyu_group_wake] 还在被嫌弃的安静期里，不回: %s', key)
            return False
        if _note_retort(text, key):
            return False
        if text.startswith(('/', '／')):
            # 别的插件的斜杠命令（"/摸鱼 排行" 之类）里就算带了「鱼鱼 / 鲸」也不抢答
            return False
        _note_activity(event, umo)
        lowered = text.lower()
        keywords = _RUNTIME.get('keywords') or []
        hit_keyword = any((k in text or k.lower() in lowered) for k in keywords)
        _push_user_text(umo, text)
        if hit_keyword:
            # 叫名字也照样要有社交分寸：别人在 @ 别人 / 引用别人时不抢话。
            # 注意这里不能用 _directed_elsewhere：它一看见话里有她的名字就返回 False，
            # 而关键词命中必然带名字，等于永远不生效；改用不管名字的 _directed_to_other。
            if _RUNTIME.get('random_skip_directed', True) and _directed_to_other(event):
                logger.debug('[dafeiyu_group_wake] 话里带她的名字，但这条是在跟别人说话，不抢: %s', key)
                return False
            if _self_share_blocked(umo):
                return False
            cooldown = _as_float(_RUNTIME.get('cooldown_seconds'), 0.0)
            if not _cooldown_ok(_LAST_WAKE, _group_key(event, umo), cooldown):
                return False
            try:
                event.set_extra(_EXTRA_REASON, 'keyword')
            except Exception:
                pass
            return True
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
        if _RUNTIME.get('random_skip_directed', True) and _directed_elsewhere(event):
            # 在 @ 别人 / 引用别人：这不是在聊天，插进去就是自作多情
            logger.debug('[dafeiyu_group_wake] 这条是在跟别人说话，不插嘴: %s', _group_key(event, umo))
            return False
        if _self_share_blocked(umo):
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
        mine = _MY_REPLIES.setdefault(umo, [])
        mine.append(now)
        while mine and now - mine[0] > 3600:
            mine.pop(0)
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
        # 用 > 跟 _record_my_reply 的「turns > max_turns 才清」对齐：
        # 刚接满第 max_turns 句时状态还该留着显示，不能被状态接口提前删掉
        if now - last_reply > window or int(state.get('turns') or 0) > max_turns:
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
