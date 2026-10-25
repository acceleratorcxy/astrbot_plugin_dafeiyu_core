"""按会话算钱（cost 功能模块）

用户需求（m07184）：
- 「能不能给每个私聊都单独加上费用计算」

做法（不改 AstrBot 源码）：
- AstrBot 每次内部 agent 跑完会往 provider_stats 表写一条统计：
  umo / conversation_id / provider_model / token_input_other / token_input_cached /
  token_output / created_at（见 astrbot/core/pipeline/process_stage/method/agent_sub_stages/internal.py
  的 _record_internal_agent_stats + core/db/sqlite.py insert_provider_stat）。
- 这里直接读这张表（只读），按 umo 汇总 token，再按配置的牌价折成钱。
- 牌价可配（默认按 DeepSeek 官方 V3.2 之后的价：输入缓存未命中 2 元、缓存命中 0.2 元、
  输出 3 元 / 百万 token），换模型/涨价时在插件配置里改就行。
- 不写历史、不打扰对话；页面「上下文」tab 和 /费用、每晚 0 点报告都会用。
"""
from __future__ import annotations

import importlib
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from astrbot.api import logger

TZ = timezone(timedelta(hours=8))


def _context_module():
    """拿 core_context 模块（用来判断一个会话属于哪个配置档案）。"""
    pkg = __package__ or "data.plugins.astrbot_plugin_dafeiyu_core"
    for name in ("%s.core_context" % pkg, "core_context"):
        try:
            return importlib.import_module(name)
        except Exception:
            continue
    return None


def _own_umo_filter(plugin):
    """返回一个判断 umo 是否属于鲸鲸自己档案的函数（拿不到就返回 None）。"""
    module = _context_module()
    if module is None:
        return None
    try:
        own = module.own_profile_ids(plugin)
    except Exception:
        return None
    if not own:
        return None

    def _belongs(umo: str) -> bool:
        try:
            return module._profile_of_umo(plugin, umo) in own
        except Exception:
            return False

    return _belongs

DEFAULTS = {
    'enabled': True,
    'currency': 'CNY',
    'input_price': 2.0,
    'cached_input_price': 0.2,
    'output_price': 3.0,
    # 峰谷计价（默认关：还是要手动勾选才用峰谷价，价格口径是 DeepSeek 官方 2026 峰谷价）
    'peak_valley_enabled': False,
    'peak_input_price': 2.0,
    'peak_cached_input_price': 0.04,
    'peak_output_price': 8.0,
    'offpeak_input_price': 1.0,
    'offpeak_cached_input_price': 0.02,
    'offpeak_output_price': 4.0,
    'peak_ranges': '09:00-12:00,14:00-18:00',
    'peak_weekdays_only': True,
    'peak_holidays': '',
    'report_top': 8,
    'include_group': True,
    'budget_monthly': 0.0,
}

_RUNTIME: dict = dict(DEFAULTS)


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
        if key in ('currency',):
            _RUNTIME[key] = str(value or 'CNY').strip().upper() or 'CNY'
        elif key in (
            'input_price', 'cached_input_price', 'output_price', 'budget_monthly',
            'peak_input_price', 'peak_cached_input_price', 'peak_output_price',
            'offpeak_input_price', 'offpeak_cached_input_price', 'offpeak_output_price',
        ):
            _RUNTIME[key] = max(0.0, _as_float(value, float(DEFAULTS[key])))
        elif key in ('peak_ranges', 'peak_holidays'):
            _RUNTIME[key] = str(value or DEFAULTS[key]).strip()
        elif key == 'report_top':
            _RUNTIME[key] = max(1, _as_int(value, int(DEFAULTS[key])))
        else:
            _RUNTIME[key] = bool(value)


def _symbol() -> str:
    currency = str(_RUNTIME.get('currency') or 'CNY').upper()
    return {'CNY': '¥', 'USD': '$', 'EUR': '€'}.get(currency, currency + ' ')


def _cost(input_other: int, input_cached: int, output: int) -> float:
    return (
        max(0, input_other) / 1_000_000.0 * _as_float(_RUNTIME.get('input_price'), 2.0)
        + max(0, input_cached) / 1_000_000.0 * _as_float(_RUNTIME.get('cached_input_price'), 0.2)
        + max(0, output) / 1_000_000.0 * _as_float(_RUNTIME.get('output_price'), 3.0)
    )


