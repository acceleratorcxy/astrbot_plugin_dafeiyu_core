# -*- coding: utf-8 -*-
"""汇报投递：报告/汇报统一走「官方机器人」（QQ 官方机器人）并用 markdown 排版。

背景：
- 个人 QQ（aiocqhttp / OneBot v11）不支持 markdown，发过去只会显示原始符号；
- QQ 官方机器人（机器人「大肥鱼」，平台 id 形如 qq_official_bot）支持原生 markdown
  （msg_type=2，AstrBot 适配器用 MessageChain.use_markdown_ 标记），实测能正常渲染。

所以把「汇报类」内容（每晚整理报告、/报告、/费用、/上下文、备份结果）
交给本模块统一投递：默认走官方机器人 + markdown，失败再退回原来的私聊通道。
配置在插件配置页的「主人私聊通知（notify）」段。
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.core.message.components import Plain
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.core.platform.message_type import MessageType

DEFAULTS: dict[str, Any] = {
    # official=走官方机器人（markdown）；aiocqhttp=走原来的私聊通道（纯文本）
    "report_channel": "event",
    "report_platform_id": "",
    "report_target": "",
    "report_use_markdown": True,
    "report_fallback": True,
}

_RUNTIME: dict[str, Any] = dict(DEFAULTS)

_SECTION_RE = re.compile(r"^【([^】]+)】(.*)$")


def _apply_runtime(section: dict | None) -> None:
    """把插件配置里的 notify 段同步到本模块的运行时状态。"""
    section = section if isinstance(section, dict) else {}
    for key, default in DEFAULTS.items():
        value = section.get(key)
        if value is None or value == "":
            value = default
        if isinstance(default, bool):
            _RUNTIME[key] = bool(value)
        else:
            _RUNTIME[key] = str(value)
    return None


def _cfg(plugin: Any, key: str, default: Any = None) -> Any:
    """优先用运行时值，取不到再读插件配置。"""
    value = _RUNTIME.get(key)
    if value is not None and value != "":
        return value
    try:
        section = plugin._section("notify")
    except Exception:
        section = {}
    value = section.get(key) if isinstance(section, dict) else None
    if value is None or value == "":
        return DEFAULTS.get(key, default)
    return value


def to_markdown(text: str) -> str:
    """把「方括号小标题 + 两个空格缩进 · 」的纯文本汇报转成 markdown。

    - 第一行标题加 ## ；
    - 【备份】 这类小标题变成 **备份**；
    - 「  · xxx」变成「- xxx」。
    转换只做展示层，原文仍然是纯文本，方便退回不支持 markdown 的通道。
    """
    lines: list[str] = []
    for index, raw in enumerate(str(text or "").split("\n")):
        line = raw.rstrip()
        if not line.strip():
            lines.append("")
            continue
        matched = _SECTION_RE.match(line)
        if matched:
            name = matched.group(1).strip()
            rest = matched.group(2)
            lines.append(("**%s**%s" % (name, rest)).rstrip())
            continue
        stripped = line.lstrip()
        if stripped.startswith("·"):
            lines.append("- " + stripped[1:].strip())
            continue
        if index == 0 and not line.startswith("#"):
            lines.append("## " + line)
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def find_platform(plugin: Any, platform_id: str):
    """按平台 id 找平台适配器实例。"""
    if not platform_id:
        return None
    try:
        instances = plugin.context.platform_manager.platform_insts
    except Exception:
        return None
    for inst in instances:
        try:
            if str(inst.meta().id) == str(platform_id):
                return inst
        except Exception:
            continue
    return None


async def send(
    plugin: Any, plain_text: str, event: Any = None, markdown: str | None = None
) -> list[str]:
    """投递一份汇报，返回成功投递的目标标识列表。

    markdown 参数可以给一份「已经排版好的 markdown」（表格等）；不给就用
    plain_text 做一次简单转换（方括号小标题 / 缩进列表）。
    """
    text = str(plain_text or "").strip()
    if not text and not markdown:
        return []
    channel = str(_cfg(plugin, "report_channel", "official") or "official").strip().lower()
    use_markdown = bool(_cfg(plugin, "report_use_markdown", True))
    fallback = bool(_cfg(plugin, "report_fallback", True))

    if channel in ("official", "robot", "qqofficial", "qq_official"):
        platform_id = str(_cfg(plugin, "report_platform_id", "") or "").strip()
        target = str(_cfg(plugin, "report_target", "") or "").strip()
        inst = find_platform(plugin, platform_id)
        if inst is not None and target:
            if use_markdown:
                payload = str(markdown or "").strip() or to_markdown(text)
            else:
                payload = text
            try:
                chain = MessageChain(chain=[Plain(payload)])
                chain.use_markdown_ = use_markdown
                session = MessageSesion(platform_id, MessageType.FRIEND_MESSAGE, target)
                await inst.send_by_session(session, chain)
                logger.info(
                    "[dafeiyu_report] 汇报已发到官方机器人（%s）",
                    "markdown" if use_markdown else "纯文本",
                )
                return ["%s:FriendMessage:%s" % (platform_id, target)]
            except Exception as exc:
                logger.warning(
                    "[dafeiyu_report] 官方机器人发送失败: %s(%r)",
                    type(exc).__name__,
                    exc,
                )
        else:
            logger.warning(
                "[dafeiyu_report] 没找到官方机器人平台实例或没配接收人（platform=%s）",
                platform_id or "?",
            )
        if not fallback:
            return []
    elif channel == "event":
        if event is not None:
            try:
                await event.send(MessageChain().message(text))
                return [getattr(event, "unified_msg_origin", "current") or "current"]
            except Exception as exc:
                logger.warning("[dafeiyu_report] 当前会话发送失败: %s", exc)
        if not fallback:
            return []
    return await plugin.send_private_to_master(text, event)


# ===================================================================== 报告模型
# 汇报分两层：
#   1) build_model() 把要汇报的东西抓成结构化数据（备份/资料/夜里/上下文/费用/历史备份）；
#   2) render_markdown() 用表格排成好读的 markdown，render_plain() 退回纯文本。
# 这样同一个模型既能发官方机器人（markdown 表格），也能退回个人 QQ（纯文本）。


def _fmt_size(num: Any) -> str:
    try:
        value = float(num or 0)
    except Exception:
        value = 0.0
    if value >= 1024 * 1024:
        return "%.1fMB" % (value / (1024 * 1024))
    if value >= 1024:
        return "%dKB" % int(round(value / 1024))
    return "%dB" % int(value)


def _pct(ratio: Any) -> str:
    try:
        value = float(ratio or 0.0)
    except Exception:
        value = 0.0
    if value >= 0.01:
        return "%d%%" % int(round(value * 100))
    if value * 100 >= 0.1:
        return "%.1f%%" % (value * 100)
    return "不到 0.1%"


def md_cell(value: Any) -> str:
    """表格单元格转义：竖线换行都会破坏表格。"""
    text = str(value if value is not None else "")
    text = text.replace("\r", " ").replace("\n", " ").replace("|", "／")
    return text.strip() or "-"


def md_table(headers: list[str], rows: list[list[Any]], align: str = "") -> str:
    """拼一个 markdown 表格；align 形如 'lrr' 表示每列左/右对齐。"""
    head = "| " + " | ".join(md_cell(h) for h in headers) + " |"
    seps: list[str] = []
    for index, _ in enumerate(headers):
        side = (align[index] if index < len(align) else "") or ""
        if side == "r":
            seps.append("---:")
        elif side == "c":
            seps.append(":---:")
        else:
            seps.append("---")
    body = ["| " + " | ".join(md_cell(c) for c in row) + " |" for row in rows]
    return "\n".join([head, "| " + " | ".join(seps) + " |"] + body)


def _context_module():
    try:
        import importlib

        return importlib.import_module("core_context")
    except Exception:
        return None


async def build_model(plugin: Any, result: dict | None = None, top_context: int = 40,
                      top_cost: int = 6) -> dict:
    """抓一份汇报需要的结构化数据。result 是刚做完的备份结果（可选）。"""
    model: dict[str, Any] = {
        "title": "🐋 鲸鲸的每日整理",
        "stamp": _dt.datetime.now().strftime("%m-%d %H:%M"),
        "backup": None,
        "counts": {},
        "night": [],
        "scope": "",
        "contexts": [],
        "cost": None,
        "backups": [],
        "interest": {},
    }
    backup = getattr(plugin, "_backup", None)
    counts: dict[str, Any] = dict((result or {}).get("counts") or {})
    filename = str((result or {}).get("filename") or "")
    size = 0
    if result and filename and backup is not None:
        try:
            size = int((backup.dir / filename).stat().st_size)
        except Exception:
            size = 0
    if backup is not None:
        if not counts:
            try:
                data, _meta = await backup.latest()
                counts = dict((data or {}).get("counts") or {})
            except Exception as exc:
                logger.warning("[dafeiyu_report] 读取最新备份失败: %s", exc)
        try:
            rows = backup.list_backups()
        except Exception:
            rows = []
        if rows:
            latest = rows[0]
            model["backups"] = [
                {
                    "file": row.get("filename") or "",
                    "at": row.get("created_text") or "",
                    "size": _fmt_size(row.get("size")),
                }
                for row in rows
            ]
            if not filename:
                filename = latest.get("filename") or ""
                size = int(latest.get("size") or 0)
        try:
            keep = int(backup._keep())
        except Exception:
            keep = 3
        if filename:
            at_text = ""
            try:
                path = backup.dir / filename
                if path.exists():
                    at_text = _dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                at_text = ""
            model["backup"] = {
                "file": filename,
                "size": _fmt_size(size),
                "keep": keep,
                "at": at_text,
            }
    model["counts"] = counts

    guard = getattr(plugin, "_guard", None)
    night: list[dict[str, str]] = []
    if guard is not None:
        try:
            status = await guard.status(None)
        except Exception:
            status = {}
        summary_count = int(status.get("summary_count") or 0)
        restore = status.get("persona_restore")
        group_auto = status.get("group_auto") or {}
        if summary_count:
            night.append({"name": "已压缩会话", "text": "%d 个（记忆还在）" % summary_count})
        if isinstance(restore, dict) and restore.get("text"):
            night.append({"name": "人格资料复原", "text": "有（压缩后从最新备份重装过）"})
        if group_auto.get("enabled"):
            night.append(
                {
                    "name": "群聊自动压缩",
                    "text": "余量 %s · 安静 %s 分钟 · 每天 %s 点"
                    % (
                        _pct(group_auto.get("auto_ratio", 0.7)),
                        group_auto.get("idle_minutes", 30),
                        group_auto.get("daily_hour", 4),
                    ),
                }
            )
    sleep_module = getattr(plugin, "_sleep_feature", None)
    if sleep_module is not None:
        try:
            sl = sleep_module.sleep_status()
        except Exception:
            sl = {}
        if sl.get("enabled"):
            night.append(
                {
                    "name": "睡眠",
                    "text": "安静 %s 点 · 睡 %s 点 · 醒 %s 点+%s 分（今早%s）"
                    % (
                        sl.get("quiet_hour", 1),
                        sl.get("sleep_hour", 2),
                        sl.get("wake_hour", 6),
                        sl.get("wake_jitter_minutes", 45),
                        "已醒" if sl.get("phase") == "awake" else "还没醒",
                    ),
                }
            )
    model["night"] = night

    store = getattr(plugin, "_interest_store", None)
    if store is not None:
        try:
            model["interest"] = {
                "count": len(store.interests()),
                "candidates": len(store.candidates()),
            }
        except Exception:
            model["interest"] = {}

    if guard is not None:
        try:
            data = await guard.conversation_rows(top_context, own_only=True)
        except Exception as exc:
            logger.warning("[dafeiyu_report] 读取会话上下文失败: %s", exc)
            data = {"rows": []}
        for row in data.get("rows") or []:
            model["contexts"].append(
                {
                    "label": row.get("label") or row.get("umo") or "?",
                    "messages": row.get("messages", 0),
                    "pct": _pct(row.get("ratio")),
                    "summary": bool(row.get("summary")),
                }
            )
        context_mod = _context_module()
        if context_mod is not None:
            try:
                owned = context_mod.own_profile_ids(plugin)
                names = "、".join(context_mod.profile_name(plugin, cid) for cid in sorted(owned))
                model["scope"] = names
            except Exception:
                pass

    cost_module = getattr(plugin, "_cost_feature", None)
    if cost_module is not None:
        try:
            model["cost"] = await cost_module.overview(plugin, own_only=True)
            model["cost_top"] = top_cost
        except Exception as exc:
            logger.warning("[dafeiyu_report] 读取费用失败: %s", exc)
    return model


def _money(value: Any) -> str:
    try:
        return "¥%.2f" % float(value or 0)
    except Exception:
        return "¥0"


def _cost_rows(data: dict, top: int) -> list[dict]:
    rows = [row for row in (data.get("rows") or [])]
    privates = [row for row in rows if row.get("kind") == "private"]
    groups = [row for row in rows if row.get("kind") == "group"]
    shown = privates[:top]
    if len(shown) < top:
        shown += groups[: top - len(shown)]
    return shown, len(privates), len(groups)


def cost_markdown(data: dict | None, top: int = 6) -> str:
    """费用段：汇总一行 + 每会话表格。"""
    if not data:
        return ""
    if not data.get("enabled", True):
        return "### 💰 费用\n统计关着（cost.enabled=false）。"
    if data.get("error"):
        return "### 💰 费用\n读不出来：%s" % md_cell(data.get("error"))
    totals = data.get("totals") or {}
    today = data.get("today") or {}
    month = data.get("month") or {}
    pv = data.get("peak_valley") or {}
    prices = data.get("prices") or {}
    lines = ["### 💰 费用", ""]
    lines.append(
        "累计 **%s** · 今日 %s · 本月 %s · %s 次请求 / %s token"
        % (
            _money(totals.get("cost")),
            _money(today.get("cost")),
            _money(month.get("cost")),
            totals.get("requests", 0),
            totals.get("tokens_text") or totals.get("tokens", 0),
        )
    )
    if pv.get("enabled"):
        lines.append(
            "计价：峰谷价（峰时 %s 次 %s / 空闲 %s 次 %s；%s%s）"
            % (
                pv.get("peak_requests", 0),
                _money(pv.get("peak_cost")),
                pv.get("offpeak_requests", 0),
                _money(pv.get("offpeak_cost")),
                pv.get("ranges") or "09:00-12:00,14:00-18:00",
                " · 仅工作日" if pv.get("weekdays_only", True) else "",
            )
        )
    else:
        lines.append(
            "计价：牌价（每百万 token 输入/缓存/输出 %s/%s/%s）"
            % (
                prices.get("input", 0),
                prices.get("cached_input", 0),
                prices.get("output", 0),
            )
        )
    shown, private_count, group_count = _cost_rows(data, max(1, int(top or 6)))
    if not shown:
        lines.append("")
        lines.append("还没有调用记录。")
    else:
        lines.append("")
        lines.append("私聊 %d 个 · 群聊 %d 个" % (private_count, group_count))
        lines.append("")
        lines.append(
            md_table(
                ["会话", "花费", "次数", "最近"],
                [
                    [
                        row.get("label") or "未知会话",
                        row.get("cost_text") or _money(row.get("cost")),
                        row.get("requests", 0),
                        row.get("last_at_text") or "--",
                    ]
                    for row in shown
                ],
                "lrrl",
            )
        )
        rest = private_count + group_count - len(shown)
        if rest > 0:
            lines.append("")
            lines.append("…还有 %d 个会话（页面「上下文」tab 看全部）" % rest)
    budget = data.get("budget")
    if budget:
        lines.append("")
        lines.append(
            "本月预算 %s，已用 %s（%d%%）"
            % (
                budget.get("text"),
                _money(budget.get("used")),
                int(round(float(budget.get("ratio") or 0.0) * 100)),
            )
        )
    return "\n".join(lines)


def context_markdown(rows: list[dict], scope: str = "", title: str = "每个对话的上下文") -> str:
    """上下文段：每个会话一行表格（消息数 / 余量 / 状态）。"""
    lines = ["### 💬 %s" % title, ""]
    if scope:
        lines.append("只算鲸鲸自己的配置档案：%s" % scope)
        lines.append("")
    if not rows:
        lines.append("暂时还没有对话。")
        return "\n".join(lines)
    lines.append(
        md_table(
            ["会话", "消息", "占比", "状态"],
            [
                [
                    row.get("label") or row.get("umo") or "?",
                    row.get("messages", 0),
                    _pct(row.get("ratio")),
                    "已压缩" if row.get("summary") else "—",
                ]
                for row in rows
            ],
            "lrll",
        )
    )
    return "\n".join(lines)


def render_markdown(model: dict) -> str:
    """把模型排成一份带表格的 markdown 汇报。"""
    parts: list[str] = ["## %s · %s" % (model.get("title") or "汇报", model.get("stamp") or "")]
    backup = model.get("backup") or {}
    if backup:
        rows = [["文件", backup.get("file") or "-"]]
        if backup.get("at"):
            rows.append(["时间", backup.get("at")])
        rows.append(["大小", backup.get("size") or "-"])
        rows.append(["保留", "最新 %s 份" % backup.get("keep", 3)])
        parts.append("### 🗂 备份\n\n" + md_table(["项目", "内容"], rows))
    counts = model.get("counts") or {}
    if counts:
        rows = [
            ["人格", counts.get("personas", 0)],
            ["人格文件夹", counts.get("persona_folders", 0)],
            ["配置档案", counts.get("config_profiles", 0)],
            ["兴趣", "%s 条（观察中 %s 条）" % (counts.get("interests", 0), counts.get("candidates", 0))],
            [
                "会话",
                "%s 个 / %s 条消息"
                % (counts.get("conversations", 0), counts.get("messages", 0)),
            ],
            ["发言记录", "%s 条" % counts.get("speaker_lines", 0)],
            ["压缩记忆", "%s 个" % counts.get("context_summaries", 0)],
            [
                "表情包",
                "%s 包 / %s 个文件"
                % (counts.get("meme_packs", 0), counts.get("meme_files", 0)),
            ],
        ]
        parts.append("### 📦 资料\n\n" + md_table(["项目", "数量"], rows, "lr"))
    night = model.get("night") or []
    if night:
        parts.append(
            "### 🌙 夜里\n\n"
            + md_table(["项目", "状态"], [[row.get("name"), row.get("text")] for row in night])
        )
    contexts = model.get("contexts") or []
    ctx = context_markdown(contexts, model.get("scope") or "")
    parts.append(ctx)
    cost = model.get("cost")
    if cost is not None:
        parts.append(cost_markdown(cost, int(model.get("cost_top") or 6)))
    backups = model.get("backups") or []
    if backups:
        parts.append(
            "### 🗃 备份里还有\n\n"
            + md_table(
                ["文件", "时间", "大小"],
                [[row.get("file"), row.get("at"), row.get("size")] for row in backups],
                "llr",
            )
        )
    return "\n\n".join(part for part in parts if part).strip()


def render_plain(model: dict) -> str:
    """退化成纯文本（个人 QQ / 不支持 markdown 的通道用）。"""
    blocks: list[str] = ["%s（%s）" % (model.get("title") or "汇报", model.get("stamp") or "")]
    backup = model.get("backup") or {}
    if backup:
        lines = ["【备份】"]
        if backup.get("file"):
            lines.append("  · %s" % backup["file"])
        lines.append("  · %s · 只留最新 %s 份" % (backup.get("size") or "-", backup.get("keep", 3)))
        blocks.append("\n".join(lines))
    counts = model.get("counts") or {}
    if counts:
        blocks.append(
            "\n".join(
                [
                    "【资料】",
                    "  · 人格 %s · 文件夹 %s · 配置档案 %s"
                    % (counts.get("personas", 0), counts.get("persona_folders", 0), counts.get("config_profiles", 0)),
                    "  · 兴趣 %s 条（观察中 %s 条）" % (counts.get("interests", 0), counts.get("candidates", 0)),
                    "  · 会话 %s 个 / %s 条消息 · 发言记录 %s 条 · 压缩记忆 %s 个"
                    % (
                        counts.get("conversations", 0),
                        counts.get("messages", 0),
                        counts.get("speaker_lines", 0),
                        counts.get("context_summaries", 0),
                    ),
                    "  · 表情包 %s 包 / %s 个文件" % (counts.get("meme_packs", 0), counts.get("meme_files", 0)),
                ]
            )
        )
    night = model.get("night") or []
    if night:
        blocks.append("【夜里】\n" + "\n".join("  · %s：%s" % (row.get("name"), row.get("text")) for row in night))
    block = ["【每个对话的上下文】" + ("只算鲸鲸自己的档案 · %d 个" % len(model.get("contexts") or []))]
    for row in model.get("contexts") or []:
        block.append(
            "  · %s：%s 条 · %s%s"
            % (
                row.get("label"),
                row.get("messages", 0),
                row.get("pct"),
                " · 已压缩" if row.get("summary") else "",
            )
        )
    if not model.get("contexts"):
        block.append("  · 暂时还没有对话")
    blocks.append("\n".join(block))
    interest = model.get("interest") or {}
    if interest:
        blocks.append(
            "【兴趣】\n  · 现在 %s 条 · 观察中 %s 条" % (interest.get("count", 0), interest.get("candidates", 0))
        )
    cost = model.get("cost")
    if cost:
        totals = cost.get("totals") or {}
        today = cost.get("today") or {}
        month = cost.get("month") or {}
        blocks.append(
            "\n".join(
                [
                    "【费用】累计 %s · 今日 %s · 本月 %s"
                    % (_money(totals.get("cost")), _money(today.get("cost")), _money(month.get("cost"))),
                    "  · %s 次请求 / %s token"
                    % (totals.get("requests", 0), totals.get("tokens_text") or totals.get("tokens", 0)),
                ]
            )
        )
    backups = model.get("backups") or []
    if backups:
        blocks.append("【备份里还有】\n" + "\n".join("  · %s" % row.get("file") for row in backups))
    return "\n\n".join(blocks).strip()

