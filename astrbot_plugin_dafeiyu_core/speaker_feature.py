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

import hashlib
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
from astrbot.core.message.components import Image
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

try:  # 协议端内置命令静默词表（例如 aiocqhttp 的 #sl）：这类消息不该进群聊发言记录
    from . import silent_guard
except Exception:
    silent_guard = None

try:  # 图片 / 收藏表情包 / 小黄脸 的区分（OneBot image 段里的 sub_type、summary、emoji_id）
    from . import media_kind
except Exception:
    media_kind = None

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
    'context_always': True,
    'image_caption_enabled': True,
    'image_caption_provider_id': '',
    'image_caption_prompt': '用一句中文说清这张图/表情包的内容（有文字就把文字也写出来），不超过60字。',
    'image_caption_max': 3,
    'image_caption_ttl_minutes': 1440,
    'image_direct_enabled': True,
    'image_direct_max': 3,
    'sticker_label_enabled': True,
    'sticker_direct_enabled': False,
    'face_label_enabled': True,
    'fresh_block_enabled': True,
    'fresh_lines': 4,
    'fresh_minutes': 15,
}

_RUNTIME: dict = dict(DEFAULTS)
MARK = '【群聊发言记录】'
MARK_MASTER = '【主人这句话可能没头没尾】'
MARK_FRESH = '【群里最新几条】'
_DEICTIC = ('这个', '那个', '刚才', '上面', '前面', '楼上', '它', '那', '这', '咋', '怎么', '为什么', '为啥', '啥意思', '什么意思', '你说呢', '你觉得')
_QUESTION = ('？', '?', '吗', '呢', '咋', '怎么', '为什么', '为啥', '啥', '多少', '谁', '哪', '是不是', '有没有')
# 带「回指」的说法：没有上下文根本看不出指的是哪件事/哪个人
_BACKREF = (
    '刚才', '刚刚', '刚说', '刚聊', '上面', '楼上', '前面', '之前', '上次', '他们', '她们',
    '他说', '她说', '那个', '这个', '这件事', '那件事', '继续', '接着', '然后呢', '还有呢',
    '你们在聊', '聊什么', '说什么', '你说呢', '你觉得', '你怎么看', '那你说',
)
# 主人这句像在催/追问上文的说法（说明白点、然后呢、啥意思之类）
_FOLLOWUP = (
    '说明白', '说清楚', '啥意思', '什么意思', '展开', '细说', '讲讲', '具体',
    '然后呢', '继续', '接着', '咋了', '怎么回事', '真的', '哪个', '谁啊', '为什么',
)
# 纯应答词不算「没头没尾」，免得每句「好的」都补一遍上下文
_BACKCHANNEL = (
    '好的', '好', '行', '嗯', '哦', '收到', '谢谢', '笑死', '哈哈', '草', '绷',
    '确实', '可以', '没事', '晚安', '睡了', '早', '来了', '在', '？', '?',
)

# 短句里出现这些，才当作「没头没尾」
_PRONOUN = ('他', '她', '它', '这', '那', '啥', '谁', '哪', '咋', '怎么', '为什么', '吗', '什么')
_EXTRA_RECORDED = 'dafeiyu_speaker_recorded'
_LOG: dict[str, list[dict[str, Any]]] = {}
_STATE_PATH: Path | None = None
_LAST_SAVE = 0.0
# 群里别人的图片引用（常是 base64://）只在内存里留最近这点，太长的丢掉，免得吃内存/撑爆记录文件
_REF_MAX_CHARS = 600_000


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
            'fresh_lines',
            'fresh_minutes',
        ):
            _RUNTIME[key] = max(1, _as_int(value, int(DEFAULTS[key])))
        elif key in ('image_caption_max', 'image_caption_ttl_minutes', 'image_direct_max'):
            _RUNTIME[key] = max(0, _as_int(value, int(DEFAULTS[key])))
        elif key in ('image_caption_provider_id', 'image_caption_prompt'):
            _RUNTIME[key] = str(value or '').strip()
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


