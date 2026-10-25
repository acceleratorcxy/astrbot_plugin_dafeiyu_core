"""大肥鱼兴趣机制（astrbot_plugin_dafeiyu_interest）

用户需求：给大肥鱼建立兴趣机制 —— 有倾向性地回复感兴趣的话题，并且感兴趣的东西
可以自己搜索、记录下来。

实现（全部走 AstrBot 插件钩子/工具，不改框架源码）：

1. 兴趣库：JSON 存在 data/plugin_data/astrbot_plugin_dafeiyu_interest/interests.json，
   每条兴趣有 name / aliases / affinity（喜欢程度 0~1）/ notes（搜索记录）/ 计数。
2. 倾向性唤醒（群聊）：群消息里出现兴趣词时，按 affinity 加权概率把事件唤醒成
   「真正在跟她说话」，让她主动接话（CustomFilter + is_at_or_wake_command，兼容
   aiocqhttp 这类能看到全部群消息的平台）。
3. 倾向性提示（每轮）：on_llm_request 时把「最近惦记的兴趣 + 相关见闻」作为
   临时 TextPart 注入（不写历史、不破坏提示词缓存），让她接话更有偏好。
4. 自主搜索 + 记录：兴趣被聊到且达到门槛时，后台调百度 AI 搜索，把结果摘要记进
   该兴趣的 notes（有冷却和每日上限，避免乱烧配额）。
5. LLM 工具：interest_list / interest_add / interest_remove / interest_search /
   interest_remember / interest_recall —— 只有主人（role=admin）能调用写操作。
6. 命令：/兴趣 （列表）、/兴趣 加 名字 别名、/兴趣 删 名字、/兴趣 搜 名字、
   /兴趣 记 名字 内容、/兴趣 看 名字 —— 写操作仅主人可用。
"""

# """统一插件 astrbot_plugin_dafeiyu_core 的 interest 功能模块。
#
# 由旧插件 astrbot_plugin_dafeiyu_interest/main.py 机械转换而来：Main 类改名为
# InterestFeature（普通类，去掉 Star 基类与 __init__/initialize/terminate），
# 方法上的 filter/llm_tool 装饰器保留，由统一插件的 Main 多继承并复用。"""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from astrbot.api import AstrBotConfig, logger, llm_tool
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart

try:  # 协议端内置命令静默词表（例如 aiocqhttp 的 #sl），加载失败不影响主流程
    from . import silent_guard
except Exception:
    silent_guard = None
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
PLUGIN_ID = 'astrbot_plugin_dafeiyu_interest'
DEFAULTS = {'enabled': True, 'bot_filter': [], 'wake_enabled': True, 'wake_probability': 55, 'wake_threshold': 0.45, 'wake_cooldown_seconds': 180, 'wake_related_enabled': True, 'wake_related_probability': 35, 'wake_related_min_score': 0.35, 'master_wake_boost': 1.6, 'master_wake_threshold_scale': 0.7, 'only_group': True, 'hints_enabled': True, 'hints_max_items': 3, 'hints_max_notes': 2, 'auto_search_enabled': True, 'auto_search_min_affinity': 0.7, 'auto_search_cooldown_seconds': 1800, 'auto_search_max_per_day': 6, 'auto_search_top_k': 3, 'max_notes_per_interest': 8, 'candidate_ttl_days': 3, 'promote_mentions': 3, 'interest_ttl_days': 7, 'prune_enabled': True, 'prune_interval_seconds': 3600, 'affinity_decay_per_day': 0.02}
SEED_INTERESTS = [{'name': '鲸鱼', 'aliases': ['鲸', '小鲸鱼', 'whale'], 'affinity': 0.82}, {'name': '白饭', 'aliases': ['米饭', '干饭', '大米饭'], 'affinity': 0.66}, {'name': '摸鱼', 'aliases': ['偷懒', '划水', '摆烂'], 'affinity': 0.6}, {'name': '表情包', 'aliases': ['梗图', 'meme'], 'affinity': 0.58}]
BAIDU_SEARCH_URL = 'https://qianfan.baidubce.com/v2/ai_search/web_search'
TZ = timezone(timedelta(hours=8))
_RUNTIME: dict = dict(DEFAULTS)
_STORE: 'InterestStore | None' = None
_CTX: Context | None = None
_LAST_WAKE: dict[str, float] = {}
_TASKS: set = set()
_EXTRA_WAKE = 'dafeiyu_interest_wake'
_EXTRA_RELATED = 'dafeiyu_interest_related'
_EXTRA_MATCHED = 'dafeiyu_interest_matched'
_EXTRA_MENTION_DONE = 'dafeiyu_interest_mention_done'
_EXTRA_SEARCH_DONE = 'dafeiyu_interest_search_scheduled'
_EXTRA_OBSERVED = 'dafeiyu_interest_observed'

def _now() -> float:
    return time.time()

def _today() -> str:
    return datetime.now(TZ).strftime('%Y-%m-%d')

def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).replace('，', ',').split(',') if part.strip()]

