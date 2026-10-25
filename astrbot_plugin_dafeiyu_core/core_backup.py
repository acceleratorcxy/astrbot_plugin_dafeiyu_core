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
import shutil
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

SCHEMA_VERSION = 1


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

    @property
    def cfg(self) -> dict:
        return self.plugin._section("backup")

    def _keep(self) -> int:
        try:
            return max(1, int(self.cfg.get("keep", 3) or 3))
        except Exception:
            return 3

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
        rows: list[dict] = []
        try:
            cm = self.plugin.context.conversation_manager
            convs, _total = await cm.get_filtered_conversations(page=1, page_size=500, include_history=True)
        except Exception as exc:
            logger.warning("[dafeiyu_core] 取会话失败: %s", exc)
            return rows
        for conv in convs or []:
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

        self.dir.mkdir(parents=True, exist_ok=True)
        filename = f"dafeiyu-backup-{_stamp()}.json"
        path = self.dir / filename
        _write_json(path, data)
        try:
            os.chmod(path, 0o644)
        except Exception:
            pass
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
        self.dir.mkdir(parents=True, exist_ok=True)
        rows: list[dict] = []
        for path in sorted(self.dir.glob("dafeiyu-backup-*.json"), reverse=True):
            try:
                stat = path.stat()
                rows.append(
                    {
                        "filename": path.name,
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
        keep = self._keep()
        files = sorted(self.dir.glob("dafeiyu-backup-*.json"))
        if len(files) <= keep:
            return 0
        for path in files[: len(files) - keep]:
            try:
                path.unlink()
            except Exception as exc:
                logger.warning("[dafeiyu_core] 删除旧备份失败 %s: %s", path, exc)
        return len(files) - keep

    # ---------------------------------------------------------------- restore
    async def restore(self, data: dict) -> dict:
        if not isinstance(data, dict) or "personas" not in data:
            raise ValueError("备份文件格式不对：缺少 personas 字段。")
        ctx = self.plugin.context
        pm = ctx.persona_manager
        result = {"personas": 0, "folders": 0, "profiles": 0, "interests": False}

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
        #     跟兴趣库一样，必须赶在插件配置恢复（热重载会重新从磁盘读）之前写盘
        runtime = data.get("runtime_state") or {}
        if isinstance(runtime, dict) and runtime:
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
            result["runtime"] = restored

        # 5) 插件配置（放最后：会触发插件热重载）
        self_cfg = ((data.get("plugin_configs") or {}).get("self") or {}).get("config")
        if isinstance(self_cfg, dict) and self_cfg:
            try:
                await self.plugin._restore_own_config(self_cfg)
            except Exception as exc:
                logger.warning("[dafeiyu_core] 恢复插件配置失败: %s", exc)

        return result

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

    def _next_run_delay(self) -> float:
        hour = int(self.cfg.get("hour", 3) if self.cfg.get("hour") is not None else 3)
        minute = int(self.cfg.get("minute", 0) or 0)
        now = _dt.datetime.now()
        target = now.replace(hour=max(0, min(23, hour)), minute=max(0, min(59, minute)), second=0, microsecond=0)
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
                merge = getattr(self.plugin, "merge_interests_now", None)
                if merge is not None:
                    try:
                        merged = merge()
                        if merged:
                            logger.info("[dafeiyu_core] 晚间整理合并了 %s 组兴趣", len(merged))
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