def _parse_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    raw = str(text or '').replace('；', ',').replace(';', ',').replace('，', ',')
    for part in raw.split(','):
        part = part.strip()
        if not part or '-' not in part:
            continue
        left, right = part.split('-', 1)

        def _to_min(value: str) -> int | None:
            bits = value.strip().split(':')
            try:
                hour = int(bits[0])
                minute = int(bits[1]) if len(bits) > 1 else 0
            except (TypeError, ValueError):
                return None
            if hour < 0 or hour > 24 or minute < 0 or minute > 59:
                return None
            return hour * 60 + minute

        start = _to_min(left)
        end = _to_min(right)
        if start is None or end is None or end <= start:
            continue
        ranges.append((start, end))
    return ranges or [(9 * 60, 12 * 60), (14 * 60, 18 * 60)]


def _peak_ranges() -> list[tuple[int, int]]:
    return _parse_ranges(_RUNTIME.get('peak_ranges') or '')


def _peak_ranges_text() -> str:
    return ', '.join(
        '%02d:%02d-%02d:%02d' % (start // 60, start % 60, end // 60, end % 60)
        for start, end in _peak_ranges()
    )


def _parse_holidays(text: str) -> set[str]:
    out: set[str] = set()
    raw = str(text or '').replace('；', ',').replace(';', ',').replace('，', ',').replace('/', '-')
    for part in raw.split(','):
        part = part.strip()
        if len(part) >= 8:
            out.add(part[:10])
    return out


def _local_dt(value) -> datetime | None:
    """provider_stats.created_at 存的是 UTC 的 naive 时间，这里统一转成北京时间。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace('T', ' ')
        dt = None
        for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
            try:
                dt = datetime.strptime(text[:26], fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


def is_peak(when=None) -> bool:
    """这条用量是不是落在高峰时段（北京时间工作日 9-12 / 14-18，不含周末与法定节假日）。"""
    if not _RUNTIME.get('peak_valley_enabled'):
        return False
    dt = _local_dt(when)
    if dt is None:
        return False
    if _RUNTIME.get('peak_weekdays_only', True) and dt.weekday() >= 5:
        return False
    if dt.strftime('%Y-%m-%d') in _parse_holidays(_RUNTIME.get('peak_holidays') or ''):
        return False
    minute = dt.hour * 60 + dt.minute
    return any(start <= minute < end for start, end in _peak_ranges())


def _price_triple(peak: bool) -> tuple[float, float, float]:
    if not _RUNTIME.get('peak_valley_enabled'):
        return (
            _as_float(_RUNTIME.get('input_price'), 2.0),
            _as_float(_RUNTIME.get('cached_input_price'), 0.2),
            _as_float(_RUNTIME.get('output_price'), 3.0),
        )
    prefix = 'peak' if peak else 'offpeak'
    return (
        _as_float(_RUNTIME.get(prefix + '_input_price'), 2.0 if peak else 1.0),
        _as_float(_RUNTIME.get(prefix + '_cached_input_price'), 0.04 if peak else 0.02),
        _as_float(_RUNTIME.get(prefix + '_output_price'), 8.0 if peak else 4.0),
    )


def _cost_at(input_other: int, input_cached: int, output: int, when=None, peak: bool | None = None) -> float:
    if peak is None:
        peak = is_peak(when)
    p_in, p_cached, p_out = _price_triple(peak)
    return (
        max(0, input_other) / 1_000_000.0 * p_in
        + max(0, input_cached) / 1_000_000.0 * p_cached
        + max(0, output) / 1_000_000.0 * p_out
    )


def format_money(value: float) -> str:
    value = max(0.0, float(value or 0.0))
    if value >= 100:
        body = '%.1f' % value
    elif value >= 1:
        body = '%.2f' % value
    else:
        body = '%.4f' % value
    return _symbol() + body


def _fmt_tokens(value: int) -> str:
    value = max(0, int(value or 0))
    if value >= 1_000_000:
        return '%.2fM' % (value / 1_000_000.0)
    if value >= 1_000:
        return '%.1fk' % (value / 1_000.0)
    return str(value)


def _fmt_time(value) -> str:
    if value is None:
        return '--'
    text = str(value)
    if len(text) >= 16:
        return text[5:16].replace('T', ' ')
    return text


def label_of_umo(umo: str) -> str:
    parts = str(umo or '').split(':')
    platform = parts[0] if parts else ''
    if len(parts) >= 3:
        kind_raw = parts[1]
        target = ':'.join(parts[2:])
        if kind_raw == 'GroupMessage':
            scene = '群聊'
        elif kind_raw in ('FriendMessage', 'PrivateMessage'):
            scene = '私聊'
        else:
            scene = kind_raw or '会话'
        target = target.strip()
        if len(target) > 16:
            target = target[:16] + '…'
        return '%s %s %s' % (platform, scene, target)
    return str(umo or '未知会话')


def _kind_of(umo: str) -> str:
    return 'group' if ':GroupMessage:' in str(umo or '') else 'private'


def _conn(plugin):
    db = plugin.context.get_db()
    return db


async def _query(plugin, sql: str, params: dict | None = None) -> list[Any]:
    from sqlalchemy import text

    db = _conn(plugin)
    async with db.get_db() as session:
        result = await session.execute(text(sql), params or {})
        return list(result.fetchall())


async def overview(plugin, own_only: bool = False) -> dict:
    """按会话汇总 token 与费用（读 provider_stats，逐条按发生时刻算峰谷价）。

    own_only=True 时只统计鲸鲸自己配置档案的会话（别的档案的机器人不算进来）。
    """
    base_prices = {
        'input': _as_float(_RUNTIME.get('input_price'), 2.0),
        'cached_input': _as_float(_RUNTIME.get('cached_input_price'), 0.2),
        'output': _as_float(_RUNTIME.get('output_price'), 3.0),
    }
    peak_prices = {
        'input': _as_float(_RUNTIME.get('peak_input_price'), 2.0),
        'cached_input': _as_float(_RUNTIME.get('peak_cached_input_price'), 0.04),
        'output': _as_float(_RUNTIME.get('peak_output_price'), 8.0),
    }
    offpeak_prices = {
        'input': _as_float(_RUNTIME.get('offpeak_input_price'), 1.0),
        'cached_input': _as_float(_RUNTIME.get('offpeak_cached_input_price'), 0.02),
        'output': _as_float(_RUNTIME.get('offpeak_output_price'), 4.0),
    }
    data: dict[str, Any] = {
        'enabled': bool(_RUNTIME.get('enabled', True)),
        'currency': _RUNTIME.get('currency') or 'CNY',
        'prices': base_prices,
        'peak_valley': {
            'enabled': bool(_RUNTIME.get('peak_valley_enabled')),
            'peak_prices': peak_prices,
            'offpeak_prices': offpeak_prices,
            'ranges': _peak_ranges_text(),
            'weekdays_only': bool(_RUNTIME.get('peak_weekdays_only', True)),
            'holidays': sorted(_parse_holidays(_RUNTIME.get('peak_holidays') or '')),
            'peak_requests': 0,
            'offpeak_requests': 0,
            'peak_cost': 0.0,
            'offpeak_cost': 0.0,
            'peak_tokens': 0,
            'offpeak_tokens': 0,
        },
        'totals': {},
        'today': {},
        'month': {},
        'rows': [],
        'generated_at': datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S'),
        'budget_monthly': _as_float(_RUNTIME.get('budget_monthly'), 0.0),
        'error': '',
    }
    if not data['enabled']:
        return data
    try:
        raw_rows = await _query(
            plugin,
            'SELECT umo, provider_model, CAST(created_at AS TEXT) AS ts, '
            'token_input_other, token_input_cached, token_output FROM provider_stats',
        )
    except Exception as exc:
        logger.warning('[dafeiyu_cost] 读取费用统计失败: %s', exc)
        data['error'] = str(exc)
        return data

    if own_only:
        belongs = _own_umo_filter(plugin)
        if belongs is not None:
            raw_rows = [row for row in raw_rows if belongs(str(row[0] or ''))]

    now = datetime.now(TZ)
    today_key = now.strftime('%Y-%m-%d')
    month_key = now.strftime('%Y-%m')
    merged: dict[str, dict[str, Any]] = {}
    totals = {
        'requests': 0, 'input_other': 0, 'input_cached': 0, 'output': 0, 'tokens': 0, 'cost': 0.0,
        'peak_requests': 0, 'offpeak_requests': 0, 'peak_cost': 0.0, 'offpeak_cost': 0.0,
    }
    today = {'requests': 0, 'input_other': 0, 'input_cached': 0, 'output': 0, 'tokens': 0, 'cost': 0.0}
    month = {'requests': 0, 'input_other': 0, 'input_cached': 0, 'output': 0, 'tokens': 0, 'cost': 0.0}
    pv = data['peak_valley']
    for umo, model, ts, io, ic, out in raw_rows:
        umo = str(umo or '')
        if not umo:
            continue
        io = int(io or 0)
        ic = int(ic or 0)
        out = int(out or 0)
        dt = _local_dt(ts)
        peak = is_peak(dt)
        cost = _cost_at(io, ic, out, peak=peak)
        tokens = io + ic + out
        item = merged.setdefault(
            umo,
            {
                'umo': umo,
                'label': label_of_umo(umo),
                'kind': _kind_of(umo),
                'models': [],
                'requests': 0,
                'input_other': 0,
                'input_cached': 0,
                'output': 0,
                'last_at': '',
                'peak_requests': 0,
                'offpeak_requests': 0,
                'peak_cost': 0.0,
                'offpeak_cost': 0.0,
            },
        )
        model_text = str(model or '')
        if model_text and model_text not in item['models']:
            item['models'].append(model_text)
        item['requests'] += 1
        item['input_other'] += io
        item['input_cached'] += ic
        item['output'] += out
        if str(ts or '') > str(item['last_at'] or ''):
            item['last_at'] = ts
        bucket = 'peak' if peak else 'offpeak'
        item[bucket + '_requests'] += 1
        item[bucket + '_cost'] += cost
        pv[bucket + '_requests'] += 1
        pv[bucket + '_cost'] += cost
        pv[bucket + '_tokens'] += tokens
        for target, hit in (
            (totals, True),
            (today, dt is not None and dt.strftime('%Y-%m-%d') == today_key),
            (month, dt is not None and dt.strftime('%Y-%m') == month_key),
        ):
            if not hit:
                continue
            target['requests'] += 1
            target['input_other'] += io
            target['input_cached'] += ic
            target['output'] += out
            target['tokens'] += tokens
            target['cost'] += cost
        if peak:
            totals['peak_requests'] += 1
            totals['peak_cost'] += cost
        else:
            totals['offpeak_requests'] += 1
            totals['offpeak_cost'] += cost

    rows = []
    for item in merged.values():
        tokens = item['input_other'] + item['input_cached'] + item['output']
        cost = item['peak_cost'] + item['offpeak_cost']
        item['tokens'] = tokens
        item['cost'] = cost
        item['cost_text'] = format_money(cost)
        item['model'] = '、'.join(item['models']) or '未知模型'
        item['last_at_text'] = _fmt_time(item.get('last_at'))
        item['tokens_text'] = _fmt_tokens(tokens)
        rows.append(item)
    rows.sort(key=lambda row: row['cost'], reverse=True)
    if not _RUNTIME.get('include_group', True):
        rows = [row for row in rows if row['kind'] == 'private']
    ctx_module = _context_module()
    if ctx_module is not None and hasattr(ctx_module, 'umo_names'):
        try:
            names = await ctx_module.umo_names(plugin, [row['umo'] for row in rows])
            for row in rows:
                row['name'] = names.get(row['umo'], '')
                row['label'] = ctx_module._pretty_label(row['umo'], row['name'])
        except Exception as exc:
            logger.warning('[dafeiyu_cost] 取会话名失败: %s', exc)

    def _pack(bucket: dict) -> dict:
        return {
            'requests': bucket['requests'],
            'input_other': bucket['input_other'],
            'input_cached': bucket['input_cached'],
            'output': bucket['output'],
            'tokens': bucket['tokens'],
            'cost': bucket['cost'],
            'cost_text': format_money(bucket['cost']),
            'tokens_text': _fmt_tokens(bucket['tokens']),
        }

    data['rows'] = rows
    data['totals'] = dict(totals, cost_text=format_money(totals['cost']), tokens_text=_fmt_tokens(totals['tokens']))
    data['today'] = _pack(today)
    data['month'] = _pack(month)
    data['private_count'] = sum(1 for row in rows if row['kind'] == 'private')
    data['group_count'] = sum(1 for row in rows if row['kind'] == 'group')
    budget = _as_float(_RUNTIME.get('budget_monthly'), 0.0)
    if budget > 0:
        data['budget'] = {
            'monthly': budget,
            'used': data['month']['cost'],
            'ratio': min(1.0, data['month']['cost'] / budget),
            'text': format_money(budget),
        }
    return data


def _num_text(value) -> str:
    text = '%.4f' % max(0.0, float(value or 0))
    text = text.rstrip('0').rstrip('.')
    return text or '0'


def _price_text(triple: dict) -> str:
    return '%s/%s/%s' % (
        _num_text(triple.get('input', 0)),
        _num_text(triple.get('cached_input', 0)),
        _num_text(triple.get('output', 0)),
    )


def summary_line(data: dict) -> str:
    totals = data.get('totals') or {}
    today = data.get('today') or {}
    month = data.get('month') or {}
    return '【费用】累计 %s · 今日 %s · 本月 %s' % (
        format_money(totals.get('cost', 0)),
        format_money(today.get('cost', 0)),
        format_money(month.get('cost', 0)),
    )


def detail_line(data: dict) -> str:
    """一行补充：请求数 / token / 计价方式。"""
    totals = data.get('totals') or {}
    prices = data.get('prices') or {}
    pv = data.get('peak_valley') or {}
    if pv.get('enabled'):
        mode = '峰谷价'
        detail = '峰时 %s · 空闲 %s' % (
            _price_text(pv.get('peak_prices') or {}),
            _price_text(pv.get('offpeak_prices') or {}),
        )
    else:
        mode = '牌价'
        detail = _price_text(prices)
    return '  · %s 次请求 / %s token · %s（输入/缓存/输出 每百万 token：%s）' % (
        totals.get('requests', 0),
        totals.get('tokens_text') or _fmt_tokens(totals.get('tokens', 0)),
        mode,
        detail,
    )


def conversation_line(row: dict) -> str:
    tail = row.get('last_at_text') or '--'
    return '  · %s：%s · %s 次 · 最近 %s' % (
        row.get('label') or '未知会话',
        row.get('cost_text') or format_money(row.get('cost', 0)),
        row.get('requests', 0),
        tail,
    )


async def report_lines(plugin, top: int | None = None, own_only: bool = False) -> list[str]:
    data = await overview(plugin, own_only=own_only)
    lines = render_lines(data, top=top)
    if own_only and lines and lines[0].startswith('【费用】累计'):
        lines[0] += ' · 只算自己的档案'
    return lines


def render_lines(data: dict, top: int | None = None) -> list[str]:
    """把 overview() 的结果排成纯文本（供 /费用、/报告、降级通道用）。"""
    if not data.get('enabled', True):
        return ['【费用】统计关着（cost.enabled=false）。']
    if data.get('error'):
        return ['【费用】读不出来：%s' % data['error']]
    head = summary_line(data)
    lines = [head, detail_line(data)]
    pv = data.get('peak_valley') or {}
    if pv.get('enabled'):
        lines.append(
            '  · 峰谷：峰时 %s 次 %s · 空闲 %s 次 %s（%s%s）'
            % (
                pv.get('peak_requests', 0),
                format_money(pv.get('peak_cost', 0)),
                pv.get('offpeak_requests', 0),
                format_money(pv.get('offpeak_cost', 0)),
                pv.get('ranges') or '09:00-12:00,14:00-18:00',
                ' · 仅工作日' if pv.get('weekdays_only', True) else '',
            )
        )
    rows = data.get('rows') or []
    if not rows:
        lines.append('  · 还没有调用记录')
        return lines
    limit = max(1, _as_int(top, _as_int(_RUNTIME.get('report_top'), 8)))
    privates = [row for row in rows if row.get('kind') == 'private']
    groups = [row for row in rows if row.get('kind') == 'group']
    shown = privates[:limit]
    if len(shown) < limit:
        shown += groups[: limit - len(shown)]
    lines.append('  · 按会话：私聊 %s 个 · 群聊 %s 个' % (len(privates), len(groups)))
    lines.extend(conversation_line(row) for row in shown)
    if len(privates) + len(groups) > len(shown):
        lines.append(
            '  · …还有 %s 个会话（页面「上下文」tab 看全部）'
            % (len(privates) + len(groups) - len(shown))
        )
    budget = data.get('budget')
    if budget:
        percent = int(round(float(budget.get('ratio') or 0.0) * 100))
        lines.append(
            '  · 本月预算 %s，已用 %s（%s%%）'
            % (budget.get('text'), format_money(budget.get('used', 0)), percent)
        )
    return lines