def _as_float(value, default: float=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _as_int(value, default: int=0) -> int:
    try:
        return int(float(value))
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
        elif key in ('wake_probability', 'wake_related_probability', 'master_wake_boost'):
            _RUNTIME[key] = _as_float(value, float(DEFAULTS[key]))
        elif key in ('wake_threshold', 'wake_related_min_score', 'master_wake_threshold_scale'):
            _RUNTIME[key] = max(0.0, min(1.0, _as_float(value, float(DEFAULTS[key]))))
        elif key in ('wake_cooldown_seconds', 'auto_search_min_affinity', 'auto_search_cooldown_seconds'):
            _RUNTIME[key] = _as_float(value, float(DEFAULTS[key]))
        elif key in ('hints_max_items', 'hints_max_notes', 'auto_search_max_per_day', 'auto_search_top_k', 'max_notes_per_interest', 'candidate_ttl_days', 'promote_mentions', 'interest_ttl_days', 'prune_interval_seconds'):
            _RUNTIME[key] = _as_int(value, int(DEFAULTS[key]))
        elif key == 'affinity_decay_per_day':
            _RUNTIME[key] = max(0.0, _as_float(value, float(DEFAULTS[key])))
        else:
            _RUNTIME[key] = bool(value)
    if not _RUNTIME['bot_filter']:
        _RUNTIME['bot_filter'] = list(DEFAULTS['bot_filter'])

def _bot_match(umo: str) -> bool:
    bot_filter = _RUNTIME.get('bot_filter') or []
    if not bot_filter:
        return True
    return any((token in umo for token in bot_filter))

def _note_id(note: dict[str, Any]) -> str:
    """给一条兴趣记录生成稳定 id（同一条记录重复读取不变）。"""
    seed = '|'.join([
        str(note.get('at') or ''),
        str(note.get('text') or ''),
        str(note.get('url') or ''),
    ])
    return 'n' + hashlib.md5(seed.encode('utf-8')).hexdigest()[:10]

def _slug_id(name: str) -> str:
    """给兴趣生成稳定、唯一的 id（中文名走 md5 兜底，避免同毫秒重复）。"""
    slug = re.sub('[^0-9a-zA-Z]+', '_', name or '').strip('_').lower()
    if slug:
        return slug
    return 'i' + hashlib.md5((name or '').encode('utf-8')).hexdigest()[:10]

def _candidate_id(name: str) -> str:
    return 'c' + hashlib.md5((name or '').strip().lower().encode('utf-8')).hexdigest()[:10]

def _protected_names() -> set[str]:
    """人设里写死的兴趣（种子）永远不算临时话题，不许自动清掉。"""
    names = {str(seed.get('name') or '').strip() for seed in SEED_INTERESTS}
    names.discard('')
    return names

_STOP_BIGRAMS = {
    '我们', '你们', '他们', '她们', '它们', '这个', '那个', '这些', '那些', '这样', '那样',
    '什么', '怎么', '怎样', '为什', '可以', '就是', '还是', '已经', '而且', '但是', '因为',
    '所以', '一个', '没有', '现在', '今天', '今晚', '明天', '昨天', '时候', '自己', '觉得',
    '知道', '真的', '不是', '一下', '一起', '有点', '比较', '如果', '然后', '还有', '看到',
    '说的', '感觉', '应该', '可能', '问题', '东西', '事情', '地方', '时间', '我的', '你的',
    '他的', '她的', '是在', '是不', '不了', '也是', '都是', '有没', '么样', '多少', '几个',
}
_ASCII_TOKEN_RE = re.compile(r'[a-z][a-z0-9_\-\.]{1,}')
_CJK_RUN_RE = re.compile('[一-鿿]{2,}')
_NOTE_TOKEN_CACHE: dict[str, tuple] = {}

def _text_tokens(text: str) -> dict[str, set[str]]:
    """把文本拆成「英文词」和「中文双字组」两类 token，用于粗粒度相关度匹配。"""
    text = text or ''
    lowered = text.lower()
    ascii_tokens = {m for m in _ASCII_TOKEN_RE.findall(lowered) if len(m) >= 2}
    cjk_tokens: set[str] = set()
    for run in _CJK_RUN_RE.findall(text):
        for index in range(len(run) - 1):
            bigram = run[index:index + 2]
            if bigram not in _STOP_BIGRAMS:
                cjk_tokens.add(bigram)
    return {'ascii': ascii_tokens, 'cjk': cjk_tokens}

def _note_tokens(item: dict[str, Any]) -> dict[str, set[str]]:
    """兴趣记录（notes）里出现过的 token，用来判断「这条消息是不是在聊她惦记的事」。"""
    notes = item.get('notes') or []
    if not notes:
        return {'ascii': set(), 'cjk': set()}
    signature = (len(notes), '|'.join(str(n.get('at') or '') + str(n.get('title') or '') + str(n.get('text') or '')[:60] for n in notes[-8:]))
    key = str(item.get('id') or item.get('name') or '')
    cached = _NOTE_TOKEN_CACHE.get(key)
    if cached and cached[0] == signature:
        return cached[1]
    merged = ' '.join(str(n.get('title') or '') + ' ' + str(n.get('text') or '') for n in notes)
    tokens = _text_tokens(merged)
    _NOTE_TOKEN_CACHE[key] = (signature, tokens)
    return tokens

def _related_score(item: dict[str, Any], text_tokens: dict[str, set[str]]) -> float:
    """兴趣记录与消息的相关度（0~1）：英文词命中权重高，中文双字组要两个以上才作数。"""
    note_tokens = _note_tokens(item)
    ascii_hit = note_tokens['ascii'] & text_tokens['ascii']
    cjk_hit = note_tokens['cjk'] & text_tokens['cjk']
    score = 0.0
    if ascii_hit:
        score += min(0.6, 0.35 * len(ascii_hit))
    if len(cjk_hit) >= 2:
        score += min(0.5, 0.12 * len(cjk_hit))
    if not ascii_hit and len(cjk_hit) < 3:
        score = min(score, 0.25)
    return min(1.0, score)

class InterestStore:
    """兴趣库（单文件 JSON，原子写入）。"""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = self._empty()
        self._notes_need_save = False
        loaded = self._load()
        seeded = not loaded['interests']
        if seeded:
            for seed in SEED_INTERESTS:
                loaded['interests'].append(self._new_item(seed['name'], seed.get('aliases'), seed['affinity'], pinned=True, kind='persona'))
        self.data = loaded
        if seeded or self._notes_need_save:
            self.save()

    def _empty(self) -> dict[str, Any]:
        return {'version': 1, 'updated_at': 0, 'interests': [], 'candidates': [], 'stats': {'searches': 0, 'wakes': 0, 'daily': {}}}

    def _load(self) -> dict[str, Any]:
        data = self._empty()
        try:
            raw = json.loads(self.path.read_text('utf-8'))
            if isinstance(raw, dict):
                if isinstance(raw.get('interests'), list):
                    data['interests'] = [item for item in raw['interests'] if isinstance(item, dict) and item.get('name')]
                if isinstance(raw.get('stats'), dict):
                    data['stats'].update(raw['stats'])
                if isinstance(raw.get('candidates'), list):
                    data['candidates'] = [c for c in raw['candidates'] if isinstance(c, dict)]
                data['updated_at'] = raw.get('updated_at') or 0
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning('[dafeiyu_interest] 读取兴趣库失败，使用空库: %s', exc)
        seen_ids: set[str] = set()
        for item in data['interests']:
            item['name'] = str(item.get('name') or '').strip()
            item['aliases'] = _as_list(item.get('aliases'))
            item['affinity'] = max(0.0, min(1.0, _as_float(item.get('affinity'), 0.6)))
            item['notes'] = [n for n in item.get('notes') or [] if isinstance(n, dict)]
            for note in item['notes']:
                if not str(note.get('id') or '').strip():
                    note['id'] = _note_id(note)
                    self._notes_need_save = True
            item['mention_count'] = _as_int(item.get('mention_count'), 0)
            item['last_mention'] = _as_float(item.get('last_mention'), 0.0)
            item['last_search'] = _as_float(item.get('last_search'), 0.0)
            item['created_at'] = _as_float(item.get('created_at'), _now())
            item['pinned'] = bool(item.get('pinned')) or item['name'] in _protected_names()
            item['kind'] = str(item.get('kind') or ('persona' if item['pinned'] else 'auto'))
            item['last_seen'] = max(_as_float(item.get('last_seen'), 0.0), item['last_mention'], item['created_at'])
            base = str(item.get('id') or '').strip() or _slug_id(item['name'])
            item_id, n = (base, 2)
            while item_id in seen_ids:
                item_id = f'{base}_{n}'
                n += 1
            seen_ids.add(item_id)
            item['id'] = item_id
        candidates = []
        seen_c: set[str] = set()
        for cand in data.get('candidates') or []:
            name = str(cand.get('name') or '').strip()
            if not name:
                continue
            cid = str(cand.get('id') or '').strip() or _candidate_id(name)
            if cid in seen_c:
                continue
            seen_c.add(cid)
            candidates.append({
                'id': cid,
                'name': name[:40],
                'aliases': _as_list(cand.get('aliases')),
                'mentions': max(1, _as_int(cand.get('mentions'), 1)),
                'first_seen': _as_float(cand.get('first_seen'), _now()),
                'last_seen': _as_float(cand.get('last_seen'), _now()),
                'notes': [n for n in cand.get('notes') or [] if isinstance(n, dict)][-4:],
                'source': str(cand.get('source') or 'chat'),
            })
        data['candidates'] = candidates
        return data

    @staticmethod
    def _new_item(name: str, aliases, affinity: float, pinned: bool=False, kind: str='auto') -> dict[str, Any]:
        return {'id': _slug_id(name), 'name': name, 'aliases': _as_list(aliases), 'affinity': max(0.0, min(1.0, _as_float(affinity, 0.6))), 'notes': [], 'mention_count': 0, 'last_mention': 0.0, 'last_search': 0.0, 'created_at': _now(), 'last_seen': _now(), 'pinned': bool(pinned), 'kind': str(kind or 'auto')}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.data['updated_at'] = _now()
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), 'utf-8')
            os.replace(tmp, self.path)
        except Exception as exc:
            logger.warning('[dafeiyu_interest] 保存兴趣库失败: %s', exc)

    def interests(self) -> list[dict[str, Any]]:
        return self.data['interests']

    def get_by_name(self, name: str) -> dict[str, Any] | None:
        name = (name or '').strip()
        if not name:
            return None
        lowered = name.lower()
        for item in self.interests():
            names = [item['name'], *item.get('aliases', [])]
            if any((n.lower() == lowered for n in names)):
                return item
        return None

    def get_by_key(self, key: str) -> dict[str, Any] | None:
        """按 id 或名字/别名找兴趣（页面里两种都可能在用）。"""
        key = str(key or '').strip()
        if not key:
            return None
        for item in self.interests():
            if str(item.get('id') or '') == key:
                return item
        return self.get_by_name(key)

    def match(self, text: str) -> list[dict[str, Any]]:
        """找出文本里提到的兴趣，按 affinity 从高到低排序。"""
        text = (text or '').strip()
        if not text:
            return []
        lowered = text.lower()
        matched: list[dict[str, Any]] = []
        for item in self.interests():
            keys = [item['name'], *item.get('aliases', [])]
            if any((k and (k in text or k.lower() in lowered) for k in keys)):
                matched.append(item)
        matched.sort(key=lambda x: x.get('affinity', 0.0), reverse=True)
        return matched

    def related(self, text: str, min_score: float=0.35, limit: int=3) -> list[tuple[float, dict[str, Any]]]:
        """找出「没直接提名字、但在聊她惦记的事」的兴趣，按相关度排序。"""
        text = (text or '').strip()
        if len(text) < 6 or not self.interests():
            return []
        text_tokens = _text_tokens(text)
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in self.interests():
            score = _related_score(item, text_tokens)
            if score >= max(0.05, min_score):
                scored.append((score, item))
        scored.sort(key=lambda pair: (pair[0], pair[1].get('affinity', 0.0)), reverse=True)
        return scored[:max(1, limit)]

    def top(self, limit: int=3) -> list[dict[str, Any]]:
        return sorted(self.interests(), key=lambda x: x.get('affinity', 0.0), reverse=True)[:limit]

    def add(self, name: str, aliases=None, affinity: float=0.6, pinned: bool=False, kind: str='auto') -> dict[str, Any]:
        item = self.get_by_name(name)
        if item:
            for alias in _as_list(aliases):
                if alias not in item['aliases'] and alias != item['name']:
                    item['aliases'].append(alias)
            item['affinity'] = max(item['affinity'], _as_float(affinity, 0.6))
            item['last_seen'] = _now()
            if pinned:
                item['pinned'] = True
                item['kind'] = kind if kind not in ('auto', '') else 'master'
            self.save()
            return item
        item = self._new_item(name.strip(), aliases, affinity, pinned, kind)
        self.data['interests'].append(item)
        self.save()
        return item

    def remove(self, name: str) -> bool:
        item = self.get_by_name(name)
        if not item:
            return False
        self.data['interests'].remove(item)
        self.save()
        return True

    def touch_mention(self, item: dict[str, Any]) -> None:
        item['mention_count'] = _as_int(item.get('mention_count'), 0) + 1
        item['last_mention'] = _now()
        item['last_seen'] = _now()
        item['affinity'] = min(1.0, _as_float(item.get('affinity'), 0.6) + 0.02)

    # ------------------------------------------- 短期话题 / 升级 / 自动清理
    def candidates(self) -> list[dict[str, Any]]:
        return self.data.setdefault('candidates', [])

    def get_candidate(self, name: str) -> dict[str, Any] | None:
        name = str(name or '').strip()
        if not name:
            return None
        lowered = name.lower()
        for cand in self.candidates():
            keys = [str(cand.get('name') or ''), *_as_list(cand.get('aliases'))]
            if any((k and k.lower() == lowered for k in keys)):
                return cand
        return None

    def get_candidate_by_key(self, key: str) -> dict[str, Any] | None:
        key = str(key or '').strip()
        if not key:
            return None
        for cand in self.candidates():
            if str(cand.get('id') or '') == key:
                return cand
        return self.get_candidate(key)

    def observe_topic(self, text: str) -> dict[str, Any] | None:
        """聊天里提到「还在观察」的临时话题时 +1；聊够了就升级成正式兴趣。

        临时话题只是短期保存：超过 candidate_ttl_days 没再聊就自动清掉。
        """
        text = str(text or '').strip()
        if not text:
            return None
        lowered = text.lower()
        hits = []
        for cand in list(self.candidates()):
            keys = [str(cand.get('name') or ''), *_as_list(cand.get('aliases'))]
            if any((k and (k in text or k.lower() in lowered) for k in keys)):
                hits.append(cand)
        if not hits:
            return None
        promoted = None
        need = max(1, _as_int(_RUNTIME.get('promote_mentions'), 3))
        for cand in hits:
            cand['mentions'] = _as_int(cand.get('mentions'), 0) + 1
            cand['last_seen'] = _now()
            if cand['mentions'] >= need and not self.get_by_name(cand['name']):
                promoted = self.promote(cand['name'], cand.get('aliases'), cand['mentions'])
        self.save()
        return promoted

    def remember_candidate(self, name: str, aliases=None, source: str='chat', note: dict[str, Any] | None=None) -> dict[str, Any]:
        """把一个只是「刚聊到/刚搜过」的话题放进观察区，先不占正式兴趣位。"""
        name = str(name or '').strip()
        if not name:
            return {}
        cand = self.get_candidate(name)
        if cand is None:
            cand = {
                'id': _candidate_id(name),
                'name': name[:40],
                'aliases': _as_list(aliases),
                'mentions': 1,
                'first_seen': _now(),
                'last_seen': _now(),
                'notes': [],
                'source': source,
            }
            self.candidates().append(cand)
            logger.info('[dafeiyu_interest] 新临时话题「%s」（来源 %s）', cand['name'], source)
        else:
            cand['last_seen'] = _now()
            for alias in _as_list(aliases):
                if alias not in cand['aliases'] and alias != cand['name']:
                    cand['aliases'].append(alias)
        if note:
            cand.setdefault('notes', []).append(note)
            cand['notes'] = cand['notes'][-4:]
        self.save()
        return cand

    def promote(self, name: str, aliases=None, mentions: int=0) -> dict[str, Any]:
        """把临时话题升级成正式兴趣（还没到次数时也可以由主人手动升级）。"""
        item = self.get_by_name(name) or self.add(name, aliases, 0.55, pinned=False, kind='auto')
        item['mention_count'] = max(_as_int(item.get('mention_count'), 0), _as_int(mentions, 0))
        item['last_mention'] = _now()
        item['last_seen'] = _now()
        cand = self.get_candidate(name)
        if cand is not None:
            try:
                self.data['candidates'].remove(cand)
            except ValueError:
                pass
        logger.info('[dafeiyu_interest] 话题「%s」聊得够多，升级成兴趣', item['name'])
        self.save()
        return item

    def prune(self) -> dict[str, list[str]]:
        """清理久不聊的临时话题和兴趣；pinned（人设/主人相关）永不自动删。"""
        now = _now()
        cand_ttl = _as_int(_RUNTIME.get('candidate_ttl_days'), 3) * 86400
        int_ttl = _as_int(_RUNTIME.get('interest_ttl_days'), 7) * 86400
        decay = _as_float(_RUNTIME.get('affinity_decay_per_day'), 0.02)
        removed_i: list[str] = []
        removed_c: list[str] = []
        changed = False
        keep = []
        for item in self.interests():
            if item.get('pinned'):
                keep.append(item)
                continue
            last = max(
                _as_float(item.get('last_seen'), 0.0),
                _as_float(item.get('last_mention'), 0.0),
                _as_float(item.get('created_at'), 0.0),
            )
            idle = now - last if last else 0.0
            if int_ttl > 0 and idle > int_ttl:
                removed_i.append(item['name'])
                changed = True
                continue
            if decay > 0 and idle > 86400:
                old = _as_float(item.get('affinity'), 0.55)
                new = max(0.3, old - decay * (idle / 86400.0))
                if abs(new - old) > 1e-6:
                    item['affinity'] = new
                    changed = True
            keep.append(item)
        if removed_i:
            self.data['interests'] = keep
        if cand_ttl > 0:
            kept_c = []
            for cand in self.candidates():
                last = _as_float(cand.get('last_seen'), 0.0) or _as_float(cand.get('first_seen'), 0.0)
                if last and now - last > cand_ttl:
                    removed_c.append(str(cand.get('name') or ''))
                    changed = True
                    continue
                kept_c.append(cand)
            self.data['candidates'] = kept_c
        if changed:
            self.save()
            logger.info('[dafeiyu_interest] 自动清理：兴趣 %s，临时话题 %s', removed_i, removed_c)
        return {'interests': removed_i, 'candidates': removed_c}

    def add_note(self, item: dict[str, Any], text: str, title: str='', url: str='', source: str='') -> bool:
        text = (text or '').strip()
        if not text:
            return False
        for note in item.get('notes', []):
            if url and note.get('url') == url:
                return False
            if not url and note.get('text', '').strip() == text:
                return False
        note = {'at': _now(), 'text': text[:300], 'title': (title or '').strip()[:120], 'url': (url or '').strip()[:500], 'source': source}
        note['id'] = _note_id(note)
        item.setdefault('notes', []).append(note)
        max_notes = _as_int(_RUNTIME.get('max_notes_per_interest'), 8)
        if max_notes > 0:
            item['notes'] = item['notes'][-max_notes:]
        self.save()
        return True

    @staticmethod
    def get_note(item: dict[str, Any], note_id: str) -> dict[str, Any] | None:
        note_id = str(note_id or '').strip()
        if not note_id:
            return None
        for note in item.get('notes') or []:
            if str(note.get('id') or '') == note_id:
                return note
        return None

    def update_note(self, item: dict[str, Any], note_id: str, text: str | None=None, title: str | None=None, url: str | None=None) -> bool:
        note = self.get_note(item, note_id)
        if note is None:
            return False
        if text is not None and str(text).strip():
            note['text'] = str(text).strip()[:300]
        if title is not None:
            note['title'] = str(title).strip()[:120]
        if url is not None:
            note['url'] = str(url).strip()[:500]
        note['edited_at'] = _now()
        self.save()
        return True

    def remove_note(self, item: dict[str, Any], note_id: str) -> bool:
        notes = item.get('notes') or []
        for idx, note in enumerate(notes):
            if str(note.get('id') or '') == str(note_id or '').strip():
                notes.pop(idx)
                self.save()
                return True
        return False

    def bump_search(self) -> None:
        stats = self.data.setdefault('stats', {})
        stats['searches'] = _as_int(stats.get('searches'), 0) + 1
        daily = stats.setdefault('daily', {})
        today = _today()
        daily[today] = _as_int(daily.get(today), 0) + 1
        if len(daily) > 7:
            for key in sorted(daily.keys())[:-7]:
                daily.pop(key, None)
        self.save()

    def searches_today(self) -> int:
        stats = self.data.setdefault('stats', {})
        return _as_int((stats.get('daily') or {}).get(_today()), 0)

