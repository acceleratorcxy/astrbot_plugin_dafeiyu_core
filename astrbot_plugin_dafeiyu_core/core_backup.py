# -*- coding: utf-8 -*-
"""人格资料备份：统一导出 / 导入 / 每晚自动备份（只保留最新 N 份）。

一份备份 JSON 里包含：
- personas           人格（含 system_prompt / begin_dialogs / tools / skills / folder / sort_order）
- persona_folders    人格文件夹
- config_profiles    配置档案（abconf_<id>.json 内容，不含 default）
- profile_routing    配置档案元数据映射 + 会话路由表
- plugin_configs     本插件自己的配置
- interests          兴趣库数据
- meta               时间、版本、条数统计

存储位置：<plugin_data>/astrbot_plugin_dafeiyu_core/backups/dafeiyu-backup-YYYYmmdd-HHMMSS.json
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

SCHEMA_VERSION = 1

# 自动备份的 reason 取值（create(reason=...) 会写进备份文件的 meta）：
# 只有这几种才算"自动/夜间备份"，可以按 keep 轮换；主人手动做的备份不参与轮换
AUTO_REASONS = {"auto", "nightly", "night"}

# 各配置项的保守默认值（schema 里的默认值由主程序给，这里只做兜底）
DEFAULTS = {
    "hour": 3,
    "minute": 0,
    "keep": 3,
    "manual_keep": 20,  # 手动备份最多留几份（超出删最旧的）
}


def _now_ts() -> float:
    return time.time()


def _stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _fmt_size(num: Any) -> str:
    try:
        value = float(num or 0)
    except Exception:
        return "--"
    if value >= 1024 * 1024:
        return f"{value / 1024 / 1024:.1f}MB"
    return f"{value / 1024:.0f}KB"


def _read_json(path: Path | str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: Path | str, data: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def _as_int(value: Any, default: int) -> int:
    """容错取整：配置被写成 "3:00" / "abc" / None 都回落默认值，绝不抛异常。"""
    try:
        if value is None or value == "":
            return default
        text = str(value).strip()
        return int(float(text))
    except Exception:
        return default


_REASON_RE = re.compile(r'"reason"\s*:\s*"([^"]{0,32})"')


def _read_reason(path: Path) -> str:
    """只读文件头几 KB 把 reason 抠出来（备份 JSON 可能几十 MB，不能整份读）。

    抠不到就当"手动备份"处理 —— 宁可保留，也不要误删主人自己做的备份。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
        match = _REASON_RE.search(head.decode("utf-8", "ignore"))
        if match:
            return match.group(1).strip().lower()
    except Exception:
        pass
    return ""


def _persona_to_dict(persona) -> dict:
    return {
        "persona_id": getattr(persona, "persona_id", None),
        "system_prompt": getattr(persona, "system_prompt", "") or "",
        "begin_dialogs": getattr(persona, "begin_dialogs", None),
        "tools": getattr(persona, "tools", None),
        "skills": getattr(persona, "skills", None),
        "custom_error_message": getattr(persona, "custom_error_message", None),
        "folder_id": getattr(persona, "folder_id", None),
        "sort_order": getattr(persona, "sort_order", 0),
    }


def _folder_to_dict(folder) -> dict:
    return {
        "folder_id": getattr(folder, "folder_id", None),
        "name": getattr(folder, "name", ""),
        "parent_id": getattr(folder, "parent_id", None),
        "description": getattr(folder, "description", "") or "",
        "sort_order": getattr(folder, "sort_order", 0),
    }