def _strip_refs(log: dict) -> dict:
    """落盘时去掉图片引用：base64 太大，重启后旧图也没必要留着。"""
    out: dict = {}
    for umo, items in log.items():
        out[umo] = [
            {k: v for k, v in entry.items() if k != 'i'}
            for entry in items
            if isinstance(entry, dict)
        ]
    return out


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
            'groups': _strip_refs(_LOG),
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
        if silent_guard is not None and silent_guard.is_silent(event):
            return
        umo = getattr(event, 'unified_msg_origin', '') or ''
        if not umo or not _bot_match(umo):
            return
        body = (text if text is not None else (event.message_str or '')).strip()
        raw_infos: list[dict[str, Any]] = []
        faces: list[str] = []
        if text is None:
            if media_kind is not None:
                raw_infos = media_kind.image_infos(event)
                if _RUNTIME.get('face_label_enabled', True):
                    faces = media_kind.face_names(event)
            else:
                raw_infos = [{'ref': ref, 'kind': 'photo', 'name': ''} for ref in _image_refs(event)]
            raw_infos = [info for info in raw_infos if len(str(info.get('ref') or '')) <= _REF_MAX_CHARS]
        label = ''
        if media_kind is not None and (raw_infos or faces):
            label = media_kind.compose_label(
                raw_infos,
                faces,
                bool(_RUNTIME.get('sticker_label_enabled', True)),
                bool(_RUNTIME.get('face_label_enabled', True)),
            )
        if not body and not raw_infos and not faces:
            return
        if not body:
            body = label or '[图片]'
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
        if label and body != label:
            room = max(5, max_chars - len(label) - 1)
            body = (body.replace('\n', ' ').strip()[:room] + ' ' + label).strip()
        else:
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
            'i': [str(info.get('ref') or '') for info in raw_infos],
            'q': [str(info.get('kind') or 'photo') for info in raw_infos],
            'sn': [str(info.get('name') or '') for info in raw_infos],
            'fe': list(faces),
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
    stamp = _fmt_time(float(entry.get('t') or 0))
    body = entry.get('x') or ''
    if _RUNTIME.get('show_master_mark', True) and entry.get('m'):
        # 主人的群昵称不露给她，统一标成「主人」，免得她跟着叫真名/群昵称。
        return '- %s 主人：%s' % (stamp, body)
    return '- %s %s（群友）：%s' % (stamp, name, body)


_CAPTION_CACHE: dict[str, tuple[float, str]] = {}


def _image_refs(event: AstrMessageEvent) -> list[str]:
    """取出这条消息里的图片/表情引用（本地路径优先，其次 url / file）。"""
    refs: list[str] = []
    try:
        for comp in event.get_messages():
            if not isinstance(comp, Image):
                continue
            ref = (
                str(getattr(comp, 'path', '') or '')
                or str(getattr(comp, 'url', '') or '')
                or str(getattr(comp, 'file', '') or '')
            )
            if ref:
                refs.append(ref)
    except Exception:
        return refs
    return refs[:2]


async def _caption_image(plugin, umo: str, ref: str) -> str:
    """让视觉模型看一眼这张图/表情，返回一句描述（带缓存）。"""
    ttl = _as_int(_RUNTIME.get('image_caption_ttl_minutes'), 1440) * 60
    key = hashlib.md5(ref.encode('utf-8', 'ignore')).hexdigest()
    hit = _CAPTION_CACHE.get(key)
    if hit and _now() - hit[0] < ttl:
        return hit[1]
    provider = None
    pid = str(_RUNTIME.get('image_caption_provider_id') or '').strip()
    try:
        if pid:
            provider = plugin.context.get_provider_by_id(pid)
        if provider is None:
            provider = plugin.context.get_using_provider(umo)
    except Exception as exc:
        logger.debug('[dafeiyu_speaker] 取图片描述模型失败: %s', exc)
        provider = None
    if provider is None:
        return ''
    prompt = str(_RUNTIME.get('image_caption_prompt') or '').strip() or '用一句中文说清这张图的内容。'
    try:
        resp = await provider.text_chat(prompt=prompt, image_urls=[ref])
    except Exception as exc:
        logger.info('[dafeiyu_speaker] 图片没看懂: %s', exc)
        return ''
    text = str(getattr(resp, 'completion_text', '') or '').strip()
    text = ' '.join(text.split())[:120]
    if text:
        _CAPTION_CACHE[key] = (_now(), text)
    return text


def _provider_sees_image(plugin, umo: str) -> bool:
    """主模型自己能不能看图（provider 的 modalities 里有 image）。"""
    try:
        provider = plugin.context.get_using_provider(umo)
    except Exception:
        provider = None
    if provider is None:
        return False
    try:
        modalities = provider.provider_config.get('modalities', None)
    except Exception:
        return False
    if modalities == []:
        return True  # 框架把空列表当成「没配」，向后兼容为支持
    return isinstance(modalities, (list, tuple)) and 'image' in modalities


