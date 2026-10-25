# -*- coding: utf-8 -*-
"""统一查看/配置页面（AstrBot 插件页面 views/index.html）的后端接口。

路由统一以本插件 id 开头，例如：
    GET  /astrbot_plugin_dafeiyu_core/page/overview
    POST /astrbot_plugin_dafeiyu_core/page/interest/add
前端（views/index.html）里用 AstrBotPluginView.apiGet("page/overview") 访问。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from astrbot.api import logger
from astrbot.api.web import file_response, request

PLUGIN_ID = "astrbot_plugin_dafeiyu_core"

# 备份文件名的固定格式（core_backup 生成，下载时只认这个）
BACKUP_NAME_RE = re.compile(r"^dafeiyu-backup-\d{8}-\d{6}\.json$")
# 导入备份的大小上限：备份是全量 JSON，正常几 MB，太大的直接拒掉，避免一次性读爆内存
BACKUP_IMPORT_MAX_BYTES = 20 * 1024 * 1024

SECTIONS = ("owner", "group_wake", "humanizer", "interest", "meme", "search", "speaker", "cost", "sleep", "knowledge", "backup", "context_guard", "notify")

SECTION_TITLES = {
    "owner": "主人身份与听令",
    "group_wake": "群聊唤醒",
    "humanizer": "人味化输出",
    "interest": "兴趣机制",
    "meme": "主动发表情包",
    "search": "联网搜索",
    "speaker": "群聊发言记录",
    "cost": "费用计算",
    "sleep": "睡眠模式",
    "knowledge": "群档案知识库",
    "backup": "全量资料备份",
    "context_guard": "上下文守护",
    "notify": "主人私聊通知",
}


_SCHEMA_META: dict | None = None


def _schema_meta() -> dict:
    """读取 _conf_schema.json，给页面提供中文字段说明。"""
    global _SCHEMA_META
    if _SCHEMA_META is not None:
        return _SCHEMA_META
    meta: dict = {}
    try:
        data = json.loads(Path(__file__).with_name("_conf_schema.json").read_text(encoding="utf-8-sig"))
        for section, sec in data.items():
            if not isinstance(sec, dict):
                continue
            items = sec.get("items") or {}
            fields = {}
            for name, item in items.items():
                if not isinstance(item, dict):
                    continue
                fields[name] = {
                    "type": item.get("type") or "",
                    "description": item.get("description") or "",
                    "default": item.get("default"),
                }
            meta[section] = fields
    except Exception as exc:
        logger.warning("[dafeiyu_core] 读取配置 schema 失败: %s", exc)
    _SCHEMA_META = meta
    return meta


def _prefix() -> str:
    return f"/{PLUGIN_ID}/page"


async def _json_body() -> dict:
    try:
        body = await request.json(default={})
    except Exception:
        body = {}
    return body if isinstance(body, dict) else {}


# ------------------------------------------------------------ 配置值类型校验
# 页面把输入框里的东西原样 POST 过来（"5" "false" "1.5"），直接落盘会让下游
# int()/float() 抛 ValueError（备份循环、压缩循环），或者 bool("false") == True。
_FIELD_SCHEMA: dict | None = None

_BOOL_TRUE = ("true", "1", "yes", "y", "on", "是", "开", "真")
_BOOL_FALSE = ("false", "0", "no", "n", "off", "否", "关", "假")


def _section_fields() -> dict:
    """读 _conf_schema.json 里每个分段的字段定义（原始 dict：type / default / items）。"""
    global _FIELD_SCHEMA
    if _FIELD_SCHEMA is not None:
        return _FIELD_SCHEMA
    fields: dict = {}
    try:
        data = json.loads(Path(__file__).with_name("_conf_schema.json").read_text(encoding="utf-8-sig"))
        for section, sec in data.items():
            if not isinstance(sec, dict):
                continue
            items = sec.get("items") or {}
            if isinstance(items, dict):
                fields[section] = {k: v for k, v in items.items() if isinstance(v, dict)}
    except Exception as exc:
        logger.warning("[dafeiyu_core] 读取配置字段定义失败: %s", exc)
    _FIELD_SCHEMA = fields
    return fields


def _coerce_bool_value(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _BOOL_TRUE:
        return True
    if text in _BOOL_FALSE:
        return False
    raise ValueError("不是开关（true/false）")


def _coerce_number(value, as_int: bool):
    if isinstance(value, bool):
        raise ValueError("不是数字")
    if isinstance(value, str):
        value = value.strip().replace(",", "")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError("不是有限数字")
    return int(number) if as_int else number


def _clamp_float(value: float, default, name: str = "") -> float:
    """按 schema 默认值判断量纲：默认值 ≤1 当比例（0~1），≤100 当百分数（0~100）。"""
    try:
        base = float(default)
    except (TypeError, ValueError):
        base = 0.0
    if base <= 1.0:
        return max(0.0, min(1.0, value))
    if base <= 100.0:
        return max(0.0, min(100.0, value))
    return max(0.0, value)


def _clamp_int(value: int, default, name: str = "") -> int:
    """整数兜底：钟点按 0~23（-1 表示关），分钟 0~59，其余不允许离谱的巨大值。"""
    if name == "hour" or name.endswith("_hour"):
        return max(-1, min(23, value))
    if name == "minute" or name.endswith("_minute") or name.endswith("_minutes"):
        # 注意 "wake_jitter_minutes" 这类是"多少分钟"而不是"分钟数"，不能被钳到 0~59
        if name.endswith("_minutes") and name != "minute":
            pass
        else:
            return max(0, min(59, value))
    try:
        base = int(default)
    except (TypeError, ValueError):
        base = 0
    low = base if base < 0 else 0
    high = max(abs(base) * 100, 1000)
    return max(low, min(high, value))


def _coerce_list_value(value, options=None) -> list:
    if isinstance(value, str):
        items = [x.strip() for x in value.replace("，", ",").split(",")]
    elif isinstance(value, (list, tuple)):
        items = [str(x).strip() for x in value]
    else:
        raise ValueError("要列表或逗号分隔的字符串")
    items = [x for x in items if x]
    if isinstance(options, list) and options:
        allowed = {str(x) for x in options}
        bad = [x for x in items if x not in allowed]
        if bad:
            raise ValueError("不在可选范围：" + "、".join(bad[:3]))
    return items


def _coerce_field(value, spec: dict, name: str = ""):
    """按 schema 把一个值转成正确类型；转不了 raise ValueError（中文原因）。"""
    ftype = str(spec.get("type") or "string").strip().lower()
    default = spec.get("default")
    if ftype == "bool":
        return _coerce_bool_value(value)
    if ftype == "int":
        return _clamp_int(_coerce_number(value, True), default, name)
    if ftype == "float":
        return _clamp_float(_coerce_number(value, False), default, name)
    if ftype == "list":
        return _coerce_list_value(value, spec.get("items"))
    if ftype == "object":
        if isinstance(value, dict):
            return value
        raise ValueError("要一个对象")
    if ftype in ("string", "text"):
        if isinstance(value, (dict, list)):
            raise ValueError("要文本")
        return str(value)
    return value


def _personas_brief(personas) -> list[dict]:
    rows = []
    for item in personas or []:
        tools = getattr(item, "tools", None)
        if isinstance(tools, list):
            tools_brief = f"{len(tools)} 个工具"
        elif tools is None:
            tools_brief = "全部工具"
        else:
            tools_brief = "-"
        rows.append(
            {
                "persona_id": getattr(item, "persona_id", ""),
                "folder_id": getattr(item, "folder_id", None),
                "tools": tools_brief,
                "prompt_len": len(getattr(item, "system_prompt", "") or ""),
                "sort_order": getattr(item, "sort_order", 0),
            }
        )
    return rows


def register_page_apis(plugin) -> None:
    ctx = plugin.context
    p = _prefix()

    # ------------------------------------------------------------- overview
    async def page_overview():
        ctx_plugin = plugin
        personas = []
        profiles = []
        folders = []
        try:
            personas = _personas_brief(await ctx_plugin.context.persona_manager.get_all_personas())
        except Exception as exc:
            logger.warning("[dafeiyu_core] 页面读取人格失败: %s", exc)
        try:
            folders = [
                {
                    "folder_id": getattr(f, "folder_id", ""),
                    "name": getattr(f, "name", ""),
                    "parent_id": getattr(f, "parent_id", None),
                }
                for f in await ctx_plugin.context.persona_manager.get_all_folders()
            ]
        except Exception:
            folders = []
        try:
            for info in ctx_plugin.context.astrbot_config_mgr.get_conf_list():
                profiles.append({"id": info.get("id"), "name": info.get("name")})
        except Exception:
            profiles = []
        interests = []
        candidates = []
        interest_merge = {}
        store = ctx_plugin._interest_store
        if store is not None:
            try:
                interest_merge = store.merge_brief()
            except Exception:
                interest_merge = {}
            for cand in store.candidates():
                candidates.append(
                    {
                        "id": cand.get("id"),
                        "name": cand.get("name"),
                        "mentions": cand.get("mentions", 0),
                        "source": cand.get("source") or "",
                        "last_seen": cand.get("last_seen") or 0,
                        "notes": [
                            str((n or {}).get("text") or "")[:160]
                            for n in (cand.get("notes") or [])
                        ],
                    }
                )
            for item in store.interests():
                interests.append(
                    {
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "aliases": item.get("aliases") or [],
                        "affinity": round(float(item.get("affinity") or 0), 3),
                        "mention_count": item.get("mention_count", 0),
                        "pinned": bool(item.get("pinned")),
                        "kind": item.get("kind") or "auto",
                        "last_seen": item.get("last_seen") or 0,
                        "notes": [
                            {
                                "id": (n or {}).get("id") or "",
                                "text": str((n or {}).get("text") or "")[:200],
                                "title": (n or {}).get("title") or "",
                                "url": (n or {}).get("url") or "",
                                "source": (n or {}).get("source") or "",
                                "at": (n or {}).get("at") or 0,
                            }
                            for n in (item.get("notes") or [])
                        ],
                    }
                )
        backups = ctx_plugin._backup.list_backups() if ctx_plugin._backup else []
        context_brief = await ctx_plugin._guard.status() if ctx_plugin._guard else {}
        try:
            command_brief = {
                "platforms": list(ctx_plugin._command_platforms()),
                "private_enabled": ctx_plugin._command_flag("command_private_enabled", True),
                "group_enabled": ctx_plugin._command_flag("command_group_enabled", False),
                "master_only": ctx_plugin._command_master_only(),
                "group_master_ids": sorted(ctx_plugin._command_group_master_ids()),
            }
        except Exception:
            command_brief = {}
        return {
            "plugin": {
                "id": PLUGIN_ID,
                "version": getattr(ctx_plugin, "VERSION", ""),
            },
            "personas": personas,
            "folders": folders,
            "profiles": profiles,
            "interests": interests,
            "candidates": candidates,
            "interest_merge": interest_merge,
            "backups": backups,
            "context": context_brief,
            "command": command_brief,
            "sections": {
                key: ctx_plugin._section(key) for key in SECTIONS
            },
            "section_titles": SECTION_TITLES,
            "stats": {
                "personas": len(personas),
                "folders": len(folders),
                "profiles": len(profiles),
                "interests": len(interests),
                "candidates": len(candidates),
                "backups": len(backups),
            },
        }

    ctx.register_web_api(f"{p}/overview", page_overview, ["GET"], "大肥鱼人格总览")

    # ------------------------------------------------------------ interests
    async def interest_add():
        body = await _json_body()
        name = str(body.get("name") or "").strip()
        if not name:
            return {"status": "error", "message": "兴趣名字不能为空"}
        aliases = body.get("aliases") or ""
        if isinstance(aliases, list):
            aliases = ",".join(map(str, aliases))
        try:
            affinity = max(0.0, min(1.0, float(body.get("affinity") or 0.7)))
        except Exception:
            return {"status": "error", "message": "亲密度要 0~1 的数字"}
        store = plugin._interest_store
        if store is None:
            return {"status": "error", "message": "兴趣库没准备好"}
        item = store.add(name, str(aliases), affinity, pinned=True, kind="master")
        return {"status": "ok", "message": f"已添加「{item.get('name')}」", "data": {"id": item.get("id")}}

    async def interest_remove():
        body = await _json_body()
        key = str(body.get("id") or body.get("name") or "").strip()
        store = plugin._interest_store
        if store is None or not key:
            return {"status": "error", "message": "参数或兴趣库有问题"}
        item = store.get_by_key(key)
        ok = store.remove(item["name"]) if item else False
        return {"status": "ok" if ok else "error", "message": "已删除" if ok else "没找到这个兴趣"}

    async def candidate_promote():
        body = await _json_body()
        store = plugin._interest_store
        if store is None:
            return {"status": "error", "message": "兴趣库没准备好"}
        key = str(body.get("id") or body.get("name") or "").strip()
        cand = store.get_candidate_by_key(key) if key else None
        if not cand:
            return {"status": "error", "message": "没找到这个临时话题"}
        item = store.promote(cand["name"], cand.get("aliases"), cand.get("mentions"))
        return {"status": "ok", "message": f"已收进兴趣「{item.get('name')}」", "data": {"id": item.get("id")}}

    async def candidate_forget():
        body = await _json_body()
        store = plugin._interest_store
        if store is None:
            return {"status": "error", "message": "兴趣库没准备好"}
        key = str(body.get("id") or body.get("name") or "").strip()
        cand = store.get_candidate_by_key(key) if key else None
        if not cand:
            return {"status": "error", "message": "没找到这个临时话题"}
        try:
            store.candidates().remove(cand)
        except ValueError:
            pass
        store.save()
        return {"status": "ok", "message": f"已经忘掉「{cand.get('name')}」"}

    async def interest_update():
        body = await _json_body()
        store = plugin._interest_store
        if store is None:
            return {"status": "error", "message": "兴趣库没准备好"}
        key = str(body.get("id") or body.get("name") or "").strip()
        item = store.get_by_key(key) if key else None
        if not item:
            return {"status": "error", "message": "没找到这个兴趣"}
        if "affinity" in body:
            try:
                item["affinity"] = max(0.0, min(1.0, float(body.get("affinity"))))
            except Exception:
                pass
        if "aliases" in body:
            aliases = body.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [a.strip() for a in aliases.replace("，", ",").split(",") if a.strip()]
            item["aliases"] = aliases
        if "note" in body and str(body.get("note") or "").strip():
            store.add_note(item, str(body.get("note")).strip(), "", "", "page")
        store.save()
        return {"status": "ok", "message": "已保存"}

    async def interest_search():
        body = await _json_body()
        topic = str(body.get("topic") or "").strip()
        if not topic:
            return {"status": "error", "message": "要搜什么？"}
        store = plugin._interest_store
        if store is None:
            return {"status": "error", "message": "兴趣库没准备好"}
        results = await plugin.interest_search_results(topic)
        item = store.get_by_name(topic)
        if item is None:
            store.remember_candidate(topic, source="page")
            store.bump_search()
            return {
                "status": "ok",
                "message": f"搜到 {len(results)} 条；「{topic}」先记为临时话题（聊得多才会收进兴趣）",
                "data": {"results": results[:5], "candidate": True},
            }
        added = 0
        for result in results[:2]:
            if store.add_note(
                item,
                plugin.interest_note_text(result),
                result.get("title", ""),
                result.get("url", ""),
                "page",
            ):
                added += 1
        item["last_search"] = plugin.interest_now()
        store.bump_search()
        store.save()
        return {
            "status": "ok",
            "message": f"搜到 {len(results)} 条，记了 {added} 条",
            "data": {"results": results[:5]},
        }

    async def interest_note_update():
        body = await _json_body()
        store = plugin._interest_store
        if store is None:
            return {"status": "error", "message": "兴趣库没准备好"}
        key = str(body.get("interest_id") or body.get("id") or body.get("name") or "").strip()
        note_id = str(body.get("note_id") or body.get("nid") or "").strip()
        item = store.get_by_key(key) if key else None
        if not item or not note_id:
            return {"status": "error", "message": "参数不全，找不到这条记录"}
        text = body.get("text")
        title = body.get("title")
        url = body.get("url")
        if text is not None and not str(text).strip():
            return {"status": "error", "message": "记录内容不能为空"}
        if store.update_note(item, note_id, text=text, title=title, url=url):
            return {"status": "ok", "message": "记录已更新"}
        return {"status": "error", "message": "没找到这条记录（可能已被删除）"}

    async def interest_note_delete():
        body = await _json_body()
        store = plugin._interest_store
        if store is None:
            return {"status": "error", "message": "兴趣库没准备好"}
        key = str(body.get("interest_id") or body.get("id") or body.get("name") or "").strip()
        note_id = str(body.get("note_id") or body.get("nid") or "").strip()
        item = store.get_by_key(key) if key else None
        if not item or not note_id:
            return {"status": "error", "message": "参数不全，找不到这条记录"}
        if store.remove_note(item, note_id):
            return {"status": "ok", "message": "记录已删除"}
        return {"status": "error", "message": "没找到这条记录（可能已被删除）"}

    ctx.register_web_api(f"{p}/interest/add", interest_add, ["POST"], "添加兴趣")
    ctx.register_web_api(f"{p}/interest/remove", interest_remove, ["POST"], "删除兴趣")
    ctx.register_web_api(f"{p}/interest/update", interest_update, ["POST"], "修改兴趣")
    ctx.register_web_api(f"{p}/interest/search", interest_search, ["POST"], "搜索并记录兴趣")
    async def interest_merge():
        store = plugin._interest_store
        if store is None:
            return {"status": "error", "message": "兴趣库没准备好"}
        try:
            merged = store.merge_similar()
        except Exception as exc:
            return {"status": "error", "message": f"合并失败：{exc}"}
        brief = store.merge_brief()
        if not merged:
            return {"status": "ok", "message": "看了一圈，没有能合并的兴趣", "count": 0, "merged": [], "data": {"merged": [], "brief": brief}}
        return {
            "status": "ok",
            "message": f"合并了 {len(merged)} 组",
            "count": len(merged),
            "merged": merged,
            "data": {"merged": merged, "brief": brief},
        }

    ctx.register_web_api(f"{p}/interest/merge", interest_merge, ["POST"], "合并相似兴趣")
    ctx.register_web_api(f"{p}/interest/note/update", interest_note_update, ["POST"], "编辑兴趣记录")
    ctx.register_web_api(f"{p}/interest/note/delete", interest_note_delete, ["POST"], "删除兴趣记录")
    ctx.register_web_api(f"{p}/interest/candidate/promote", candidate_promote, ["POST"], "把临时话题收进兴趣")
    ctx.register_web_api(f"{p}/interest/candidate/forget", candidate_forget, ["POST"], "忘掉临时话题")

    # -------------------------------------------------------------- backups
    async def backup_list():
        rows = plugin._backup.list_backups() if plugin._backup else []
        return {"status": "ok", "data": {"backups": rows, "keep": plugin._backup._keep() if plugin._backup else 3}}

    async def backup_create():
        if plugin._backup is None:
            return {"status": "error", "ok": False, "message": "备份模块没准备好"}
        try:
            result = await plugin._backup.create(reason="page")
        except Exception as exc:
            logger.warning("[dafeiyu_core] 页面立即备份失败: %s", exc)
            return {"status": "error", "ok": False, "message": f"备份失败：{exc}"}
        if not isinstance(result, dict):
            return {"status": "error", "ok": False, "message": "备份返回结果不对"}
        return {
            "status": "ok",
            "ok": True,
            "message": f"已备份 {result.get('filename')}",
            "data": result,
        }

    async def backup_restore():
        body = await _json_body()
        filename = str(body.get("filename") or "").strip()
        if plugin._backup is None or not filename:
            return {"status": "error", "ok": False, "message": "参数不对"}
        try:
            result = await plugin._backup.restore_file(filename)
        except Exception as exc:
            logger.warning("[dafeiyu_core] 页面恢复备份失败: %s", exc)
            return {"status": "error", "ok": False, "message": f"恢复失败：{exc}"}
        # core_backup.restore_file 现在返回 dict（含 ok/error），失败时按错误返回
        if isinstance(result, dict) and result.get("ok") is False:
            reason = result.get("error") or result.get("message") or "恢复失败"
            return {"status": "error", "ok": False, "message": str(reason), "data": result}
        return {"status": "ok", "ok": True, "message": "已恢复", "data": result}

    async def backup_delete():
        body = await _json_body()
        filename = str(body.get("filename") or "").strip()
        if plugin._backup is None or not filename:
            return {"status": "error", "message": "参数不对"}
        ok = plugin._backup.delete(filename)
        return {"status": "ok" if ok else "error", "message": "已删除" if ok else "没找到"}

    async def backup_download():
        filename = (request.query.get("filename") or "").strip()
        if plugin._backup is None or not filename:
            return {"status": "error", "message": "参数不对"}
        # 只认 core_backup 生成的文件名（dafeiyu-backup-YYYYmmdd-HHMMSS.json），顺便挡住路径穿越
        if not BACKUP_NAME_RE.match(filename):
            return {"status": "error", "message": "文件名不对"}
        path = plugin._backup.dir / filename
        if not path.exists():
            return {"status": "error", "message": "没找到这个备份"}
        return file_response(path, filename=path.name, content_type="application/json")

    async def backup_import():
        files = await request.files()
        upload = files.get("file") if files else None
        if upload is None:
            return {"status": "error", "message": "没收到文件"}
        try:
            size = int(getattr(upload, "content_length", 0) or 0)
        except Exception:
            size = 0
        if size > BACKUP_IMPORT_MAX_BYTES:
            return {
                "status": "error",
                "message": f"备份文件太大了（上限 {BACKUP_IMPORT_MAX_BYTES // (1024 * 1024)}MB）",
            }
        try:
            raw = await upload.read()
        except Exception as exc:
            logger.warning("[dafeiyu_core] 读取导入备份失败: %s", exc)
            return {"status": "error", "message": f"读文件失败：{exc}"}
        if len(raw) > BACKUP_IMPORT_MAX_BYTES:
            return {
                "status": "error",
                "message": f"备份文件太大了（上限 {BACKUP_IMPORT_MAX_BYTES // (1024 * 1024)}MB）",
            }
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return {"status": "error", "message": "文件不是合法 JSON"}
        # 结构校验：只认本插件导出的备份（顶层必须有 schema 或 version），不合格不进 restore
        if not isinstance(data, dict):
            return {"status": "error", "message": "备份文件格式不对：不是 JSON 对象"}
        if "schema" not in data and "version" not in data:
            return {"status": "error", "message": "备份文件格式不对：缺少 schema/version 字段"}
        if plugin._backup is None:
            return {"status": "error", "message": "备份模块没准备好"}
        try:
            result = await plugin._backup.restore(data)
        except Exception as exc:
            return {"status": "error", "ok": False, "message": f"导入失败：{exc}"}
        # restore() 现在返回 dict（含 ok/error），失败时别报成功
        if isinstance(result, dict) and result.get("ok") is False:
            reason = result.get("error") or result.get("message") or "导入失败"
            return {"status": "error", "ok": False, "message": str(reason), "data": result}
        return {"status": "ok", "ok": True, "message": "导入完成", "data": result}

    ctx.register_web_api(f"{p}/backups", backup_list, ["GET"], "备份列表")
    ctx.register_web_api(f"{p}/backup/create", backup_create, ["POST"], "立即备份")
    ctx.register_web_api(f"{p}/backup/restore", backup_restore, ["POST"], "恢复备份")
    ctx.register_web_api(f"{p}/backup/delete", backup_delete, ["POST"], "删除备份")
    ctx.register_web_api(f"{p}/backup/download", backup_download, ["GET"], "下载备份")
    ctx.register_web_api(f"{p}/backup/import", backup_import, ["POST"], "导入备份")

    # --------------------------------------------------------------- config
    async def config_get():
        return {
            "status": "ok",
            "data": {
                "sections": {key: plugin._section(key) for key in SECTIONS},
                "titles": SECTION_TITLES,
                "meta": _schema_meta(),
            },
        }

    async def config_save():
        body = await _json_body()
        section = str(body.get("section") or "").strip()
        values = body.get("values")
        if section not in SECTIONS or not isinstance(values, dict):
            return {"status": "error", "ok": False, "message": "section 或 values 不对"}
        fields = _section_fields().get(section) or {}
        if not fields:
            return {
                "status": "error",
                "ok": False,
                "message": f"没找到 {SECTION_TITLES.get(section, section)} 的字段定义（_conf_schema.json）",
            }
        cleaned: dict = {}
        rejected: list = []
        # 先全部校验/转换，都处理完再一次性落盘，免得写一半
        for key, value in values.items():
            spec = fields.get(str(key))
            if spec is None:
                rejected.append({"key": str(key), "reason": "unknown"})
                continue
            try:
                cleaned[str(key)] = _coerce_field(value, spec, str(key))
            except Exception as exc:
                rejected.append({"key": str(key), "reason": str(exc)})
        if not cleaned:
            names = "、".join(item["key"] for item in rejected[:5]) or "？"
            return {
                "status": "error",
                "ok": False,
                "message": f"没有能保存的字段（{names}）",
                "rejected": rejected,
            }
        try:
            plugin._set_section(section, cleaned)
        except Exception as exc:
            logger.warning("[dafeiyu_core] 保存页面配置失败: %s", exc)
            return {"status": "error", "ok": False, "message": f"保存失败：{exc}", "rejected": rejected}
        message = f"已保存 {SECTION_TITLES.get(section, section)} 配置"
        if rejected:
            names = "、".join(item["key"] for item in rejected[:5])
            message += f"；{len(rejected)} 项没写入（{names}）"
        return {"status": "ok", "ok": True, "message": message, "rejected": rejected}

    ctx.register_web_api(f"{p}/config", config_get, ["GET"], "读取插件配置")
    ctx.register_web_api(f"{p}/config/save", config_save, ["POST"], "保存插件配置")

    # -------------------------------------------------------------- context
    async def context_status():
        umo = (request.query.get("umo") or "").strip()
        guard = plugin._guard
        if guard is None:
            return {"status": "error", "message": "上下文守护没准备好"}
        status = await guard.status(umo or None)
        try:
            data = await guard.conversation_rows()
            rows = data.get("rows") or []
            status["conversations"] = rows
            status["conversation_total"] = data.get("total") or 0
            status["report"] = guard._report_lines(data)
            status["stats"] = {
                "total": data.get("total") or 0,
                "groups": sum(1 for row in rows if row.get("kind") == "group"),
                "privates": sum(1 for row in rows if row.get("kind") != "group"),
                "compressed": sum(1 for row in rows if row.get("summary")),
                "near_limit": sum(1 for row in rows if (row.get("ratio") or 0) >= float(guard.cfg.get("warn_ratio", 0.75) or 0.75)),
                "messages": sum(int(row.get("messages") or 0) for row in rows),
            }
        except Exception as exc:
            logger.warning("[dafeiyu_core] 页面读取每个对话上下文失败: %s", exc)
            status["report"] = []
            status["conversations"] = []
            status["stats"] = {}
        try:
            status["cost"] = await plugin._cost_feature.overview(plugin)
        except Exception as exc:
            logger.warning("[dafeiyu_core] 页面读取费用统计失败: %s", exc)
            status["cost"] = {"enabled": False, "error": str(exc), "rows": []}
        try:
            status["speaker"] = {
                "enabled": bool(plugin._section("speaker").get("enabled", True)),
                "groups": len(plugin._speaker_feature._LOG),
                "lines": sum(len(items or []) for items in plugin._speaker_feature._LOG.values()),
            }
        except Exception as exc:
            logger.warning("[dafeiyu_core] 页面读取发言记录失败: %s", exc)
            status["speaker"] = {}
        try:
            status["engaged"] = plugin.engaged_status()
        except Exception as exc:
            logger.warning("[dafeiyu_core] 页面读取续聊状态失败: %s", exc)
            status["engaged"] = {}
        try:
            status["sleep"] = plugin._sleep_feature.sleep_status()
        except Exception as exc:
            logger.warning("[dafeiyu_core] 页面读取睡眠状态失败: %s", exc)
            status["sleep"] = {}
        try:
            status["knowledge"] = plugin._knowledge_feature.status()
        except Exception as exc:
            logger.warning("[dafeiyu_core] 页面读取群档案失败: %s", exc)
            status["knowledge"] = {}
        return {"status": "ok", "data": status}

    ctx.register_web_api(f"{p}/context", context_status, ["GET"], "上下文用量")

    logger.info("[dafeiyu_core] 页面接口已注册：%s/*", p)