class BackupManager:
    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.dir = Path(plugin._data_dir) / "backups"
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._bad_time_warned = False

    @property
    def cfg(self) -> dict:
        return self.plugin._section("backup")

    def _keep(self) -> int:
        return max(1, _as_int(self.cfg.get("keep"), int(DEFAULTS["keep"])))

    def _manual_keep(self) -> int:
        """手动备份最多留几份（默认 20，超出删最旧的；自动备份另有 keep 管）。"""
        return max(1, _as_int(self.cfg.get("manual_keep"), int(DEFAULTS["manual_keep"])))

    def _ensure_dir(self) -> Path:
        """备份目录：存在即建，权限统一收紧到 0o700（里面是人格 prompt、全量会话、
        插件配置里的 key、主人 id，不能给同机其它账号读）。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except Exception as exc:
            logger.warning("[dafeiyu_core] 设置备份目录权限失败 %s: %s", self.dir, exc)
        return self.dir

    # --------------------------------------------------- 全量资料（m08466）
    def _collect_runtime_state(self) -> dict[str, Any]:
        base = Path(self.plugin._data_dir)
        mapping = {
            "speaker_log.json": "speaker_log",
            "context_state.json": "context_state",
            "search_state.json": "search_state",
            "sleep_state.json": "sleep_state",
            "knowledge.json": "knowledge",
            "interests.json": "interests_raw",
        }
        out: dict[str, Any] = {}
        for filename, key in mapping.items():
            raw = _read_json(base / filename, None)
            out[key] = raw if raw is not None else {}
        return out

    async def _collect_conversations(self) -> list[dict]:
        """翻页取全量会话（以前只取 page=1 500 条，超出的静静丢掉，跟 README 说的全量不符）。

        上限 MAX_CONVERSATIONS 条防死循环；中途某页出错就带着已取到的返回。
        """
        page_size = 500
        max_rows = 20000
        rows: list[dict] = []
        try:
            cm = self.plugin.context.conversation_manager
        except Exception as exc:
            logger.warning("[dafeiyu_core] 取会话失败: %s", exc)
            return rows
        page = 1
        while len(rows) < max_rows:
            try:
                convs, total = await cm.get_filtered_conversations(
                    page=page, page_size=page_size, include_history=True
                )
            except Exception as exc:
                logger.warning(
                    "[dafeiyu_core] 取第 %s 页会话失败（先带着已取到的 %s 条）: %s",
                    page,
                    len(rows),
                    exc,
                )
                break
            batch = list(convs or [])
            if not batch:
                break
            for conv in batch:
                history: list = []
                try:
                    loaded = json.loads(getattr(conv, "history", "") or "[]")
                    if isinstance(loaded, list):
                        history = loaded
                except Exception:
                    history = []
                rows.append(
                    {
                        "cid": getattr(conv, "cid", ""),
                        "umo": getattr(conv, "user_id", ""),
                        "platform_id": getattr(conv, "platform_id", ""),
                        "title": getattr(conv, "title", ""),
                        "persona_id": getattr(conv, "persona_id", ""),
                        "created_at": getattr(conv, "created_at", 0),
                        "updated_at": getattr(conv, "updated_at", 0),
                        "token_usage": getattr(conv, "token_usage", 0),
                        "history": history,
                    }
                )
                if len(rows) >= max_rows:
                    break
            if len(rows) >= max_rows:
                logger.warning(
                    "[dafeiyu_core] 会话超过 %s 条，本次备份只取前 %s 条", max_rows, max_rows
                )
                break
            try:
                total_n = int(total or 0)
            except Exception:
                total_n = 0
            if total_n and len(rows) >= total_n:
                break
            if len(batch) < page_size:
                break
            page += 1
            if page > 200:  # 硬兜底：绝不给 500×200 以上的无限翻页
                logger.warning("[dafeiyu_core] 会话翻页到第 200 页停下，可能没取全")
                break
        return rows

    def _collect_meme_manager(self) -> dict[str, Any]:
        root = Path(self.plugin._data_dir).parent / "meme_manager"
        out: dict[str, Any] = {
            "root": str(root),
            "registry": _read_json(root / "registry.json", {}) or {},
            "selection_rules": _read_json(root / "selection_rules.json", {}) or {},
            "packs": [],
        }
        packs_dir = root / "packs"
        if not packs_dir.is_dir():
            return out
        for pack in sorted(packs_dir.iterdir()):
            if not pack.is_dir():
                continue
            files: list[dict] = []
            total = 0
            try:
                for path in sorted(pack.rglob("*")):
                    if not path.is_file():
                        continue
                    try:
                        size = path.stat().st_size
                    except Exception:
                        size = 0
                    total += size
                    files.append({"path": str(path.relative_to(pack)), "bytes": size})
            except Exception as exc:
                logger.warning("[dafeiyu_core] 列出表情包文件失败 %s: %s", pack, exc)
            out["packs"].append(
                {
                    "id": pack.name,
                    "manifest": _read_json(pack / "manifest.json", {}) or {},
                    "memes_data": _read_json(pack / "memes_data.json", {}) or {},
                    "file_count": len(files),
                    "bytes": total,
                    "files": files,
                }
            )
        return out

    # ------------------------------------------------------------- collecting
    async def create(self, reason: str = "manual") -> dict:
        ctx = self.plugin.context
        data: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "plugin": self.plugin.PLUGIN_ID,
            "plugin_version": self.plugin.VERSION,
            "created_at": _now_ts(),
            "reason": reason,
        }

        personas = []
        try:
            for item in await ctx.persona_manager.get_all_personas():
                personas.append(_persona_to_dict(item))
        except Exception as exc:
            logger.warning("[dafeiyu_core] 备份人格失败: %s", exc)
        data["personas"] = personas

        folders = []
        try:
            for item in await ctx.persona_manager.get_all_folders():
                folders.append(_folder_to_dict(item))
        except Exception as exc:
            logger.warning("[dafeiyu_core] 备份人格文件夹失败: %s", exc)
        data["persona_folders"] = folders

        profiles: list[dict] = []
        try:
            acm = ctx.astrbot_config_mgr
            for info in acm.get_conf_list():
                if str(info.get("id")) == "default":
                    continue
                profiles.append(
                    {
                        "id": info.get("id"),
                        "name": info.get("name"),
                        "config": _read_json(info.get("path"), {}) or {},
                    }
                )
        except Exception as exc:
            logger.warning("[dafeiyu_core] 备份配置档案失败: %s", exc)
        data["config_profiles"] = profiles

        routing: dict[str, Any] = {}
        try:
            from astrbot.api import sp

            routing["abconf_mapping"] = await sp.global_get("abconf_mapping", {}) or {}
            routing["umop_config_routing"] = await sp.global_get("umop_config_routing", {}) or {}
        except Exception as exc:
            logger.warning("[dafeiyu_core] 备份配置路由失败: %s", exc)
        data["profile_routing"] = routing

        try:
            data["plugin_configs"] = {
                "self": {
                    "config": dict(self.plugin.config or {}),
                }
            }
        except Exception:
            data["plugin_configs"] = {}

        try:
            store = self.plugin._interest_store
            if store is not None:
                data["interests"] = store.data
        except Exception:
            data["interests"] = {}

        try:
            data["runtime_state"] = self._collect_runtime_state()
        except Exception as exc:
            logger.warning("[dafeiyu_core] 备份运行状态失败: %s", exc)
            data["runtime_state"] = {}
        try:
            data["conversations"] = await self._collect_conversations()
        except Exception as exc:
            logger.warning("[dafeiyu_core] 备份会话失败: %s", exc)
            data["conversations"] = []
        try:
            data["meme_manager"] = self._collect_meme_manager()
        except Exception as exc:
            logger.warning("[dafeiyu_core] 备份表情包库失败: %s", exc)
            data["meme_manager"] = {}

        runtime_state = data.get("runtime_state") or {}
        speaker_log = runtime_state.get("speaker_log") or {}
        context_state = runtime_state.get("context_state") or {}
        conversation_rows = data.get("conversations") or []
        meme = data.get("meme_manager") or {}
        meme_packs = meme.get("packs") or []
        interests_data = data.get("interests") or {}
        counts = {
            "personas": len(data["personas"]),
            "persona_folders": len(data["persona_folders"]),
            "config_profiles": len(profiles),
            "interests": len(interests_data.get("interests") or []),
            "candidates": len(interests_data.get("candidates") or []),
            "conversations": len(conversation_rows),
            "messages": sum(len(row.get("history") or []) for row in conversation_rows),
            "speaker_lines": sum(len(v) for v in speaker_log.values() if isinstance(v, list)),
            "context_summaries": len(context_state.get("summaries") or {}),
            "knowledge_groups": len((runtime_state.get("knowledge") or {}).get("groups") or {}),
            "meme_packs": len(meme_packs),
            "meme_files": sum(int(pack.get("file_count") or 0) for pack in meme_packs),
        }
        data["counts"] = counts

        self._ensure_dir()
        filename = f"dafeiyu-backup-{_stamp()}.json"
        path = self.dir / filename
        _write_json(path, data)
        # 里面有主人 id、人格 prompt、全量会话、插件配置里的 key：只给本进程读
        try:
            os.chmod(path, 0o600)
        except Exception as exc:
            logger.warning("[dafeiyu_core] 设置备份文件权限失败 %s: %s", path, exc)
        self.prune()
        logger.info(
            "[dafeiyu_core] 已备份全部资料: %s（人格 %s / 档案 %s / 兴趣 %s / 会话 %s 个 %s 条 / 表情包 %s 包 %s 文件）",
            filename,
            counts["personas"],
            counts["config_profiles"],
            counts["interests"],
            counts["conversations"],
            counts["messages"],
            counts["meme_packs"],
            counts["meme_files"],
        )
        return {"filename": filename, "path": str(path), "counts": counts}

    # ---------------------------------------------------------------- listing
    def list_backups(self) -> list[dict]:
        self._ensure_dir()
        rows: list[dict] = []
        for path in sorted(self.dir.glob("dafeiyu-backup-*.json"), reverse=True):
            try:
                stat = path.stat()
                rows.append(
                    {
                        "filename": path.name,
                        "reason": _read_reason(path),
                        "size": stat.st_size,
                        "created_at": stat.st_mtime,
                        "created_text": _dt.datetime.fromtimestamp(stat.st_mtime).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                    }
                )
            except Exception:
                continue
        return rows

    def latest_path(self) -> Path | None:
        files = sorted(self.dir.glob("dafeiyu-backup-*.json"))
        if not files:
            return None
        return max(files, key=lambda p: p.stat().st_mtime)

    async def latest(self) -> tuple[dict | None, dict]:
        path = self.latest_path()
        if not path:
            return None, {}
        data = _read_json(path, None)
        if not isinstance(data, dict):
            return None, {}
        return data, {"filename": path.name, "created_at": data.get("created_at")}

    def prune(self) -> int:
        """只轮换自动/夜间备份；主人手动做的备份不参与轮换（只给个总数上限）。

        以前是按文件名一刀切只留 keep 份，主人刚在面板点的手动备份会被一起删掉。
        """
        keep = self._keep()
        manual_keep = self._manual_keep()
        try:
            files = sorted(
                self.dir.glob("dafeiyu-backup-*.json"), key=lambda p: p.stat().st_mtime
            )
        except Exception as exc:
            logger.warning("[dafeiyu_core] 轮换旧备份失败: %s", exc)
            return 0
        auto_files: list[Path] = []
        manual_files: list[Path] = []
        for path in files:
            if _read_reason(path) in AUTO_REASONS:
                auto_files.append(path)
            else:
                # 读不到 reason 的老备份一律当手动备份：宁可不删
                manual_files.append(path)
            # 顺手把老备份文件的权限也收紧（历史上是 0o644，同机其它账号能读）
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
        removed = 0
        for path in auto_files[: max(0, len(auto_files) - keep)]:
            if self._drop(path):
                removed += 1
        extra = manual_files[: max(0, len(manual_files) - manual_keep)]
        for path in extra:
            if self._drop(path):
                removed += 1
        if extra:
            logger.info(
                "[dafeiyu_core] 手动备份超过 %s 份，清掉了最旧的 %s 份", manual_keep, len(extra)
            )
        return removed

    def _drop(self, path: Path) -> bool:
        try:
            path.unlink()
            return True
        except Exception as exc:
            logger.warning("[dafeiyu_core] 删除旧备份失败 %s: %s", path, exc)
            return False

    # ---------------------------------------------------------------- restore
    async def restore(self, data: dict) -> dict:
        """恢复备份：整体包一层兜底，中途炸掉也返回 {"ok": False, "error": ...}。

        以前没有事务也没有回滚，中途异常会停在半路、调用方只看到异常，谁也说不清
        恢复到了哪一步。逐条 upsert 本身是可重入的，失败后重跑一次即可。
        """
        if not isinstance(data, dict) or "personas" not in data:
            raise ValueError("备份文件格式不对：缺少 personas 字段。")
        result: dict[str, Any] = {
            "ok": True,
            "personas": 0,
            "folders": 0,
            "profiles": 0,
            "interests": False,
        }
        try:
            await self._restore_impl(data, result)
        except Exception as exc:
            logger.warning("[dafeiyu_core] 恢复备份中途失败（已恢复的部分保留）: %s", exc)
            result["ok"] = False
            result["error"] = f"{type(exc).__name__}: {exc}"
            # 半路失败时运行时状态可能已经写盘、内存还是旧值：尽力再读盘一次
            if not result.get("reloaded") and not result.get("need_manual_reload"):
                try:
                    result.update(await self._reload_runtime(None))
                except Exception as exc2:
                    result["need_manual_reload"] = True
                    result["hint"] = f"自动重载失败（{exc2}），请手动重载插件让恢复的数据生效"
            return result
        return result

    async def _restore_impl(self, data: dict, result: dict) -> None:
        ctx = self.plugin.context
        pm = ctx.persona_manager

        # 1) 文件夹：按 parent 关系建，记录旧 id → 新 id 映射
        folder_map: dict[str, str] = {}
        folders = [f for f in (data.get("persona_folders") or []) if isinstance(f, dict)]
        # 现有文件夹：同名同父级的直接复用，避免每次恢复都堆出一堆重复目录
        existing_by_key: dict[tuple[str, str | None], str] = {}
        try:
            for folder in await pm.get_all_folders():
                key = (
                    str(getattr(folder, "name", "") or "").strip(),
                    getattr(folder, "parent_id", None) or None,
                )
                existing_by_key.setdefault(key, str(getattr(folder, "folder_id", "") or ""))
        except Exception:
            existing_by_key = {}
        pending = list(folders)
        for _ in range(4):
            if not pending:
                break
            rest = []
            for item in pending:
                parent_old = item.get("parent_id")
                if parent_old and parent_old not in folder_map:
                    rest.append(item)
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                target_parent = folder_map.get(parent_old) if parent_old else None
                reused = existing_by_key.get((name, target_parent or None))
                if reused:
                    folder_map[str(item.get("folder_id"))] = reused
                    continue
                try:
                    folder = await pm.create_folder(
                        name,
                        parent_id=target_parent,
                        description=item.get("description") or None,
                        sort_order=int(item.get("sort_order") or 0),
                    )
                    folder_map[str(item.get("folder_id"))] = getattr(folder, "folder_id", "")
                    result["folders"] += 1
                except Exception as exc:
                    logger.warning("[dafeiyu_core] 恢复人格文件夹失败 %s: %s", name, exc)
            pending = rest

        # 2) 人格
        try:
            current = {p.persona_id for p in await pm.get_all_personas()}
        except Exception:
            current = set()
        for item in data.get("personas") or []:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("persona_id") or "").strip()
            if not pid:
                continue
            old_folder = item.get("folder_id")
            folder_id = folder_map.get(str(old_folder)) if old_folder else None
            try:
                if pid in current:
                    await pm.update_persona(
                        pid,
                        system_prompt=item.get("system_prompt") or "",
                        begin_dialogs=item.get("begin_dialogs") or None,
                        tools=item.get("tools", None),
                        skills=item.get("skills", None),
                        custom_error_message=item.get("custom_error_message", None),
                    )
                else:
                    await pm.create_persona(
                        pid,
                        item.get("system_prompt") or "",
                        begin_dialogs=item.get("begin_dialogs") or None,
                        tools=item.get("tools", None),
                        skills=item.get("skills", None),
                        custom_error_message=item.get("custom_error_message", None),
                        folder_id=folder_id,
                        sort_order=int(item.get("sort_order") or 0),
                    )
                if folder_id and pid in current:
                    try:
                        await pm.move_persona_to_folder(pid, folder_id)
                    except Exception:
                        pass
                result["personas"] += 1
            except Exception as exc:
                logger.warning("[dafeiyu_core] 恢复人格 %s 失败: %s", pid, exc)

        # 3) 配置档案
        try:
            from astrbot.core.config.astrbot_config import ASTRBOT_CONFIG_PATH

            conf_dir = Path(ASTRBOT_CONFIG_PATH).parent / "config"
        except Exception:
            conf_dir = Path(self.plugin._data_dir).parent.parent / "config"
        routing = data.get("profile_routing") or {}
        mapping = dict(routing.get("abconf_mapping") or {})
        for item in data.get("config_profiles") or []:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or "").strip()
            if not cid or cid == "default":
                continue
            path = conf_dir / f"abconf_{cid}.json"
            try:
                _write_json(path, item.get("config") or {})
                try:
                    os.chmod(path, 0o644)
                except Exception:
                    pass
                mapping[cid] = {"path": str(path), "name": item.get("name") or cid}
                result["profiles"] += 1
            except Exception as exc:
                logger.warning("[dafeiyu_core] 恢复配置档案 %s 失败: %s", cid, exc)
        try:
            from astrbot.api import sp

            await sp.global_put("abconf_mapping", mapping)
            acm = ctx.astrbot_config_mgr
            try:
                await acm.initialize()
            except Exception:
                pass
            umop = routing.get("umop_config_routing")
            if isinstance(umop, dict) and umop:
                await sp.global_put("umop_config_routing", umop)
                try:
                    await acm.ucr.initialize()
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("[dafeiyu_core] 恢复配置路由失败: %s", exc)

        # 4) 兴趣库（必须在插件配置恢复之前：插件配置恢复会热重载插件，
        #    重载时 initialize() 会新建 InterestStore 从磁盘读；顺序反了会把
        #    刚恢复的兴趣库在内存里覆盖回旧数据）
        interests = data.get("interests")
        if isinstance(interests, dict) and interests.get("interests") is not None:
            try:
                store = self.plugin._interest_store
                if store is not None:
                    store.data = interests
                    store.save()
                    result["interests"] = True
            except Exception as exc:
                logger.warning("[dafeiyu_core] 恢复兴趣库失败: %s", exc)

        # 4.5) 运行时状态（发言记录 / 压缩记忆 / 搜索计数 / 睡眠状态）
        #     跟兴趣库一样，必须赶在"重新读盘"之前写盘
        result["runtime"] = self._write_runtime_state(data.get("runtime_state") or {})

        # 5) 插件配置 + 重新读盘（放最后：会触发插件热重载）
        #    备份里有插件配置就用备份的；老备份/精简备份没带 plugin_configs 时，
        #    读当前配置原样应用一次，只为触发和 _restore_own_config 一样的重载逻辑。
        #    不这么做的话，runtime_state 已经把 context_state.json / speaker_log.json
        #    覆盖成新内容，而运行中的实例内存还是旧值，下一次 _save() 会把旧数据写回。
        self_cfg = ((data.get("plugin_configs") or {}).get("self") or {}).get("config")
        note = await self._reload_runtime(self_cfg)
        result.update(note)
        if note.get("reloaded"):
            # 重载期间旧实例的 _save() 可能又拿内存里的旧值盖了一次盘，
            # 最后再写一遍，保证盘上留下的是备份里的内容
            result["runtime"] = self._write_runtime_state(data.get("runtime_state") or {})

    def _write_runtime_state(self, runtime: Any) -> int:
        """把备份里的运行时状态写回数据目录，返回写成功的文件数。"""
        if not isinstance(runtime, dict) or not runtime:
            return 0
        base = Path(self.plugin._data_dir)
        mapping = (
            ("speaker_log", "speaker_log.json"),
            ("context_state", "context_state.json"),
            ("search_state", "search_state.json"),
            ("sleep_state", "sleep_state.json"),
            ("knowledge", "knowledge.json"),
        )
        restored = 0
        for key, filename in mapping:
            payload = runtime.get(key)
            if not isinstance(payload, (dict, list)) or not payload:
                continue
            try:
                _write_json(base / filename, payload)
                restored += 1
            except Exception as exc:
                logger.warning("[dafeiyu_core] 恢复 %s 失败: %s", filename, exc)
        return restored

    async def _reload_runtime(self, backup_cfg: Any) -> dict:
        """把"重新读盘"拆成独立一步，不管备份里有没有插件配置都走一遍。

        返回 {"reloaded": bool, "need_manual_reload": bool, "hint": str}，
        真的重载不了（拿不到配置 / 重载抛异常）时明确提示"需手动重载插件"。
        """
        note: dict[str, Any] = {"reloaded": False, "need_manual_reload": False, "hint": ""}
        cfg: dict | None = None
        if isinstance(backup_cfg, dict) and backup_cfg:
            cfg = dict(backup_cfg)
        else:
            try:
                current = getattr(self.plugin, "config", None) or {}
                cfg = dict(current) if current else None
            except Exception as exc:
                logger.warning("[dafeiyu_core] 读取当前插件配置失败: %s", exc)
                cfg = None
        restore_own = getattr(self.plugin, "_restore_own_config", None)
        if not isinstance(cfg, dict) or not cfg or restore_own is None:
            note["need_manual_reload"] = True
            note["hint"] = "这次恢复没能自动重载插件，请手动重载插件让恢复的数据生效"
            logger.warning(
                "[dafeiyu_core] 恢复后无法自动重载插件（备份里没有插件配置或当前配置读不到），"
                "已覆盖的运行时状态需要手动重载插件才会生效"
            )
            return note
        try:
            await restore_own(cfg)
            note["reloaded"] = True
            logger.info("[dafeiyu_core] 恢复完成，已重新读盘（插件已重载）")
        except Exception as exc:
            note["need_manual_reload"] = True
            note["hint"] = f"自动重载失败（{exc}），请手动重载插件让恢复的数据生效"
            logger.warning("[dafeiyu_core] 恢复后重载插件失败: %s，需手动重载", exc)
        return note

    async def restore_file(self, filename: str) -> dict:
        path = (self.dir / Path(filename).name).resolve()
        if not str(path).startswith(str(self.dir.resolve())):
            raise ValueError("非法备份文件名。")
        data = _read_json(path, None)
        if not isinstance(data, dict):
            raise ValueError("备份文件解析失败。")
        result = await self.restore(data)
        result["filename"] = path.name
        return result

    def delete(self, filename: str) -> bool:
        path = (self.dir / Path(filename).name).resolve()
        if not str(path).startswith(str(self.dir.resolve())):
            return False
        if not path.exists():
            return False
        path.unlink()
        return True

    def read_backup_bytes(self, filename: str) -> bytes:
        path = (self.dir / Path(filename).name).resolve()
        if not str(path).startswith(str(self.dir.resolve())):
            raise ValueError("非法备份文件名。")
        return path.read_bytes()

    # --------------------------------------------------------------- schedule
    async def _nightly_report(self, result: dict) -> None:
        """每晚整理完把结果发给主人（默认走官方机器人 + markdown 表格）。"""
        try:
            report = getattr(self.plugin, "_report_feature", None)
            if report is None:
                logger.warning("[dafeiyu_core] 汇报模块没准备好，跳过晚间整理报告")
                return
            model = await report.build_model(self.plugin, result=result, top_context=40, top_cost=6)
            model["title"] = "🐋 鲸鲸的每日整理"
            plain = report.render_plain(model)
            markdown = report.render_markdown(model)
            sender = getattr(self.plugin, "send_report", None)
            if sender is not None:
                sent = await sender(plain, None, markdown=markdown)
            else:
                sent = await self.plugin.send_private_to_master(plain, None)
            if sent:
                logger.info("[dafeiyu_core] 今晚整理结果已发到官方机器人: %s", ",".join(sent))
            else:
                logger.warning("[dafeiyu_core] 今晚整理结果发送失败（通道不可用）")
        except Exception as exc:
            logger.warning("[dafeiyu_core] 发送晚间整理报告失败: %s", exc)

    @staticmethod
    def _parse_time_field(raw: Any, default: int, high: int) -> tuple[int, bool]:
        """配置缺失算正常（用默认值），写了非法值才算不正常。"""
        if raw is None or raw == "":
            return default, True
        value = _as_int(raw, -1)
        if value < 0 or value > high:
            return default, False
        return value, True

    def _schedule_time(self) -> tuple[int, int]:
        """容错解析"每晚几点备份"：配置写成 "3:00" / "abc" / 99 都不抛异常。

        以前 int() 直接写在 _next_run_delay 里，配置一写错，_loop 每 10 分钟重试都是
        同一个 ValueError，夜间备份永远不执行。
        """
        hour_default = int(DEFAULTS["hour"])
        minute_default = int(DEFAULTS["minute"])
        hour, hour_ok = self._parse_time_field(self.cfg.get("hour"), hour_default, 23)
        minute, minute_ok = self._parse_time_field(self.cfg.get("minute"), minute_default, 59)
        if not (hour_ok and minute_ok) and not self._bad_time_warned:
            self._bad_time_warned = True
            logger.warning(
                "[dafeiyu_core] 备份时间配置不合法（hour=%r, minute=%r），按默认 %s:%02d 执行",
                self.cfg.get("hour"),
                self.cfg.get("minute"),
                hour,
                minute,
            )
        return hour, minute

    def _next_run_delay(self) -> float:
        hour, minute = self._schedule_time()
        now = _dt.datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += _dt.timedelta(days=1)
        return max(60.0, (target - now).total_seconds())

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                delay = self._next_run_delay()
                logger.info("[dafeiyu_core] 下次自动备份在 %.1f 小时后", delay / 3600)
                await asyncio.sleep(delay)
                if self._stopping:
                    return
                organize = getattr(self.plugin, "organize_interests", None)
                if organize is not None:
                    try:
                        res = await organize()
                        if res.get("ok"):
                            logger.info("[dafeiyu_core] 晚间整理：合并兴趣 %s 组、临时话题 %s 组、并进兴趣 %s 个（%s）", len(res.get("interests") or []), len(res.get("candidates") or []), len(res.get("promoted") or []), res.get("by") or "rule")
                        else:
                            logger.info("[dafeiyu_core] 晚间整理跳过：%s", res.get("message") or "")
                    except Exception as exc:
                        logger.warning("[dafeiyu_core] 晚间整理兴趣失败: %s", exc)
                if bool(self.cfg.get("enabled", True)):
                    result = await self.create(reason="auto")
                    if bool(self.plugin._notify_section().get("nightly_report", True)):
                        await self._nightly_report(result)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("[dafeiyu_core] 自动备份出错: %s", exc)
                await asyncio.sleep(600)

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        try:
            self._task = asyncio.create_task(self._loop())
        except Exception as exc:
            logger.warning("[dafeiyu_core] 启动自动备份失败: %s", exc)

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except Exception:
                pass