async def _entry_line(plugin, umo: str, entry: dict[str, Any], budget: list[int], direct_refs: list[str] | None = None) -> str:
    """一条发言 + 它带的图片。

    direct_refs 不是 None 时走「把原图直接塞给多模态主模型」：引用收进 direct_refs，
    行尾只留 ［图N］ 编号；否则退回「先让视觉模型转述成一句话」。
    """
    line = _line_of(entry)
    refs = [str(ref) for ref in (entry.get('i') or [])]
    if not refs:
        return line
    kinds = entry.get('q') or []
    names = entry.get('sn') or []
    sticker_direct = bool(_RUNTIME.get('sticker_direct_enabled', False))

    def _kind_at(index: int) -> str:
        return str(kinds[index]) if index < len(kinds) else 'photo'

    def _name_at(index: int) -> str:
        return str(names[index]) if index < len(names) else ''

    def _skip(index: int) -> bool:
        # 收藏表情包有名字就只按名字理解，不占图片额度（省 token，也免得她当照片解说）
        return _kind_at(index) == 'sticker' and not sticker_direct and bool(_name_at(index))

    if direct_refs is not None:
        if not _RUNTIME.get('image_direct_enabled', True):
            return line
        marks: list[str] = []
        for index, ref in enumerate(refs[:2]):
            if budget[0] <= 0:
                break
            if _skip(index):
                continue
            budget[0] -= 1
            direct_refs.append(str(ref))
            marks.append('［图%d］' % len(direct_refs))
        if marks:
            line += ' ' + ' '.join(marks)
        return line
    if not _RUNTIME.get('image_caption_enabled', True):
        return line
    caps: list[str] = []
    for index, ref in enumerate(refs[:2]):
        if budget[0] <= 0:
            break
        if _skip(index):
            continue
        budget[0] -= 1
        cap = await _caption_image(plugin, umo, str(ref))
        caps.append(cap or '（图没看清）')
    if caps:
        line += '［图：%s］' % '／'.join(caps)
    return line


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


async def fresh_block(event: AstrMessageEvent, plugin) -> str:
    """群聊每次回话都带上「现在几点 + 群里最新几条」，免得她接已经过时的旧话题（慢半拍）。"""
    try:
        if not _RUNTIME.get('fresh_block_enabled', True):
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
        now = _now()
        window = _as_int(_RUNTIME.get('fresh_minutes'), 15) * 60
        lines_n = _as_int(_RUNTIME.get('fresh_lines'), 4)
        recent = [entry for entry in items if now - float(entry.get('t') or 0) <= window]
        if not recent:
            return ''
        picked = recent[-lines_n:]
        sid = _sender_id(event)
        current = (event.message_str or '').strip().replace(chr(10), ' ')[: _as_int(_RUNTIME.get('max_chars'), 40)]
        trigger_t = 0.0
        for entry in reversed(recent):
            if entry.get('s') == (sid[-6:] if sid else '') and (entry.get('x') or '') == current:
                trigger_t = float(entry.get('t') or 0)
                break
        lines = [MARK_FRESH + '现在是 %s。下面是群里最新的几条（含你没回的，按时间从旧到新）：' % _fmt_time(now)]
        for entry in picked:
            lines.append(_line_of(entry))
        if trigger_t and now - trigger_t >= 5:
            lines.append(
                '你要回的那条「%s」是 %s 发的，已经过去 %d 秒；这期间群里又说了上面这些，回话以最新的为准，别拿更早的旧话题套上。'
                % (current[:30], _fmt_time(trigger_t), int(now - trigger_t))
            )
        else:
            lines.append('回话接最新这几条；要是最新几条已经换了话题，就跟着换，别拿旧话题硬套。')
        return chr(10).join(lines)
    except Exception as exc:
        logger.debug('[dafeiyu_speaker] 拼最新消息块失败: %s', exc)
        return ''