async def _baidu_search(query: str, top_k: int=3) -> list[dict[str, str]]:
    query = (query or '').strip()
    if not query:
        return []
    try:
        if _CTX is None:
            return []
        cfg = _CTX.get_config()
        provider_settings = cfg.get('provider_settings', {}) or {}
        key = str(provider_settings.get('websearch_baidu_app_builder_key', '') or '').strip()
        if not key:
            logger.debug('[dafeiyu_interest] 未配置百度搜索 key，跳过搜索')
            return []
        import aiohttp
        payload = {'messages': [{'role': 'user', 'content': query[:72]}], 'search_source': 'baidu_search_v2', 'resource_type_filter': [{'type': 'web', 'top_k': max(1, min(10, top_k))}]}
        headers = {'Authorization': f'Bearer {key}', 'X-Appbuilder-Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(BAIDU_SEARCH_URL, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning('[dafeiyu_interest] 搜索失败 HTTP %s', resp.status)
                    return []
                data = await resp.json(content_type=None)
        refs = data.get('references')
        if not isinstance(refs, list):
            inner = data.get('data') if isinstance(data.get('data'), dict) else {}
            refs = inner.get('references') if isinstance(inner, dict) else []
        results: list[dict[str, str]] = []
        for ref in refs or []:
            if not isinstance(ref, dict):
                continue
            url = str(ref.get('url') or '').strip()
            if not url:
                continue
            results.append({'title': str(ref.get('title') or '').strip(), 'url': url, 'content': str(ref.get('content') or ref.get('snippet') or '').strip()})
        return results
    except Exception as exc:
        logger.warning('[dafeiyu_interest] 搜索异常: %s', exc)
        return []

def _note_text(result: dict[str, str]) -> str:
    content = re.sub('\\s+', ' ', result.get('content', '')).strip()
    title = result.get('title', '').strip()
    if content:
        return f'{title}：{content[:150]}' if title else content[:150]
    return title[:150]

def _format_notes(item: dict[str, Any], limit: int=2) -> str:
    notes = item.get('notes') or []
    if not notes:
        return ''
    lines = []
    for note in notes[-limit:]:
        text = re.sub('\\s+', ' ', str(note.get('text') or '')).strip()
        if text:
            lines.append(text[:120])
    return '；'.join(lines)

async def _auto_search_for(item: dict[str, Any]) -> None:
    """后台搜索一个兴趣并记录结果。"""
    if _STORE is None:
        return
    try:
        query = f'{item['name']} 冷知识'
        results = await _baidu_search(query, _as_int(_RUNTIME.get('auto_search_top_k'), 3))
        if not results:
            return
        added = 0
        for result in results[:2]:
            if _STORE.add_note(item, _note_text(result), result.get('title', ''), result.get('url', ''), 'search'):
                added += 1
        item['last_search'] = _now()
        _STORE.bump_search()
        _STORE.save()
        logger.info('[dafeiyu_interest] 兴趣搜索「%s」：%s 条结果，记录 %s 条', query, len(results), added)
    except Exception as exc:
        logger.warning('[dafeiyu_interest] 后台搜索失败: %s', exc)

def _schedule_auto_search(event, matched: list[dict[str, Any]]) -> None:
    """按门槛/冷却/每日上限决定要不要后台搜索。一个事件只调度一次。"""
    try:
        if not _RUNTIME.get('auto_search_enabled', True) or _STORE is None:
            return
        if not matched:
            return
        if event.get_extra(_EXTRA_SEARCH_DONE):
            return
        min_affinity = _as_float(_RUNTIME.get('auto_search_min_affinity'), 0.7)
        cooldown = _as_float(_RUNTIME.get('auto_search_cooldown_seconds'), 1800.0)
        max_per_day = _as_int(_RUNTIME.get('auto_search_max_per_day'), 6)
        now = _now()
        for item in matched:
            if item.get('affinity', 0.0) < min_affinity:
                continue
            if now - item.get('last_search', 0.0) < cooldown:
                continue
            if max_per_day > 0 and _STORE.searches_today() >= max_per_day:
                return
            item['last_search'] = now
            _STORE.save()
            event.set_extra(_EXTRA_SEARCH_DONE, True)
            task = asyncio.create_task(_auto_search_for(item))
            _TASKS.add(task)
            task.add_done_callback(_TASKS.discard)
            return
    except Exception as exc:
        logger.debug('[dafeiyu_interest] 调度搜索失败: %s', exc)

def _build_hint_block(matched: list[dict[str, Any]], related: bool=False) -> str:
    if _STORE is None or not _RUNTIME.get('hints_enabled', True):
        return ''
    max_items = _as_int(_RUNTIME.get('hints_max_items'), 3)
    max_notes = _as_int(_RUNTIME.get('hints_max_notes'), 2)
    if matched:
        items = matched[:max(1, max_items)]
        if related:
            header = '【大肥鱼的私人兴趣备忘】（只影响语气，别照念）这条消息可能跟她惦记的事有关：' + '、'.join((i['name'] for i in matched[:3]))
        else:
            header = '【大肥鱼的私人兴趣备忘】（只影响语气，别照念）这次聊到了：' + '、'.join((i['name'] for i in matched[:3]))
    else:
        items = _STORE.top(max(1, max_items))
        header = '【大肥鱼的私人兴趣备忘】（只影响语气，别照念）最近惦记：'
    lines = []
    for item in items:
        notes = _format_notes(item, max_notes)
        suffix = f'（记得：{notes}）' if notes else ''
        lines.append(f'- {item['name']}{suffix}')
    block = header + '\n' + '\n'.join(lines)
    if related:
        block += '\n这条消息跟她惦记的事沾边，可以顺势接一句、语气来劲一点，但别硬扯，别把备忘念出来。'
    elif matched:
        block += '\n聊到这些兴趣时可以稍微来劲、接话更主动一点，但别硬扯，别把备忘念出来。'
    return block

def _group_key(event, umo: str) -> str:
    try:
        return str(event.get_group_id() or umo)
    except Exception:
        return umo

def _cooldown_ok(store: dict, key: str, cooldown: float) -> bool:
    if cooldown <= 0:
        return True
    now = _now()
    last = store.get(key)
    if last is not None and now - last < cooldown:
        return False
    store[key] = now
    return True

def _pin_master_related(event, matched) -> None:
    """主人聊到的兴趣升成「主人相关」并永久保留，永远不会被自动清理掉。"""
    if not matched or _STORE is None:
        return
    try:
        if not event.is_admin():
            return
    except Exception:
        return
    changed = False
    for item in matched:
        item['last_seen'] = _now()
        if item.get('pinned'):
            continue  # 人设种子等其他 pinned 兴趣保持原 kind
        item['pinned'] = True
        item['kind'] = 'master'
        changed = True
        logger.info('[dafeiyu_interest] 主人聊到「%s」，标成主人相关兴趣（不自动清理）', item.get('name'))
    if changed:
        _STORE.save()


def _lively_boost(umo: str) -> float:
    """群里正热闹时（由 group_wake 模块统计）提高主动接话的概率。"""
    try:
        import sys
        for name, mod in list(sys.modules.items()):
            if not name.endswith('group_wake_feature'):
                continue
            fn = getattr(mod, 'activity_boost_for', None)
            if fn is not None:
                return max(1.0, float(fn(umo)))
    except Exception:
        pass
    return 1.0


def _interest_should_wake(event) -> bool:
    try:
        if not _RUNTIME.get('enabled', True) or not _RUNTIME.get('wake_enabled', True):
            return False
        umo = getattr(event, 'unified_msg_origin', '') or ''
        if not _bot_match(umo):
            return False
        if _RUNTIME.get('only_group', True):
            try:
                is_group = bool(event.get_group_id())
            except Exception:
                is_group = False
            if not is_group and ':GroupMessage:' not in umo:
                return False
        text = (event.message_str or '').strip()
        if not text or text.startswith('/'):
            return False
        if _STORE is None:
            return False
        if not event.get_extra(_EXTRA_OBSERVED):
            event.set_extra(_EXTRA_OBSERVED, True)
            try:
                promoted = _STORE.observe_topic(text)
                if promoted is not None:
                    logger.info('[dafeiyu_interest] 临时话题聊够了，升级成兴趣：%s', promoted.get('name'))
            except Exception as exc:
                logger.debug('[dafeiyu_interest] 观察临时话题失败: %s', exc)
        cooldown = _as_float(_RUNTIME.get('wake_cooldown_seconds'), 180.0)
        try:
            is_master = bool(event.is_admin())
        except Exception:
            is_master = False
        boost = _as_float(_RUNTIME.get('master_wake_boost'), 1.6) if is_master else 1.0
        lively = _lively_boost(umo)
        threshold_scale = _as_float(_RUNTIME.get('master_wake_threshold_scale'), 0.7) if is_master else 1.0
        matched = _STORE.match(text)
        if matched:
            try:
                _pin_master_related(event, matched)
            except Exception:
                pass
            best = max((item.get('affinity', 0.0) for item in matched))
            threshold = _as_float(_RUNTIME.get('wake_threshold'), 0.45) * threshold_scale
            if best < threshold:
                return False
            probability = _as_float(_RUNTIME.get('wake_probability'), 55.0) * min(1.0, max(0.05, best))
            if is_master:
                probability = min(98.0, probability * max(1.0, boost))
            probability = min(98.0, probability * lively)
            if probability <= 0 or random.random() * 100.0 >= probability:
                return False
            if not _cooldown_ok(_LAST_WAKE, _group_key(event, umo), cooldown):
                return False
            try:
                event.set_extra(_EXTRA_MATCHED, [item['name'] for item in matched])
                event.set_extra(_EXTRA_WAKE, 'interest')
            except Exception:
                pass
            return True
        if not _RUNTIME.get('wake_related_enabled', True):
            return False
        min_score = _as_float(_RUNTIME.get('wake_related_min_score'), 0.35)
        if is_master:
            min_score = max(0.05, min_score * threshold_scale)
        related = _STORE.related(text, min_score)
        if not related:
            return False
        score, _top_item = related[0]
        names = [item['name'] for _score, item in related]
        probability = _as_float(_RUNTIME.get('wake_related_probability'), 35.0) * min(1.0, score / 0.6)
        if is_master:
            probability = min(95.0, probability * max(1.0, boost))
        probability = min(95.0, probability * lively)
        if probability <= 0 or random.random() * 100.0 >= probability:
            return False
        if not _cooldown_ok(_LAST_WAKE, _group_key(event, umo), cooldown):
            return False
        try:
            event.set_extra(_EXTRA_MATCHED, names)
            event.set_extra(_EXTRA_RELATED, True)
            event.set_extra(_EXTRA_WAKE, 'related')
        except Exception:
            pass
        logger.info('[dafeiyu_interest] 兴趣相关唤醒：%s | 相关=%s | 相关度=%.2f | %s', umo, names, score, text[:40])
        return True
    except Exception as exc:
        logger.debug('[dafeiyu_interest] 唤醒判定失败: %s', exc)
        return False

def start_background(interval_seconds: float=3600.0) -> None:
    """启动定期清理任务：久不聊的临时话题/兴趣自动删掉。"""
    if _STORE is None or not _RUNTIME.get('prune_enabled', True):
        return
    interval = max(60.0, _as_float(interval_seconds, 3600.0))

    async def _loop():
        await asyncio.sleep(min(20.0, interval))
        while True:
            try:
                _STORE.prune()
            except Exception as exc:
                logger.warning('[dafeiyu_interest] 定期清理失败: %s', exc)
            await asyncio.sleep(interval)

    task = asyncio.create_task(_loop())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    logger.info('[dafeiyu_interest] 已启动兴趣自动清理（每 %s 秒）', int(interval))

class InterestWakeFilter(CustomFilter):
    """群聊里聊到她感兴趣的话题时，按概率唤醒回复。"""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        try:
            if silent_guard is not None and silent_guard.is_silent(event):
                return False
        except Exception:
            pass
        return _interest_should_wake(event)

class InterestFeature:

    @filter.custom_filter(InterestWakeFilter, False)
    async def wake_on_interest(self, event: AstrMessageEvent):
        try:
            if not event.is_at_or_wake_command:
                event.is_at_or_wake_command = True
            event.is_wake = True
            names = event.get_extra(_EXTRA_MATCHED) or []
            related = bool(event.get_extra(_EXTRA_RELATED))
            if _STORE is not None:
                stats = _STORE.data.setdefault('stats', {})
                stats['wakes'] = _as_int(stats.get('wakes'), 0) + 1
                if related:
                    stats['related_wakes'] = _as_int(stats.get('related_wakes'), 0) + 1
                _STORE.save()
            logger.info('[dafeiyu_interest] 兴趣%s唤醒：%s | %s | 命中=%s', '相关' if related else '', event.unified_msg_origin, (event.message_str or '')[:60], names)
        except Exception as exc:
            logger.warning('[dafeiyu_interest] 唤醒事件时出错: %s', exc)

    @filter.on_llm_request(priority=5040)
    async def inject_interest_hints(self, event: AstrMessageEvent, req):
        """把兴趣备忘 + 本轮提到的兴趣作为临时上下文注入。"""
        try:
            if not _RUNTIME.get('enabled', True) or _STORE is None:
                return
            umo = getattr(event, 'unified_msg_origin', '') or ''
            if not _bot_match(umo):
                return
            text = (event.message_str or '').strip()
            if text and not event.get_extra(_EXTRA_OBSERVED):
                event.set_extra(_EXTRA_OBSERVED, True)
                try:
                    _STORE.observe_topic(text)
                except Exception:
                    pass
            matched = _STORE.match(text) if text else []
            related_mode = False
            if not matched:
                names = event.get_extra(_EXTRA_MATCHED) or []
                for name in names:
                    item = _STORE.get_by_name(name)
                    if item is not None and item not in matched:
                        matched.append(item)
                matched = matched[:3]
                related_mode = bool(matched) and bool(event.get_extra(_EXTRA_RELATED))
            block = _build_hint_block(matched, related=related_mode)
            if block:
                part = TextPart(text=block)
                try:
                    part.mark_as_temp()
                except Exception:
                    pass
                req.extra_user_content_parts.append(part)
            if matched and (not related_mode) and (not event.get_extra(_EXTRA_MENTION_DONE)):
                event.set_extra(_EXTRA_MENTION_DONE, True)
                for item in matched[:2]:
                    _STORE.touch_mention(item)
                _STORE.save()
                try:
                    _pin_master_related(event, matched)
                except Exception as exc:
                    logger.debug('[dafeiyu_interest] 标记主人相关兴趣失败: %s', exc)
                _schedule_auto_search(event, matched)
        except Exception as exc:
            logger.warning('[dafeiyu_interest] 注入兴趣备忘失败: %s', exc)

    @staticmethod
    def _require_master(event: AstrMessageEvent) -> str:
        try:
            ok = bool(event.is_admin())
        except Exception:
            ok = False
        if ok:
            return ''
        return '（这是本鲸和主人的小本本，别人碰不了。想聊这个就直接聊天。）'

    @llm_tool(name='interest_list')
    async def interest_list(self, event: AstrMessageEvent) -> str:
        """列出大肥鱼当前记录的兴趣和喜欢程度。

        不需要参数。返回兴趣列表文本。
        """
        deny = self._require_master(event)
        if deny:
            return deny
        if _STORE is None:
            return '兴趣库还没准备好。'
        rows = []
        for item in _STORE.interests():
            rows.append(f'{item['name']}（喜欢 {item.get('affinity', 0):.2f}，被提到 {item.get('mention_count', 0)} 次，记录 {len(item.get('notes') or [])} 条）')
        if not rows:
            return '暂时没有记录兴趣。'
        text = '本鲸的兴趣：' + '；'.join(rows[:20])
        cands = _STORE.candidates()[:3]
        if cands:
            text += '\n还在观察的临时话题（聊得多才算兴趣）：' + '、'.join(f"{c['name']}({c.get('mentions', 0)}次)" for c in cands)
        return text

    @llm_tool(name='interest_add')
    async def interest_add(self, event: AstrMessageEvent, name: str, aliases: str) -> str:
        """给大肥鱼增加一个兴趣。

        Args:
            name(string): 兴趣名称，例如「鲸鱼」
            aliases(string): 别名，用英文逗号分隔，可留空
        """
        deny = self._require_master(event)
        if deny:
            return deny
        if _STORE is None:
            return '兴趣库还没准备好。'
        item = _STORE.add(name, aliases, 0.7, pinned=True, kind='master')
        return f'记下了，本鲸对「{item['name']}」感兴趣了（喜欢程度 {item['affinity']:.2f}）。'

    @llm_tool(name='interest_remove')
    async def interest_remove(self, event: AstrMessageEvent, name: str) -> str:
        """删除一个兴趣。

        Args:
            name(string): 要删除的兴趣名称
        """
        deny = self._require_master(event)
        if deny:
            return deny
        if _STORE is None:
            return '兴趣库还没准备好。'
        return f'「{name}」删掉了。' if _STORE.remove(name) else f'没找到「{name}」这个兴趣。'

    @llm_tool(name='interest_search')
    async def interest_search(self, event: AstrMessageEvent, topic: str) -> str:
        """上网搜一下某个兴趣/话题的最新消息。

        只是查资料：已经记成兴趣的话题会把结果记在它下面；还没记成兴趣的只算「临时观察」，
        聊得多才会正式收进兴趣库，所以不用担心查一次就记一辈子。

        Args:
            topic(string): 想搜的兴趣或话题
        """
        topic = str(topic or '').strip()
        if not topic:
            return '要搜什么？'
        deny = self._require_master(event)
        if deny:
            return deny
        if _STORE is None:
            return '兴趣库还没准备好。'
        results = await _baidu_search(topic, 3)
        if not results:
            return '没搜到东西，可能是网络或搜索配置的问题。'
        summary = '；'.join((_note_text(r)[:80] for r in results[:2]))
        item = _STORE.get_by_name(topic)
        if item is None:
            # 只是临时话题：先记在观察区，聊得多才会升级成正式兴趣
            _STORE.remember_candidate(topic, source='search')
            _STORE.bump_search()
            return f'搜到 {len(results)} 条。「{topic}」先当临时话题记着（还没算本鲸的兴趣），聊得多了再收进兴趣库：{summary}'
        added = 0
        for result in results[:2]:
            if _STORE.add_note(item, _note_text(result), result.get('title', ''), result.get('url', ''), 'tool'):
                added += 1
        item['last_search'] = _now()
        _STORE.bump_search()
        _STORE.save()
        return f'搜到 {len(results)} 条，记了 {added} 条到「{item['name']}」：{summary}'

    @llm_tool(name='interest_remember')
    async def interest_remember(self, event: AstrMessageEvent, interest: str, note: str) -> str:
        """把一条信息记到某个兴趣下面。

        Args:
            interest(string): 兴趣名称
            note(string): 要记下的内容
        """
        deny = self._require_master(event)
        if deny:
            return deny
        if _STORE is None:
            return '兴趣库还没准备好。'
        item = _STORE.get_by_name(interest) or _STORE.add(interest, [], 0.55, pinned=True, kind='master')
        if not item.get('pinned'):
            item['pinned'] = True
            item['kind'] = 'master'
        item['last_seen'] = _now()
        ok = _STORE.add_note(item, note, '', '', 'user')
        return f'记到「{item['name']}」下面了。' if ok else '这条已经记过了。'

    @llm_tool(name='interest_recall')
    async def interest_recall(self, event: AstrMessageEvent, interest: str) -> str:
        """回忆某个兴趣下面记录过的内容。

        Args:
            interest(string): 兴趣名称
        """
        deny = self._require_master(event)
        if deny:
            return deny
        if _STORE is None:
            return '兴趣库还没准备好。'
        item = _STORE.get_by_name(interest)
        if not item:
            return f'「{interest}」还没记过。'
        notes = item.get('notes') or []
        if not notes:
            return f'「{item['name']}」还没有记下的内容。'
        lines = []
        for note in notes[-3:]:
            lines.append('- ' + re.sub('\\s+', ' ', str(note.get('text') or ''))[:120])
        return f'「{item['name']}」记下的：' + '；'.join(lines)

    @filter.command('兴趣')
    async def cmd_interest(self, event: AstrMessageEvent):
        try:
            if _STORE is None:
                yield event.plain_result('兴趣库还没准备好。')
                return
            raw = (event.message_str or '').strip().replace('／', '/')
            body = raw
            # AstrBot 在唤醒阶段会把唤醒前缀（例如 / 或机器人名字）从 message_str 上剥掉，
            # 群里发 /兴趣 看 到这里可能只剩「兴趣 看」，所以三种前缀都试一遍。
            for prefix_text in ('/兴趣', '兴趣'):
                if body.startswith(prefix_text):
                    body = body[len(prefix_text):].strip()
                    break
            if not body:
                yield event.plain_result(self._cmd_list_text())
                return
            parts = body.split(maxsplit=1)
            action = parts[0]
            rest = parts[1].strip() if len(parts) > 1 else ''
            if action in ('加', '添加', '新增'):
                if not self._is_master(event):
                    yield event.plain_result('只有主人能加。')
                    return
                if not rest:
                    yield event.plain_result('用法：/兴趣 加 名字 别名1,别名2')
                    return
                pieces = rest.split(maxsplit=1)
                name = pieces[0]
                aliases = pieces[1] if len(pieces) > 1 else ''
                item = _STORE.add(name, aliases, 0.7, pinned=True, kind='master')
                yield event.plain_result(f'记下了，本鲸对「{item['name']}」感兴趣（这个会一直留着）。')
                return
            if action in ('删', '删除'):
                if not self._is_master(event):
                    yield event.plain_result('只有主人能删。')
                    return
                name = rest.strip()
                if not name:
                    yield event.plain_result('用法：/兴趣 删 名字')
                    return
                ok = _STORE.remove(name)
                yield event.plain_result(f'「{name}」删掉了。' if ok else f'没找到「{name}」。')
                return
            if action in ('搜', '搜索'):
                if not self._is_master(event):
                    yield event.plain_result('只有主人能让本鲸去搜。')
                    return
                topic = rest.strip()
                if not topic:
                    yield event.plain_result('用法：/兴趣 搜 话题')
                    return
                result = await self.interest_search(event, topic)
                yield event.plain_result(result)
                return
            if action in ('记', '记录'):
                if not self._is_master(event):
                    yield event.plain_result('只有主人能让本鲸记。')
                    return
                pieces = rest.split(maxsplit=1)
                if len(pieces) < 2:
                    yield event.plain_result('用法：/兴趣 记 名字 内容')
                    return
                result = await self.interest_remember(event, pieces[0], pieces[1])
                yield event.plain_result(result)
                return
            if action in ('看', '查看', '详情'):
                item = _STORE.get_by_name(rest.strip())
                if not item:
                    yield event.plain_result(f'没找到「{rest.strip()}」。')
                    return
                notes = item.get('notes') or []
                lines = [f'{item['name']}：喜欢 {item.get('affinity', 0):.2f}，提到 {item.get('mention_count', 0)} 次，记了 {len(notes)} 条']
                for note in notes[-3:]:
                    lines.append('- ' + re.sub('\\s+', ' ', str(note.get('text') or ''))[:100])
                yield event.plain_result('\n'.join(lines))
                return
            item = _STORE.get_by_name(body)
            if item:
                yield event.plain_result(f'{item['name']}：喜欢 {item.get('affinity', 0):.2f}')
                return
            yield event.plain_result('用法：/兴趣 | /兴趣 加 名字 别名 | /兴趣 删 名字 | /兴趣 搜 话题 | /兴趣 记 名字 内容 | /兴趣 看 名字')
        except Exception as exc:
            logger.warning('[dafeiyu_interest] /兴趣 命令出错: %s', exc)

    def _is_master(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    @staticmethod
    def _cmd_list_text() -> str:
        if _STORE is None:
            return '兴趣库还没准备好。'
        items = _STORE.top(8)
        if not items:
            return '本鲸现在没什么特别的兴趣。'
        rows = [f'{i['name']}({i.get('affinity', 0):.2f})' for i in items]
        text = '本鲸最近惦记：' + '、'.join(rows)
        cands = _STORE.candidates()[:4]
        if cands:
            text += '\n还在观察：' + '、'.join(f"{c['name']}({c.get('mentions', 0)}次)" for c in cands)
        return text
