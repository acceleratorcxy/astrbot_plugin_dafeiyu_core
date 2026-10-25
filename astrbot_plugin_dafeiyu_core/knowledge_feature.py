"""群聊知识库（knowledge 功能模块）

用户需求（m10784 / m10785）：
- 「还有每晚的总结，还要考虑一些群内黑话，群友昵称一类的东西，比如群内昵称奈白的雪子的那个，但其实大家叫他tom这种」
- 「每晚总结的这些总结成知识库，共后续大模型调用」

做法（不改框架源码、不写会话历史）：
1. 每晚（默认 03:40，备份之后）把每个群的发言记录 + 会话历史喂给模型，让它输出结构化档案：
   群成员昵称 / 大家平时怎么叫（外号、别名）/ 身份特点、群内黑话与梗、最近聊的话题。
2. 档案落在 plugin_data/astrbot_plugin_dafeiyu_core/knowledge.json，按 umo 分开，跨天累积（昵称对照、黑话不会丢）。
3. 她要在群里说话时，on_llm_request 把【本群档案】临时注入（mark_as_temp，不写历史、不破坏缓存），
   告诉她谁是谁、外号怎么叫、黑话什么意思、最近在聊什么。
4. 命令 /群档案（看 / 更新 / 全部）给主人查看和手动刷新。
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.agent.message import TextPart
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_ID = 'astrbot_plugin_dafeiyu_core'
TZ = timezone(timedelta(hours=8))
MARK = '【本群档案】'

DEFAULTS = {
    'enabled': True,
    'nightly_enabled': True,
    'nightly_hour': 3,
    'nightly_minute': 40,
    'max_groups': 20,
    'max_lines': 120,
    'provider_id': '',
    'inject_enabled': True,
    'inject_max_members': 12,
    'inject_max_slang': 10,
    'inject_max_topics': 5,
    'retain_days': 90,
    'prompt': '',
    # 单个群的总结超时（秒）：防止一个群卡住整晚的整理
    'summary_timeout_seconds': 180,
}

_RUNTIME = dict(DEFAULTS)
_SECTION: dict[str, Any] = {}

_STATE: dict[str, Any] = {
    'version': 1,
    'updated_at': '',
    'groups': {},
    'stats': {'runs': 0, 'groups': 0},
    # 每晚整理从第几个群开始（群数超过 max_groups 时轮转，保证后面的群也会被整理）
    'cursor': 0,
}
_STATE_FILE = 'knowledge.json'
_TASK = None
_LAST_RUN = {'at': '', 'groups': 0, 'ok': False}

SUMMARIZE_PROMPT = (
    '你是群聊档案整理员。下面是一个 QQ 群的聊天记录（可能混着群友发的图片说明）。'
    '请仔细读，整理成 JSON，只输出 JSON，不要解释、不要代码块。格式：'
    '{"group":"群名（不知道就空字符串）",'
    '"members":[{"nick":"群昵称","alias":"大家平时怎么叫他（外号/简称，没有就空）","note":"一句话身份或特点"}],'
    '"slang":[{"term":"群内黑话/梗/缩写","meaning":"什么意思"}],'
    '"topics":["最近在聊的话题，最多 5 条，每条不超过 20 字"]}。'
    '要求：1) members 只收出现过的人，昵称照抄，别编；2) 有人被叫外号（比如昵称是「奈白的雪子」但大家叫他 tom）'
    '一定要在 alias 里写清楚；3) 黑话只收真的需要解释的，普通词不要收；4) 没有的内容给空数组。'
)

_NAME_RE = re.compile(r'^\s*([^\s:：]{1,24})\s*[:：]\s*(.+)$')


def _now_iso() -> str:
    return datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')


def _as_int(value: Any, default: int, low: int = 0) -> int:
    try:
        return max(low, int(value))
    except Exception:
        return default


def _apply_runtime(section: Any) -> None:
    global _SECTION
    if not isinstance(section, dict):
        section = {}
    _SECTION = dict(section)
    for key, default in DEFAULTS.items():
        if key not in section:
            _RUNTIME[key] = default
            continue
        value = section.get(key)
        if key in ('enabled', 'nightly_enabled', 'inject_enabled'):
            _RUNTIME[key] = bool(value)
        elif key in ('nightly_hour', 'nightly_minute', 'max_groups', 'max_lines',
                     'inject_max_members', 'inject_max_slang', 'inject_max_topics', 'retain_days',
                     'summary_timeout_seconds'):
            _RUNTIME[key] = _as_int(value, default)
        else:
            _RUNTIME[key] = str(value or '').strip()
    _RUNTIME['nightly_hour'] = min(23, max(0, _RUNTIME['nightly_hour']))
    _RUNTIME['nightly_minute'] = min(59, max(0, _RUNTIME['nightly_minute']))


def _state_path(plugin: Any) -> Path:
    base = Path(get_astrbot_plugin_data_path()) / PLUGIN_ID
    return base / _STATE_FILE


def _normalize_group(entry: Any) -> dict:
    if not isinstance(entry, dict):
        entry = {}
    members = []
    for item in entry.get('members') or []:
        if not isinstance(item, dict):
            continue
        nick = str(item.get('nick') or '').strip()
        if not nick:
            continue
        members.append({
            'nick': nick[:40],
            'alias': str(item.get('alias') or '').strip()[:40],
            'note': str(item.get('note') or '').strip()[:80],
            'seen': str(item.get('seen') or '')[:24],
        })
    slang = []
    for item in entry.get('slang') or []:
        if not isinstance(item, dict):
            continue
        term = str(item.get('term') or '').strip()
        if not term:
            continue
        slang.append({'term': term[:30], 'meaning': str(item.get('meaning') or '').strip()[:120]})
    topics = []
    for item in entry.get('topics') or []:
        text = item if isinstance(item, str) else (item.get('text') if isinstance(item, dict) else '')
        text = str(text or '').strip()
        if text and text not in topics:
            topics.append(text[:60])
    return {
        'name': str(entry.get('name') or '').strip()[:60],
        'members': members[:80],
        'slang': slang[:80],
        'topics': topics[-10:],
        'updated_at': str(entry.get('updated_at') or ''),
        'lines': _as_int(entry.get('lines'), 0),
    }


def _load(plugin: Any) -> None:
    global _STATE
    path = _state_path(plugin)
    data = None
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding='utf-8-sig'))
    except Exception as exc:
        logger.warning('[dafeiyu_knowledge] 读取知识库失败: %s', exc)
    state = {'version': 1, 'updated_at': '', 'groups': {}, 'stats': {'runs': 0, 'groups': 0}, 'cursor': 0}
    if isinstance(data, dict):
        state['updated_at'] = str(data.get('updated_at') or '')
        state['cursor'] = _as_int(data.get('cursor'), 0)
        stats = data.get('stats') if isinstance(data.get('stats'), dict) else {}
        state['stats'] = {
            'runs': _as_int(stats.get('runs'), 0),
            'groups': _as_int(stats.get('groups'), 0),
        }
        groups = data.get('groups') if isinstance(data.get('groups'), dict) else {}
        for umo, entry in groups.items():
            state['groups'][str(umo)] = _normalize_group(entry)
    _STATE = state


def _save() -> None:
    path = Path(_STATE.get('_path') or '')
    try:
        target = Path(_STATE['_path']) if _STATE.get('_path') else None
    except Exception:
        target = None
    if target is None:
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _STATE['updated_at'] = _now_iso()
        payload = {k: v for k, v in _STATE.items() if not k.startswith('_')}
        tmp = target.with_suffix('.tmp')
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
        tmp.replace(target)
    except Exception as exc:
        logger.warning('[dafeiyu_knowledge] 保存知识库失败: %s', exc)


def _bind_path(plugin: Any) -> None:
    _STATE['_path'] = str(_state_path(plugin))


def _speaker_module():
    for name, module in list(sys.modules.items()):
        if name.endswith('speaker_feature'):
            return module
    return None


def _core_context_module():
    for name, module in list(sys.modules.items()):
        if name.endswith('core_context'):
            return module
    return None


_PREFIX_RE = re.compile(r'^\s*(群友|助手)\s*[:：]\s*')


def _dedupe_key(text: Any) -> str:
    """素材去重用的规范化文本：剥掉「群友：/助手：」前缀、去掉空白，取末尾 80 字。

    会话历史那边会先加「群友：/助手：」前缀，发言记录那边是原始正文，
    两边必须用同一个规范化函数，否则短消息永远对不上，同一条会进素材两次。
    """
    raw = _PREFIX_RE.sub('', str(text or ''))
    raw = re.sub(r'\s+', '', raw)
    return raw[-80:]


def _text_of_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get('text') or ''))
        return ' '.join(p for p in parts if p)
    if isinstance(content, dict):
        return str(content.get('text') or '')
    return ''


async def _group_name(plugin: Any, umo: str) -> str:
    try:
        ctx = _core_context_module()
        if ctx is not None and hasattr(ctx, 'umo_names'):
            names = await ctx.umo_names(plugin, [umo])
            return str((names or {}).get(umo) or '')
    except Exception:
        pass
    return ''


async def _material(plugin: Any, umo: str, limit: int) -> tuple[str, int]:
    """拼这个群的聊天素材：会话历史 + 发言记录。返回 (文本, 条数)。"""
    lines: list[str] = []
    seen: set[str] = set()

    cm = getattr(plugin.context, 'conversation_manager', None)
    if cm is not None:
        try:
            cid = await cm.get_curr_conversation_id(umo)
            conv = await cm.get_conversation(umo, cid) if cid else None
            history = json.loads(getattr(conv, 'history', '') or '[]') if conv else []
            for msg in history[-80:]:
                if not isinstance(msg, dict):
                    continue
                role = str(msg.get('role') or '')
                text = _text_of_content(msg.get('content')).strip()
                if not text:
                    continue
                if role == 'assistant':
                    # 助手发言带角色标记再规范化：别和群友说的同一句话互相顶掉去重名额
                    text = '助手：' + text
                    key = '助手|' + _dedupe_key(text)
                else:
                    if role == 'user':
                        text = '群友：' + text
                    # 群友发言与发言记录用同一个键（都剥掉前缀），同一条才不会进素材两次
                    key = _dedupe_key(text)
                if key in seen:
                    continue
                seen.add(key)
                lines.append(text)
        except Exception as exc:
            logger.debug('[dafeiyu_knowledge] 读会话历史失败 %s: %s', umo, exc)

    speaker = _speaker_module()
    entries = []
    if speaker is not None:
        try:
            entries = list((getattr(speaker, '_LOG', {}) or {}).get(umo) or [])
        except Exception:
            entries = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        text = str(item.get('x') or '').strip()
        if not text:
            continue
        name = str(item.get('n') or '').strip() or '群友'
        stamp = ''
        try:
            stamp = datetime.fromtimestamp(float(item.get('t') or 0), TZ).strftime('%H:%M')
        except Exception:
            stamp = ''
        line = '- %s %s：%s' % (stamp, name, text)
        key = _dedupe_key(text)
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)

    lines = lines[-max(10, limit):]
    try:
        owner = None
        for name, module in list(sys.modules.items()):
            if name.endswith('owner_feature'):
                owner = module
                break
        master = ''
        if owner is not None:
            master = str((getattr(owner, '_RUNTIME', {}) or {}).get('master_name') or '').strip()
        if master:
            lines.insert(0, '（注意：%s 是主人，不是普通群友；member 列表里也要标出他是主人）' % master)
    except Exception:
        pass
    return '\n'.join(lines), len(lines)


def _parse_json(text: str) -> dict | None:
    raw = str(text or '').strip()
    if not raw:
        return None
    raw = re.sub(r'^\\s*[' + chr(96) + r']{3}.*?\\n', '', raw, flags=re.S)
    raw = raw.replace(chr(96) * 3, '')
    start = raw.find('{')
    end = raw.rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _merge(umo: str, name: str, data: dict, n_lines: int) -> dict:
    entry = _normalize_group(_STATE['groups'].get(umo) or {})
    if name:
        entry['name'] = name[:60]
    got_name = str(data.get('group') or '').strip()
    if got_name:
        entry['name'] = got_name[:60]
    now = _now_iso()

    by_nick = {m['nick']: m for m in entry['members']}
    for item in data.get('members') or []:
        if not isinstance(item, dict):
            continue
        nick = str(item.get('nick') or '').strip()
        if not nick:
            continue
        alias = str(item.get('alias') or '').strip()
        note = str(item.get('note') or '').strip()
        old = by_nick.get(nick)
        if old is None:
            old = {'nick': nick[:40], 'alias': alias[:40], 'note': note[:80], 'seen': now}
            entry['members'].append(old)
            by_nick[nick] = old
        else:
            if alias:
                old['alias'] = alias[:40]
            if note:
                old['note'] = note[:80]
            old['seen'] = now

    slang = {s['term']: s for s in entry['slang']}
    for item in data.get('slang') or []:
        if not isinstance(item, dict):
            continue
        term = str(item.get('term') or '').strip()
        meaning = str(item.get('meaning') or '').strip()
        if not term or not meaning:
            continue
        slang[term[:30]] = {'term': term[:30], 'meaning': meaning[:120]}
    entry['slang'] = list(slang.values())[-80:]

    topics = list(entry['topics'])
    for item in data.get('topics') or []:
        text = item if isinstance(item, str) else (item.get('text') if isinstance(item, dict) else '')
        text = str(text or '').strip()
        if text and text not in topics:
            topics.append(text[:60])
    entry['topics'] = topics[-10:]
    entry['updated_at'] = now
    entry['lines'] = n_lines
    entry['members'] = entry['members'][-80:]
    _STATE['groups'][umo] = entry
    return entry


async def _summarize(plugin: Any, umo: str, name: str, material: str) -> dict | None:
    provider = None
    try:
        pid = str(_RUNTIME.get('provider_id') or '').strip()
        if pid:
            provider = plugin.context.get_provider_by_id(pid)
        if provider is None:
            provider = await plugin.context.get_using_provider_async(umo)
    except Exception as exc:
        logger.warning('[dafeiyu_knowledge] 取模型失败: %s', exc)
        return None
    if provider is None:
        return None
    prompt = str(_RUNTIME.get('prompt') or '').strip() or SUMMARIZE_PROMPT
    header = '群：%s\n聊天记录：\n' % (name or umo)
    timeout = max(30, _as_int(_RUNTIME.get('summary_timeout_seconds'), 180))
    try:
        # 单群限时：卡住的群跳过，别把整晚的整理拖死
        resp = await asyncio.wait_for(
            provider.text_chat(
                prompt=header + material,
                system_prompt=prompt,
            ),
            timeout=float(timeout),
        )
    except asyncio.TimeoutError:
        logger.warning('[dafeiyu_knowledge] 总结群聊超时（%ss）跳过 %s', timeout, umo)
        return None
    except Exception as exc:
        logger.warning('[dafeiyu_knowledge] 总结群聊失败 %s: %s', umo, exc)
        return None
    text = ''
    try:
        text = str(getattr(resp, 'completion_text', '') or '')
    except Exception:
        text = ''
    data = _parse_json(text)
    if data is None:
        logger.info('[dafeiyu_knowledge] 模型输出不是 JSON，跳过: %s', umo)
    return data


def _rotate_umos(umos: list[str], max_groups: int) -> list[str]:
    """从上次停下的位置开始取 max_groups 个群（越界取模）。

    群数超过 max_groups 时轮着来，后面的群不会被永远跳过；失败也不影响整理。
    """
    total = len(umos)
    if total <= max_groups:
        return list(umos)
    try:
        offset = _as_int(_STATE.get('cursor'), 0) % total
        picked = umos[offset:offset + max_groups]
        if len(picked) < max_groups:
            picked = picked + umos[:max_groups - len(picked)]
        _STATE['cursor'] = (offset + len(picked)) % total
    except Exception:
        return umos[:max_groups]
    return picked


async def run_nightly(plugin: Any, force: bool = False) -> dict:
    """把每个群最近的聊天总结进知识库。"""
    if not bool(_RUNTIME.get('enabled')):
        return {'ok': False, 'reason': 'disabled', 'groups': 0}
    speaker = _speaker_module()
    umos: list[str] = []
    if speaker is not None:
        try:
            for umo, items in (getattr(speaker, '_LOG', {}) or {}).items():
                if ':GroupMessage:' in str(umo) and items:
                    umos.append(str(umo))
        except Exception:
            pass
    for umo in list(_STATE['groups'].keys()):
        if umo not in umos:
            umos.append(umo)
    bot_filter = [str(x) for x in (_SECTION.get('bot_filter') or []) if str(x).strip()] if isinstance(_SECTION.get('bot_filter'), list) else []
    if bot_filter:
        umos = [u for u in umos if any(k in u for k in bot_filter)]

    limit = max(20, _as_int(_RUNTIME.get('max_lines'), 120))
    max_groups = max(1, _as_int(_RUNTIME.get('max_groups'), 20))
    updated = 0
    processed = 0
    # 排序后再轮转，保证 offset 每次都对得上同一批群
    for umo in _rotate_umos(sorted(set(umos)), max_groups):
        try:
            material, count = await _material(plugin, umo, limit)
            if count < 6 and not force:
                await asyncio.sleep(1.0)
                continue
            processed += 1
            name = await _group_name(plugin, umo)
            data = await _summarize(plugin, umo, name, material)
            if data is not None:
                entry = _merge(umo, name, data, count)
                updated += 1
                logger.info(
                    '[dafeiyu_knowledge] 群档案更新: %s 成员 %d / 黑话 %d / 话题 %d',
                    umo, len(entry['members']), len(entry['slang']), len(entry['topics']),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 一个群出错只跳过它，继续整理下一个群
            logger.warning('[dafeiyu_knowledge] 整理群 %s 失败，跳过: %s', umo, exc)
        await asyncio.sleep(1.0)

    _STATE['stats']['runs'] = _as_int(_STATE['stats'].get('runs'), 0) + 1
    _STATE['stats']['groups'] = len(_STATE['groups'])
    _prune()
    _save()
    _LAST_RUN.update({'at': _now_iso(), 'groups': updated, 'ok': True})
    logger.info('[dafeiyu_knowledge] 今晚整理完 %d 个群，更新 %d 个', processed, updated)
    return {'ok': True, 'processed': processed, 'updated': updated, 'groups': len(_STATE['groups'])}


def _prune() -> None:
    days = max(0, _as_int(_RUNTIME.get('retain_days'), 90))
    if not days:
        return
    cutoff = datetime.now(TZ) - timedelta(days=days)
    for umo in list(_STATE['groups'].keys()):
        entry = _STATE['groups'][umo]
        stamp = str(entry.get('updated_at') or '')
        try:
            dt = datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S').replace(tzinfo=TZ)
        except Exception:
            continue
        if dt < cutoff:
            _STATE['groups'].pop(umo, None)


def _group_block(umo: str) -> str:
    entry = _STATE['groups'].get(umo)
    if not isinstance(entry, dict):
        return ''
    name = str(entry.get('name') or '')
    members = list(entry.get('members') or [])[-max(1, _as_int(_RUNTIME.get('inject_max_members'), 12)):]
    slang = list(entry.get('slang') or [])[-max(1, _as_int(_RUNTIME.get('inject_max_slang'), 10)):]
    topics = list(entry.get('topics') or [])[-max(1, _as_int(_RUNTIME.get('inject_max_topics'), 5)):]
    if not (members or slang or topics):
        return ''
    lines = [MARK + '群：' + (name or umo)]
    if members:
        lines.append('群友称呼对照（左边是群昵称，右边是大家平时怎么叫 / 是谁）：')
        for item in members:
            alias = str(item.get('alias') or '').strip()
            note = str(item.get('note') or '').strip()
            right = '、'.join(x for x in (('大家叫「%s」' % alias) if alias else '', note) if x)
            lines.append('- %s → %s' % (item.get('nick'), right or '（暂时没有别的叫法）'))
    if slang:
        lines.append('群内黑话 / 梗：')
        for item in slang:
            lines.append('- %s：%s' % (item.get('term'), item.get('meaning')))
    if topics:
        lines.append('最近群里聊过：' + '；'.join(topics))
    lines.append(
        '这是你以前在这个群整理下来的档案：记录里出现这些昵称或外号时按对照理解，'
        '别人用外号叫你认识的群友别当陌生人；群里说黑话按上面的意思理解，不要望文生义。'
    )
    return '\n'.join(lines)


def _next_delay(hour: int, minute: int) -> float:
    now = datetime.now(TZ)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(30.0, (target - now).total_seconds())


async def _loop(plugin: Any) -> None:
    while True:
        delay = _next_delay(_RUNTIME['nightly_hour'], _RUNTIME['nightly_minute'])
        logger.info('[dafeiyu_knowledge] 下次整理群档案在 %.1f 小时后', delay / 3600)
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        try:
            if bool(_RUNTIME.get('enabled')) and bool(_RUNTIME.get('nightly_enabled')):
                await run_nightly(plugin)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning('[dafeiyu_knowledge] 每晚整理异常: %s', exc)


def start_background(plugin: Any) -> None:
    global _TASK
    if not bool(_RUNTIME.get('enabled')) or not bool(_RUNTIME.get('nightly_enabled')):
        logger.info('[dafeiyu_knowledge] 知识库每晚整理关着，不启动')
        return
    if _TASK is not None and not _TASK.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()
    _TASK = loop.create_task(_loop(plugin))
    logger.info('[dafeiyu_knowledge] 已启动群档案每晚整理（每天 %d:%02d）', _RUNTIME['nightly_hour'], _RUNTIME['nightly_minute'])


def stop_background() -> None:
    global _TASK
    if _TASK is not None and not _TASK.done():
        _TASK.cancel()
    _TASK = None


def status() -> dict:
    return {
        'enabled': bool(_RUNTIME.get('enabled')),
        'nightly_enabled': bool(_RUNTIME.get('nightly_enabled')),
        'hour': _RUNTIME.get('nightly_hour'),
        'minute': _RUNTIME.get('nightly_minute'),
        'groups': len(_STATE['groups']),
        'updated_at': _STATE.get('updated_at') or '',
        'last_run': dict(_LAST_RUN),
        'stats': dict(_STATE.get('stats') or {}),
        'members': sum(len(g.get('members') or []) for g in _STATE['groups'].values()),
        'slang': sum(len(g.get('slang') or []) for g in _STATE['groups'].values()),
    }


def _group_keys() -> list[str]:
    return sorted(_STATE['groups'].keys())


def _text_of_event(event: AstrMessageEvent) -> str:
    raw = str(getattr(event, 'message_str', '') or '').strip()
    for prefix in ('/群档案', '/群知识', '群档案', '群知识'):
        if raw.startswith(prefix):
            return raw[len(prefix):].strip()
    return raw


class KnowledgeFeature:

    @filter.on_llm_request(priority=5046)
    async def inject_knowledge(self, event: AstrMessageEvent, req) -> None:
        try:
            if not bool(_RUNTIME.get('enabled')) or not bool(_RUNTIME.get('inject_enabled')):
                return
            umo = str(event.unified_msg_origin or '')
            if ':GroupMessage:' not in umo:
                return
            block = _group_block(umo)
            if not block:
                return
            part = TextPart(text=block)
            try:
                part.mark_as_temp()
            except Exception:
                pass
            req.extra_user_content_parts.append(part)
            entry = _STATE['groups'].get(umo) or {}
            logger.info(
                '[dafeiyu_knowledge] 注入群档案: %s（成员 %d / 黑话 %d / 话题 %d）',
                umo, len(entry.get('members') or []), len(entry.get('slang') or []), len(entry.get('topics') or []),
            )
        except Exception as exc:
            logger.warning('[dafeiyu_knowledge] 注入群档案失败: %s', exc)

    @filter.command('群档案', alias={'群知识'})
    async def cmd_knowledge(self, event: AstrMessageEvent):
        try:
            if not bool(event.is_admin()):
                yield event.plain_result('群档案只给主人看。')
                return
            arg = _text_of_event(event)
            umo = str(event.unified_msg_origin or '')
            if arg in ('更新', '刷新', '整理'):
                if ':GroupMessage:' not in umo:
                    yield event.plain_result('这个要在一个群里发，我才能整理这个群的档案。')
                    return
                material, count = await _material(self, umo, max(20, _as_int(_RUNTIME.get('max_lines'), 120)))
                if count < 6:
                    yield event.plain_result('这个群最近聊天太少，没什么好整理的（至少 6 条）。')
                    return
                name = await _group_name(self, umo)
                data = await _summarize(self, umo, name, material)
                if data is None:
                    yield event.plain_result('这次没整理出来，等会儿再试试。')
                    return
                entry = _merge(umo, name, data, count)
                _save()
                yield event.plain_result(
                    '群档案更新好了：成员 %d 个、黑话 %d 条、最近话题 %d 条。'
                    % (len(entry['members']), len(entry['slang']), len(entry['topics']))
                )
                return
            if arg in ('全部', '所有', '列表'):
                if not _STATE['groups']:
                    yield event.plain_result('还没有群档案，晚上整理完就有了。')
                    return
                lines = ['🐋 群档案（共 %d 个群）' % len(_STATE['groups'])]
                for key in _group_keys():
                    entry = _STATE['groups'][key]
                    lines.append(
                        '· %s：成员 %d / 黑话 %d / 话题 %d（%s）'
                        % (entry.get('name') or key, len(entry.get('members') or []),
                           len(entry.get('slang') or []), len(entry.get('topics') or []),
                           entry.get('updated_at') or '还没更新')
                    )
                yield event.plain_result('\n'.join(lines))
                return
            block = _group_block(umo) if ':GroupMessage:' in umo else ''
            if not block:
                yield event.plain_result('这个群还没有档案。在群里发「/群档案 更新」可以马上整理一份。')
                return
            yield event.plain_result(block)
        except Exception as exc:
            logger.warning('[dafeiyu_knowledge] /群档案 出错: %s', exc)
            yield event.plain_result('查群档案的时候出错了。')