async def digest_block(event: AstrMessageEvent, plugin) -> tuple[str, list[str]]:
    """拼出「最近的群聊发言」临时块（排除本轮这条，它已经作为用户消息进了上下文）。

    返回 (文本, 需要直接喂给多模态主模型的图片引用列表)。
    """
    try:
        if not _RUNTIME.get('enabled', True):
            return '', []
        umo = getattr(event, 'unified_msg_origin', '') or ''
        if not umo or not _bot_match(umo):
            return '', []
        try:
            group_id = str(event.get_group_id() or '')
        except Exception:
            group_id = ''
        if _RUNTIME.get('only_group', True) and not group_id and ':GroupMessage:' not in umo:
            return '', []
        items = _LOG.get(umo) or []
        if not items:
            return '', []
        if _is_master(event):
            # 主人的「没头没尾」交给 master_context_block 单独补，避免补两遍。
            return '', []
        if (
            _RUNTIME.get('context_on_demand', True)
            and not _RUNTIME.get('context_always', True)
            and not _needs_context(event.message_str or '')
        ):
            logger.info('[dafeiyu_speaker] 这句能看懂，不读群聊上下文: %s', umo)
            return '', []
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
            return '', []
        picked.reverse()
        direct = bool(_RUNTIME.get('image_direct_enabled', True)) and _provider_sees_image(plugin, umo)
        budget = [
            _as_int(_RUNTIME.get('image_direct_max') if direct else _RUNTIME.get('image_caption_max'),
                    _as_int(DEFAULTS['image_direct_max'] if direct else DEFAULTS['image_caption_max'], 3))
        ]
        direct_refs: list[str] = []
        lines = [MARK + '这几条群消息你都在场（按时间先后，含你没回的）：']
        for entry in picked:
            lines.append(await _entry_line(plugin, umo, entry, budget, direct_refs if direct else None))
        lines.append('回答时按这个记录分清是谁说的、在跟谁说话；每个人名字照记录写，别把两个人的话搞混。')
        if media_kind is not None and _RUNTIME.get('sticker_label_enabled', True):
            lines.append(
                '记录里 [图片] 是真人拍的照片/截图；[表情包] 或 [表情包：名字] 是别人发的收藏表情包；'
                '[表情：名字] 是 QQ 自带小黄脸。表情包别当照片一样解说里面画了什么，按它的情绪接一句就行。'
            )
        if direct_refs:
            lines.append('记录里的［图1］［图2］是别人发的原图，按编号顺序附在这条消息的图片里，你直接看图理解，别只看文字猜。')
        else:
            lines.append('记录里带［图：…］的是别人发的图片或表情包，按那个内容理解；先看清这句本身在问什么，别拿之前的话题硬套。')
        logger.info('[dafeiyu_speaker] 这句看不懂，补了 %s 条群聊上下文: %s', len(picked), umo)
        if direct_refs:
            logger.info('[dafeiyu_speaker] 直接把 %s 张群图给主模型看: %s', len(direct_refs), umo)
        return '\n'.join(lines), direct_refs
    except Exception as exc:
        logger.debug('[dafeiyu_speaker] 生成发言记录失败: %s', exc)
        return '', []


async def master_context_block(event: AstrMessageEvent, plugin) -> tuple[str, list[str]]:
    """主人这句可能没头没尾时，把刚才别人在聊的补给她，让她自己猜主人在说哪件事。

    判定（只群聊、只主人、非命令）：短句 <= master_context_short_chars，或含指代/疑问词且 <=20 字，
    或问句 <=16 字；并且最近 master_context_minutes 分钟里主人没自己聊过别的。
    返回 (文本, 需要直接喂给多模态主模型的图片引用列表)。
    """
    try:
        if not _RUNTIME.get('master_context_enabled', True):
            return '', []
        if not _is_master(event):
            return '', []
        umo = getattr(event, 'unified_msg_origin', '') or ''
        if not umo or not _bot_match(umo):
            return '', []
        try:
            group_id = str(event.get_group_id() or '')
        except Exception:
            group_id = ''
        if not group_id and ':GroupMessage:' not in umo:
            return '', []
        text = (event.message_str or '').strip()
        if not text or text.startswith('/') or len(text) > 40:
            return '', []
        max_chars = _as_int(_RUNTIME.get('max_chars'), 40)
        short_chars = _as_int(_RUNTIME.get('master_context_short_chars'), 8)
        short = len(text) <= short_chars and any(word in text for word in _QUESTION + _PRONOUN)
        deictic = len(text) <= 20 and any(word in text for word in _DEICTIC)
        followup = any(word in text for word in _FOLLOWUP)
        tiny = len(text) <= 6 and not any(word in text for word in _BACKCHANNEL)
        if not (short or deictic or followup or tiny):
            return '', []
        items = _LOG.get(umo) or []
        if not items:
            return '', []
        now = _now()
        window = _as_int(_RUNTIME.get('master_context_minutes'), 30) * 60
        sid = _sender_id(event)
        mine = sid[-6:] if sid else ''
        current = text.replace('\n', ' ')[:max_chars]
        recent = [entry for entry in items if now - float(entry.get('t') or 0) <= window]
        if not recent:
            return '', []
        own_talk = [
            entry
            for entry in recent
            if entry.get('m') and entry.get('s') == mine and (entry.get('x') or '') != current
        ]
        if own_talk:
            return '', []
        others = [
            entry
            for entry in recent
            if not entry.get('m') and not (entry.get('s') == mine and (entry.get('x') or '') == current)
        ]
        if not others:
            return '', []
        limit = _as_int(_RUNTIME.get('master_context_lines'), 12)
        picked = others[-limit:]
        direct = bool(_RUNTIME.get('image_direct_enabled', True)) and _provider_sees_image(plugin, umo)
        budget = [
            _as_int(_RUNTIME.get('image_direct_max') if direct else _RUNTIME.get('image_caption_max'),
                    _as_int(DEFAULTS['image_direct_max'] if direct else DEFAULTS['image_caption_max'], 3))
        ]
        direct_refs: list[str] = []
        lines = [
            MARK_MASTER + '主人这句像是接着刚才的聊天说的，但只看到这一句，看不出指的是哪件事/哪个人。',
            '刚才群里在聊：',
        ]
        for entry in picked:
            lines.append(await _entry_line(plugin, umo, entry, budget, direct_refs if direct else None))
        lines.append('主人刚说：「%s」' % current)
        if direct_refs:
            lines.append('记录里的［图1］［图2］是别人发的原图，按编号顺序附在这条消息的图片里，你直接看图理解。')
        lines.append('先看上面别人在聊什么，猜主人指的是哪件事、哪个人，再顺着接；猜不准就直接说没看懂、让主人说清楚，别装懂、别编。')
        logger.info('[dafeiyu_speaker] 主人没头没尾，补了 %s 条上下文: %s', len(picked), umo)
        if direct_refs:
            logger.info('[dafeiyu_speaker] 直接把 %s 张群图给主模型看: %s', len(direct_refs), umo)
        return '\n'.join(lines), direct_refs
    except Exception as exc:
        logger.debug('[dafeiyu_speaker] 生成主人上下文失败: %s', exc)
        return '', []


def _attach_images(req, refs: list[str]) -> None:
    """把别人发的原图挂到这次请求上，多模态主模型就能直接看图（不用先转述）。"""
    if not refs:
        return
    try:
        existing = list(getattr(req, 'image_urls', None) or [])
        merged = list(existing)
        for ref in refs:
            if ref and ref not in merged:
                merged.append(ref)
        req.image_urls = merged
    except Exception as exc:
        logger.debug('[dafeiyu_speaker] 附加群图失败: %s', exc)


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
            block, refs = await digest_block(event, self)
            if block:
                part = TextPart(text=block)
                try:
                    part.mark_as_temp()
                except Exception:
                    pass
                req.extra_user_content_parts.append(part)
                _attach_images(req, refs)
            if media_kind is not None:
                sn = media_kind.sticker_names(event, _RUNTIME.get('sticker_label_enabled', True))
                sn = [name for name in sn if name]
                if sn:
                    hint = TextPart(text=(
                        '对方这条带着表情包（%s），你正常接一句；想配个表情包就用 send_meme 发一张情绪对得上的，'
                        '别解说表情包里画了啥。' % '、'.join(sn[:2])
                    ))
                    try:
                        hint.mark_as_temp()
                    except Exception:
                        pass
                    req.extra_user_content_parts.append(hint)
            fresh = await fresh_block(event, self)
            if fresh:
                fpart = TextPart(text=fresh)
                try:
                    fpart.mark_as_temp()
                except Exception:
                    pass
                req.extra_user_content_parts.append(fpart)
        except Exception as exc:
            logger.warning('[dafeiyu_speaker] 注入群聊发言记录失败: %s', exc)

    @filter.on_llm_request(priority=5043)
    async def inject_master_context(self, event: AstrMessageEvent, req):
        """主人说话没头没尾时，把刚才别人在聊的补上，让她自己猜主人指什么。"""
        try:
            record(event)
            block, refs = await master_context_block(event, self)
            if not block:
                return
            part = TextPart(text=block)
            try:
                part.mark_as_temp()
            except Exception:
                pass
            req.extra_user_content_parts.append(part)
            _attach_images(req, refs)
        except Exception as exc:
            logger.warning('[dafeiyu_speaker] 注入主人上下文失败: %s', exc)

