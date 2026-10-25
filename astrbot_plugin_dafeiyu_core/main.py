# -*- coding: utf-8 -*-
"""大肥鱼人格核心（astrbot_plugin_dafeiyu_core）

把原来四个独立插件合并成一个：
- 主人身份与听令规则（owner_feature）
- 群聊关键词唤醒 + 随机搭话（group_wake_feature）
- 人味化输出（humanizer_feature）
- 兴趣机制 + 自主搜索记录（interest_feature）
- 人格资料备份（core_backup）
- 上下文快满提醒 / 确认压缩 / 压缩后恢复人格资料（core_context）
- 统一配置与查看页面（core_page + views/index.html）

设计说明：
- 所有 @filter / @llm_tool 装饰的处理器都必须定义在 main.py（或由 main 里定义的类继承而来），
  因为 AstrBot 只按 metadata.module_path（data.plugins.<dir>.main）绑定和清理 handler。
  子模块里的 handler 在 __init__ 里统一把 handler_module_path 改写到主模块（同 meme_manager 做法）。
- 统一配置用嵌套分段：owner / group_wake / humanizer / interest / backup / context_guard / page。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import functools
import importlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_ID = "astrbot_plugin_dafeiyu_core"
VERSION = "2.3.5"
REPO = "https://github.com/acceleratorcxy/astrbot_plugin_dafeiyu_core"

_DATA_ROOT = Path(get_astrbot_plugin_data_path())
_LEGACY_INTEREST = _DATA_ROOT / "astrbot_plugin_dafeiyu_interest" / "interests.json"


def _load_sibling(name: str):
    """加载同目录子模块（优先相对导入，失败则按文件路径兜底）。"""
    pkg = __package__ or ("data.plugins." + PLUGIN_ID)
    try:
        return importlib.import_module(f"{pkg}.{name}")
    except Exception:
        path = Path(__file__).with_name(f"{name}.py")
        spec = importlib.util.spec_from_file_location(f"{pkg}.{name}", path)
        if spec is None or spec.loader is None:
            raise
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module


owner_feature = _load_sibling("owner_feature")
group_wake_feature = _load_sibling("group_wake_feature")
humanizer_feature = _load_sibling("humanizer_feature")
interest_feature = _load_sibling("interest_feature")
meme_feature = _load_sibling("meme_feature")
search_feature = _load_sibling("search_feature")
speaker_feature = _load_sibling("speaker_feature")
cost_feature = _load_sibling("cost_feature")
sleep_feature = _load_sibling("sleep_feature")
report_feature = _load_sibling("report_feature")
knowledge_feature = _load_sibling("knowledge_feature")
core_backup = _load_sibling("core_backup")
core_context = _load_sibling("core_context")
core_page = _load_sibling("core_page")

SECTIONS = ("owner", "group_wake", "humanizer", "interest", "meme", "search", "speaker", "cost", "sleep", "knowledge", "backup", "context_guard", "notify", "page")

DEFAULT_PRIVATE_UMOS: tuple = ()

LEGACY_CONFIG_FILES = {
    "owner": "astrbot_plugin_dafeiyu_owner_config.json",
    "group_wake": "astrbot_plugin_dafeiyu_group_wake_config.json",
    "humanizer": "astrbot_plugin_dafeiyu_humanizer_config.json",
    "interest": "astrbot_plugin_dafeiyu_interest_config.json",
}

BACKUP_HELP = (
    "用法：/备份 现在 | /备份 列表 | /备份 恢复 文件名 | /备份 删除 文件名\n"
    "（也可以打开 AstrBot 里本插件的「大肥鱼人格」页面下载/导入；每晚睡眠时间（默认 3 点）会做一次全量备份、只留最新 3 份，并把整理结果私聊发给主人）"
)

HELP_COMMANDS = [
    ("/help 或 /命令", "看这张命令表"),
    ("/触发 <动作>", "在私聊里一键让她干活（动作见下）"),
    ("/上下文 [全部]", "看当前会话的上下文用量；加「全部」看每个对话"),
    ("/压缩", "把前面聊的压成一段记忆"),
    ("/报告", "整理报告（备份 + 每个对话上下文 + 费用 + 兴趣）"),
    ("/费用 [N]", "按会话看花了多少钱"),
    ("/睡眠 [醒|睡]", "看睡眠状态；主人能强制叫醒她"),
    ("/备份 现在 | 列表 | 恢复 <文件名> | 删除 <文件名>", "全量备份：人格、配置档案、兴趣、会话、发言记录、压缩状态、表情包清单"),
    ("/主人", "看当前主人设置"),
    ("/兴趣 看 | 加 | 删 | 搜 | 记", "兴趣库：手动加、删、搜、记笔记"),
    ("/群档案 [更新|全部]", "群内黑话和群友昵称档案：每晚自动整理，她聊天时自己会用"),
]

HELP_TRIGGERS = [
    ("报告", "同 /报告"),
    ("备份 [列表|恢复 文件名|删除 文件名]", "同 /备份"),
    ("压缩", "同 /压缩"),
    ("上下文 [全部]", "同 /上下文"),
    ("费用 [N]", "同 /费用"),
    ("兴趣", "兴趣库概览"),
    ("搜索 <要搜什么>", "联网搜一次，结果私聊发你"),
    ("发图 [心情]", "私聊发一张表情包（可带心情，比如 无语、开心）"),
    ("群压缩", "立刻巡检一次群聊自动压缩"),
    ("续聊", "看现在正在续聊的群"),
    ("睡眠 [醒|睡]", "看睡眠状态 / 叫醒 / 让她睡"),
    ("帮助", "看 /触发 的用法"),
]

HELP_SETUP = [
    ("指令入口", "插件页面「配置」→「主人私聊通知」→ command_platforms：允许用指令的机器人（默认只有官方机器人）"),
    ("私聊 / 群聊", "同一段的 command_private_enabled / command_group_enabled：分别开关私聊、群聊里的指令（群聊默认关）"),
    ("只允许主人", "command_master_only 默认开；群聊里认不出主人时，把群成员 ID 填进 command_group_master_ids（/sid 可查）"),
    ("其它配置", "插件页面「配置」标签共 13 段，改完保存立即生效，不用重启"),
    ("汇报通道", "report_channel=official 时汇报发到官方机器人、用 markdown 表格；不支持 markdown 的聊天会自动降级成纯文本"),
    ("在哪里看", "AstrBot 面板 → 插件 → 大肥鱼人格核心 → 总览 / 命令 / 兴趣 / 备份 / 配置 / 上下文"),
]


def help_markdown() -> str:
    """给支持 markdown 的机器人看的命令说明。"""
    bt = chr(96)
    lines = ["## 🐋 鲸鲸的命令", "", "> 只认主人；群里发这些，她只私聊回你。", ""]
    lines += ["### 🎯 常用指令", "", "| 命令 | 说明 |", "| --- | --- |"]
    for cmd, desc in HELP_COMMANDS:
        lines.append("| " + bt + cmd + bt + " | " + desc + " |")
    lines += ["", "### 🛠 /触发 的动作", "", "| 动作 | 说明 |", "| --- | --- |"]
    for cmd, desc in HELP_TRIGGERS:
        lines.append("| " + bt + cmd + bt + " | " + desc + " |")
    lines += ["", "### ⚙️ 怎么设置", "", "| 项目 | 怎么配 |", "| --- | --- |"]
    for key, desc in HELP_SETUP:
        lines.append("| **" + key + "** | " + desc + " |")
    lines += ["", "### 💡 例子", "", bt * 3,
              "/触发 报告", "/触发 搜索 英雄联盟 S16 今天赛程",
              "/触发 发图 无语", "/触发 群压缩", bt * 3]
    return chr(10).join(lines)


def help_plain() -> str:
    """不支持 markdown 的聊天（个人 QQ 等）用的纯文本版本。"""
    lines = ["【鲸鲸的命令】只认主人；群里发这些，她只私聊回你。", "", "【常用指令】"]
    for cmd, desc in HELP_COMMANDS:
        lines.append("  · " + cmd + "：" + desc)
    lines += ["", "【/触发 的动作】"]
    for cmd, desc in HELP_TRIGGERS:
        lines.append("  · " + cmd + "：" + desc)
    lines += ["", "【怎么设置】"]
    for key, desc in HELP_SETUP:
        lines.append("  · " + key + "：" + desc)
    lines += ["", "【例子】", "  · /触发 报告", "  · /触发 搜索 英雄联盟 S16 今天赛程",
              "  · /触发 发图 无语", "  · /触发 群压缩"]
    return chr(10).join(lines)


TRIGGER_TEXT = """用法：/触发 <动作> [参数]

报告 | 备份 [列表|恢复 文件名|删除 文件名] | 压缩 | 上下文 [全部] | 费用 [N]
兴趣 | 搜索 <要搜什么> | 发图 [心情] | 群压缩 | 续聊 | 睡眠 [醒|睡] | 帮助

例子：
/触发 报告
/触发 搜索 英雄联盟 S16 今天赛程
/触发 发图 无语
/触发 群压缩

（这些命令都只在私聊里回你，不会发到群里。）
设置入口：插件页面「配置」→「主人私聊通知」，指令入口、私聊/群聊开关、只允许主人都在那一段。"""



class HelpAliasFilter(filter.CustomFilter):
    """把 /help 抢在 AstrBot 内置 help 前面处理。

    内置 help 是保留命令，会先回它自己的清单并把事件停掉，导致 /help 看不到我们的表。
    """

    def filter(self, event: AstrMessageEvent, cfg=None) -> bool:
        try:
            text = (event.message_str or "").strip()
            for prefix in ("/", "／"):
                if text.startswith(prefix):
                    text = text[len(prefix):].strip()
                    break
            if not text:
                return False
            first = text.split()[0].lower()
            return first in ("help",)
        except Exception:
            return False


@register(
    PLUGIN_ID,
    "dsh",
    "大肥鱼人格核心：认主 + 兴趣 + 群聊唤醒 + 人味化 + 人格备份 + 上下文压缩，统一配置与查看页面",
    VERSION,
    REPO,
)
class Main(Star, owner_feature.OwnerFeature, group_wake_feature.GroupWakeFeature,
           humanizer_feature.HumanizerFeature, interest_feature.InterestFeature,
           meme_feature.MemeFeature, search_feature.SearchFeature,
           speaker_feature.SpeakerFeature, sleep_feature.SleepFeature,
           knowledge_feature.KnowledgeFeature):
    PLUGIN_ID = PLUGIN_ID
    VERSION = VERSION

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._normalize_mixin_handler_module_paths()
        self._data_dir = _DATA_ROOT / PLUGIN_ID
        self._interest_store = None
        self._backup = None
        self._guard = None
        self._cost_feature = cost_feature
        self._report_feature = report_feature
        self._speaker_feature = speaker_feature
        self._sleep_feature = sleep_feature
        self._knowledge_feature = knowledge_feature
        self._apply_all_runtime()

    # ---------------------------------------------------------------- config
    def _section(self, name: str) -> dict:
        try:
            value = self.config.get(name)
        except Exception:
            value = None
        if isinstance(value, dict):
            return value
        return {}

    def _set_section(self, name: str, values: dict) -> dict:
        current = dict(self._section(name))
        for key, value in (values or {}).items():
            current[key] = value
        try:
            self.config[name] = current
            self.config.save_config()
        except Exception as exc:
            logger.warning("[dafeiyu_core] 保存配置失败: %s", exc)
        self._apply_all_runtime()
        return current

    def _apply_all_runtime(self) -> None:
        owner_feature._apply_runtime(self._section("owner"))
        group_wake_feature._apply_runtime(self._section("group_wake"))
        interest_feature._apply_runtime(self._section("interest"))
        meme_feature._apply_runtime(self._section("meme"))
        search_feature._apply_runtime(self._section("search"))
        speaker_feature._apply_runtime(self._section("speaker"))
        cost_feature._apply_runtime(self._section("cost"))
        sleep_feature._apply_runtime(self._section("sleep"))
        report_feature._apply_runtime(self._section("notify"))
        knowledge_feature._apply_runtime(self._section("knowledge"))

    def _cfg(self, key: str, default=None):
        """humanizer_feature 会调用 self._cfg，这里改成读 humanizer 分段。"""
        section = self._section("humanizer")
        value = section.get(key)
        if value is None:
            return humanizer_feature.DEFAULTS.get(key, default)
        return value

    # ------------------------------------------------------------- 私聊通知
    def _notify_section(self) -> dict:
        return self._section("notify")

    def _private_targets(self, event: AstrMessageEvent | None = None) -> list[str]:
        """主人私聊的 umo 列表（优先当前平台）。"""
        raw = self._notify_section().get("private_umos")
        if not isinstance(raw, list) or not raw:
            raw = list(DEFAULT_PRIVATE_UMOS)
        targets = [str(item).strip() for item in raw if str(item).strip()]
        if event is not None:
            try:
                platform = str(event.get_platform_name() or "")
            except Exception:
                platform = ""
            if platform:
                matched = [u for u in targets if u.startswith(platform + ":")]
                matched.sort(key=lambda u: 0 if ":FriendMessage:" in u else 1)
                if matched:
                    others = [u for u in targets if not u.startswith(platform + ":")]
                    return matched + others
        return targets

    def _is_master_private(self, event: AstrMessageEvent) -> bool:
        try:
            if not event.is_private_chat():
                return False
        except Exception:
            return False
        return bool(event.is_admin())

    async def send_private_to_master(self, text: str, event: AstrMessageEvent | None = None) -> list[str]:
        """把内容私聊发给主人，返回成功的 umo 列表。

        - 当前就是主人私聊：直接回在当前会话。
        - 否则用 context.send_message 主动私聊（aiocqhttp 可用；qq_official 平台不支持主动消息）。
        - notify.private_only=false 时才允许退回当前会话发送。
        """
        text = str(text or "").strip()
        if not text:
            return []
        private_only = bool(self._notify_section().get("private_only", True))
        if event is not None and self._is_master_private(event):
            try:
                await event.send(MessageChain().message(text))
                return [getattr(event, "unified_msg_origin", "current")]
            except Exception as exc:
                logger.warning("[dafeiyu_core] 私聊回复失败: %s", exc)
        sent: list[str] = []
        for target in self._private_targets(event):
            try:
                ok = await self.context.send_message(target, MessageChain().message(text))
                if ok:
                    sent.append(target)
            except Exception as exc:
                logger.warning(
                    "[dafeiyu_core] 主动私聊主人失败 %s: %s(%r)，改用直连协议端",
                    target,
                    type(exc).__name__,
                    exc,
                )
                if await self._send_via_platform(target, text):
                    sent.append(target)
            if sent:
                break
        if sent:
            return sent
        if not private_only and event is not None:
            try:
                await event.send(MessageChain().message(text))
                return ["current"]
            except Exception as exc:
                logger.warning("[dafeiyu_core] 当前会话发送失败: %s", exc)
        return []

    async def _send_via_platform(self, target: str, text: str) -> bool:
        """兜底：绕过 context.send_message，直接找平台实例私聊发主人。"""
        return await self._direct_send(target, text)

    async def send_to_umo(self, umo: str, text: str) -> bool:
        """给任意会话（私聊/群聊）发一条文本：先走框架接口，失败再直连协议端。"""
        text = str(text or "").strip()
        if not text or not umo:
            return False
        try:
            ok = await self.context.send_message(umo, MessageChain().message(text))
            if ok:
                return True
        except Exception as exc:
            logger.warning(
                "[dafeiyu_core] 框架发消息失败 %s: %s(%r)",
                umo,
                type(exc).__name__,
                exc,
            )
        return await self._direct_send(umo, text)

    async def _direct_send(self, target: str, text: str) -> bool:
        """直接调协议端发送（私聊/群聊都支持）。

        多连接（例如同时挂着多个 OneBot 客户端）时 aiocqhttp 的 call_action
        会因为 api client 不唯一而抛 ApiNotAvailable；这里显式指定 self_id
        逐个 api client 尝试。
        """
        try:
            session = MessageSesion.from_str(target)
        except Exception:
            return False
        platform_id = str(getattr(session, "platform_name", "") or "")
        user_id = str(getattr(session, "session_id", "") or "")
        if not platform_id or not user_id.isdigit():
            return False
        msg_type = str(getattr(session, "message_type", "") or "")
        is_group = (":GroupMessage:" in str(target or "")) or ("Group" in msg_type)
        action = "send_group_msg" if is_group else "send_private_msg"
        id_key = "group_id" if is_group else "user_id"
        action_label = "群聊" if is_group else "私聊"
        for inst in self.context.platform_manager.platform_insts:
            try:
                if inst.meta().id != platform_id:
                    continue
            except Exception:
                continue
            bot = getattr(inst, "bot", None)
            if bot is None:
                return False
            api_clients = (
                getattr(bot, "_wsr_api_clients", None)
                or getattr(bot, "_api_clients", None)
                or {}
            )
            if not isinstance(api_clients, dict) or not api_clients:
                return False
            for self_id in list(api_clients.keys()):
                try:
                    payload = {
                        id_key: int(user_id),
                        "message": [{"type": "text", "data": {"text": text}}],
                        "self_id": self_id,
                    }
                    await bot.call_action(action, **payload)
                    logger.info(
                        "[dafeiyu_core] 直连协议端%s成功 %s (self_id=%s)",
                        action_label,
                        target,
                        self_id,
                    )
                    return True
                except Exception as exc:
                    logger.warning(
                        "[dafeiyu_core] 直连协议端%s失败 %s (self_id=%s): %s(%r)",
                        action_label,
                        target,
                        self_id,
                        type(exc).__name__,
                        exc,
                    )
        return False

    async def answer_private(self, event: AstrMessageEvent, text: str, hint: str = "这个只私聊发你。") -> list:
        """敏感信息只走私聊：返回给当前会话的提示（列表）。"""
        sent = await self.send_private_to_master(text, event)
        if not sent:
            return [event.plain_result("私聊发不出去（平台不支持主动消息），内容就不往这里发了，看看日志。")]
        if self._is_master_private(event):
            return []
        return [event.plain_result(hint)]

    async def send_report(
        self, text: str, event: AstrMessageEvent | None = None, markdown: str | None = None
    ) -> list[str]:
        """汇报类内容统一投递：默认走官方机器人 + markdown，失败退回原私聊通道。"""
        try:
            return await report_feature.send(self, text, event, markdown=markdown)
        except Exception as exc:
            logger.warning("[dafeiyu_core] 汇报投递失败: %s(%r)", type(exc).__name__, exc)
            return await self.send_private_to_master(text, event)

    def _report_hint(self, event: AstrMessageEvent | None, hint: str) -> list:
        """汇报已在别的通道发出后，只在私聊里回一句提示（群里不吭声）。"""
        try:
            if event is not None and event.is_private_chat():
                return [event.plain_result(hint)]
        except Exception:
            return []
        return []

    async def _restore_own_config(self, config: dict) -> None:
        try:
            self.config.clear()
            self.config.update(config)
            self.config.save_config()
        except Exception as exc:
            logger.warning("[dafeiyu_core] 写回插件配置失败: %s", exc)
            return
        self._apply_all_runtime()
        try:
            star_manager = getattr(self.context, "_star_manager", None)
            if star_manager is not None:
                await star_manager.reload(PLUGIN_ID)
        except Exception as exc:
            logger.warning("[dafeiyu_core] 重载插件失败: %s", exc)

    # ------------------------------------------------------------ lifecycle
    async def initialize(self):
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning("[dafeiyu_core] 创建数据目录失败: %s", exc)
        try:
            speaker_feature._load(self)
        except Exception as exc:
            logger.warning("[dafeiyu_core] 读取群聊发言记录失败: %s", exc)
        self._migrate_legacy_config()
        self._apply_all_runtime()
        self._wrap_command_handlers()
        self._normalize_llm_tools()
        self._migrate_interest_data()
        try:
            search_feature._load_state(self)
        except Exception as exc:
            logger.warning("[dafeiyu_core] 初始化搜索状态失败: %s", exc)
        try:
            sleep_feature._load_state(self)
        except Exception as exc:
            logger.warning("[dafeiyu_core] 初始化睡眠状态失败: %s", exc)

        interest_feature._CTX = self.context
        try:
            self._interest_store = interest_feature.InterestStore(self._data_dir / "interests.json")
        except Exception as exc:
            logger.warning("[dafeiyu_core] 初始化兴趣库失败: %s", exc)
            self._interest_store = None
        interest_feature._STORE = self._interest_store
        try:
            if self._interest_store is not None:
                self._interest_store.prune()
            interest_feature.start_background(self._section("interest").get("prune_interval_seconds") or 3600)
        except Exception as exc:
            logger.warning("[dafeiyu_core] 启动兴趣清理失败: %s", exc)

        self._backup = core_backup.BackupManager(self)
        self._guard = core_context.ContextGuard(self)
        try:
            self._guard.start_background()
        except Exception as exc:
            logger.warning("[dafeiyu_core] 启动群聊自动压缩失败: %s", exc)
        try:
            sleep_feature.start_background()
        except Exception as exc:
            logger.warning("[dafeiyu_core] 启动睡眠模式失败: %s", exc)
        try:
            knowledge_feature._load(self)
            knowledge_feature._bind_path(self)
            knowledge_feature.start_background(self)
        except Exception as exc:
            logger.warning("[dafeiyu_core] 启动群档案整理失败: %s", exc)
        if bool(self._section("backup").get("enabled", True)):
            self._backup.start()

        try:
            core_page.register_page_apis(self)
        except Exception as exc:
            logger.warning("[dafeiyu_core] 注册页面接口失败: %s", exc)

        logger.info(
            "[dafeiyu_core] v%s 已加载：主人=%s | 兴趣 %s 条 | 自动备份=%s 保留%s份 | 上下文提醒=%s | 群聊自动压缩=%s | 群聊发言记录=%s",
            VERSION,
            owner_feature._RUNTIME.get("master_name"),
            len(self._interest_store.interests()) if self._interest_store else 0,
            self._section("backup").get("enabled", True),
            self._section("backup").get("keep", 3),
            self._section("context_guard").get("enabled", True),
            self._section("context_guard").get("group_auto_compress", True),
            self._section("speaker").get("enabled", True),
        )
        logger.info(
            "[dafeiyu_core] 睡眠模式=%s（安静 %s 点 / 睡 %s 点 / 醒 %s 点+%s 分）",
            self._section("sleep").get("enabled", True),
            self._section("sleep").get("quiet_hour", 1),
            self._section("sleep").get("sleep_hour", 2),
            self._section("sleep").get("wake_hour", 6),
            self._section("sleep").get("wake_jitter_minutes", 45),
        )
        try:
            kstatus = knowledge_feature.status()
        except Exception:
            kstatus = {}
        logger.info(
            "[dafeiyu_core] 群档案=%s（%s 个群 / 黑话 %s 条 / 每晚 %s:%02s 整理）",
            self._section("knowledge").get("enabled", True),
            kstatus.get("groups", 0),
            kstatus.get("slang", 0),
            self._section("knowledge").get("nightly_hour", 3),
            self._section("knowledge").get("nightly_minute", 40),
        )

    async def terminate(self):
        try:
            knowledge_feature.stop_background()
        except Exception:
            pass
        try:
            sleep_feature.stop_background()
        except Exception:
            pass
        try:
            if self._guard is not None:
                self._guard.stop_background()
        except Exception:
            pass
        try:
            if self._backup is not None:
                await self._backup.stop()
        except Exception:
            pass
        try:
            speaker_feature._save(force=True)
        except Exception:
            pass
        for task in list(interest_feature._TASKS):
            try:
                task.cancel()
            except Exception:
                pass
        logger.info("[dafeiyu_core] 插件已卸载")

    # ---------------------------------------------------------- migrations
    def _migrate_legacy_config(self) -> None:
        marker = self._data_dir / "migrated_from_v1.json"
        if marker.exists():
            return
        config_dir = Path(get_astrbot_plugin_data_path()).parent / "config"
        merged: dict[str, dict] = {}
        for section, filename in LEGACY_CONFIG_FILES.items():
            path = config_dir / filename
            old = None
            for encoding in ("utf-8-sig", "utf-8"):
                try:
                    with open(path, "r", encoding=encoding) as f:
                        old = json.load(f)
                    break
                except Exception:
                    old = None
            if not isinstance(old, dict):
                continue
            current = dict(self._section(section))
            for key, value in old.items():
                current[key] = value
            merged[section] = current
        if merged:
            try:
                for section, values in merged.items():
                    self.config[section] = values
                self.config.save_config()
                logger.info("[dafeiyu_core] 已迁移旧插件配置：%s", ",".join(merged.keys()))
            except Exception as exc:
                logger.warning("[dafeiyu_core] 迁移旧插件配置失败: %s", exc)
                return
        try:
            marker.write_text(json.dumps({"at": __import__("time").time(), "sections": list(merged.keys())}, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _migrate_interest_data(self) -> None:
        target = self._data_dir / "interests.json"
        if target.exists():
            return
        if not _LEGACY_INTEREST.exists():
            return
        try:
            shutil.copy2(_LEGACY_INTEREST, target)
            try:
                os.chmod(target, 0o644)
            except Exception:
                pass
            logger.info("[dafeiyu_core] 已迁移旧兴趣库: %s", target)
        except Exception as exc:
            logger.warning("[dafeiyu_core] 迁移兴趣库失败: %s", exc)

    # -------------------------------------------------------- page helpers
    async def interest_search_results(self, topic: str, top_k: int = 3) -> list:
        try:
            return await interest_feature._baidu_search(topic, top_k)
        except Exception as exc:
            logger.warning("[dafeiyu_core] 搜索兴趣失败: %s", exc)
            return []

    def interest_note_text(self, result) -> str:
        try:
            return interest_feature._note_text(result)
        except Exception:
            return str((result or {}).get("title") or "")

    def merge_interests_now(self) -> list:
        """把很像的兴趣合并成一条（晚上整理时自动跑，页面和 /兴趣 整理 也能手动触发）。"""
        store = getattr(self, "_interest_store", None)
        if store is None:
            return []
        try:
            if not bool(interest_feature._RUNTIME.get("merge_enabled", True)):
                return []
            return store.merge_similar()
        except Exception as exc:
            logger.warning("[dafeiyu_core] 合并兴趣失败: %s", exc)
            return []

    def interest_now(self) -> float:
        try:
            return interest_feature._now()
        except Exception:
            return 0.0

    def engaged_status(self) -> dict:
        """给页面和 /触发 续聊 用：现在正在续聊的群。"""
        try:
            return group_wake_feature.engaged_status()
        except Exception:
            return {
                "enabled": False,
                "count": 0,
                "groups": {},
                "max_turns": 0,
                "window_seconds": 0,
                "probability": 0,
            }

    # ------------------------------------------------- handler normalization
    @classmethod
    def _normalize_mixin_handler_module_paths(cls) -> None:
        plugin_package = (cls.__module__ or "").rsplit(".", 1)[0]
        if not plugin_package:
            return
        prefixes = tuple(f"{plugin_package}.{name}" for name in ("owner_feature", "group_wake_feature", "humanizer_feature", "interest_feature", "meme_feature", "search_feature", "speaker_feature", "cost_feature", "sleep_feature", "knowledge_feature"))
        adjusted = 0
        try:
            from astrbot.core.star.star_handler import star_handlers_registry

            for handler in star_handlers_registry:
                path = getattr(handler, "handler_module_path", "") or ""
                if path.startswith(prefixes):
                    handler.handler_module_path = cls.__module__
                    adjusted += 1
        except Exception as exc:
            logger.warning("[dafeiyu_core] 归一化指令处理器失败: %s", exc)
        if adjusted:
            logger.info("[dafeiyu_core] 已把 %s 个子模块处理器绑定到主模块", adjusted)

    def _command_platforms(self) -> tuple[str, ...]:
        """允许用指令的机器人（平台 id），其他平台发指令一律不执行。"""
        try:
            items = self._section("notify").get("command_platforms")
        except Exception:
            items = None
        if isinstance(items, str):
            items = [x.strip() for x in items.replace("，", ",").split(",") if x.strip()]
        if not isinstance(items, (list, tuple)) or not items:
            return ("*",)
        return tuple(str(x).strip() for x in items if str(x).strip())

    def _command_flag(self, key: str, default: bool) -> bool:
        """读 notify 段里的布尔开关，没配就用默认值。"""
        try:
            value = self._section("notify").get(key)
        except Exception:
            value = None
        if value is None or value == "":
            return default
        if isinstance(value, str):
            return value.strip().lower() not in ("false", "0", "no", "off", "")
        return bool(value)

    def _command_master_only(self) -> bool:
        return self._command_flag("command_master_only", True)

    def _command_group_master_ids(self) -> set[str]:
        """群聊里额外认作主人的成员 ID（QQ 官方的群成员 ID 和私聊 openid 不一样）。"""
        try:
            items = self._section("notify").get("command_group_master_ids")
        except Exception:
            items = None
        if isinstance(items, str):
            items = items.replace("，", ",").split(",")
        if not isinstance(items, (list, tuple)):
            return set()
        return {str(x).strip() for x in items if str(x).strip()}

    def _is_master_event(self, event) -> bool:
        """这条消息是不是主人发的。关掉「只允许主人」就谁都可以。"""
        try:
            if bool(event.is_admin()):
                return True
        except Exception:
            pass
        if not self._command_master_only():
            return True
        try:
            if not event.get_group_id():
                return False
            sender = str(event.get_sender_id())
        except Exception:
            return False
        return sender in self._command_group_master_ids()

    def _command_gate(self, event) -> tuple[bool, str]:
        """(能不能用指令, 不能用的原因: platform/channel/master)。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        platform = umo.split(":", 1)[0].strip()
        allowed = self._command_platforms()
        if allowed and "*" not in allowed and platform not in allowed:
            return False, "platform"
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            is_private = False
        if is_private:
            if not self._command_flag("command_private_enabled", True):
                return False, "channel"
        else:
            if not self._command_flag("command_group_enabled", False):
                return False, "channel"
        if not self._is_master_event(event):
            return False, "master"
        return True, ""

    def _command_allowed(self, event) -> bool:
        """这条消息所在的机器人/会话是否允许触发指令。"""
        return self._command_gate(event)[0]

    def _deny_command_here(self, event, reason: str = "platform") -> None:
        """指令不在这里执行：群里不吭声，私聊提示一句。"""
        try:
            event.should_call_llm(True)
        except Exception:
            pass
        texts = {
            "platform": "指令得在官方机器人那边发，这边我只跟你聊天。",
            "channel": "这边不让用指令。",
            "master": "指令只给主人用。",
        }
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            is_private = False
        if is_private:
            try:
                event.set_result(event.plain_result(texts.get(reason) or texts["platform"]))
            except Exception:
                pass
        try:
            event.stop_event()
        except Exception:
            pass

    def _wrap_command_handlers(self) -> None:
        """让指令处理完就停掉事件，避免同一条指令又被大模型闲聊回一遍。"""
        import inspect as _inspect

        try:
            from astrbot.core.star.filter.command import CommandFilter
            from astrbot.core.star.star_handler import EventType, star_handlers_registry
        except Exception:
            return

        def _block_default_llm(event) -> None:
            """指令只是指令：立刻关掉默认 LLM 链路，免得同一条指令又被闲聊回一遍。"""
            try:
                event.should_call_llm(True)
            except Exception:
                try:
                    event.call_llm = True
                except Exception:
                    pass

        def _wrap(inner):
            if _inspect.isasyncgenfunction(inner):

                @functools.wraps(inner)
                async def gen(event, *args, **kwargs):
                    ok, reason = self._command_gate(event)
                    if not ok:
                        self._deny_command_here(event, reason)
                        return
                    _block_default_llm(event)
                    async for item in inner(event, *args, **kwargs):
                        yield item
                    try:
                        event.stop_event()
                    except Exception as exc:
                        logger.warning("[dafeiyu_core] 终止指令事件失败: %s(%r)", type(exc).__name__, exc)

                gen._dafeiyu_stops_event = True
                return gen

            @functools.wraps(inner)
            async def plain(event, *args, **kwargs):
                ok, reason = self._command_gate(event)
                if not ok:
                    self._deny_command_here(event, reason)
                    return None
                _block_default_llm(event)
                result = await inner(event, *args, **kwargs)
                try:
                    event.stop_event()
                except Exception:
                    pass
                return result

            plain._dafeiyu_stops_event = True
            return plain

        wrapped = 0
        try:
            for md in star_handlers_registry:
                if getattr(md, "handler_module_path", "") != self.__class__.__module__:
                    continue
                if getattr(md, "event_type", None) != EventType.AdapterMessageEvent:
                    continue
                filters = getattr(md, "event_filters", []) or []
                if not any(isinstance(f, CommandFilter) for f in filters):
                    continue
                if getattr(md.handler, "_dafeiyu_stops_event", False):
                    continue
                md.handler = _wrap(md.handler)
                wrapped += 1
        except Exception as exc:
            logger.warning("[dafeiyu_core] 指令事件终止接管失败: %s", exc)
            return
        if wrapped:
            logger.info("[dafeiyu_core] 已接管 %s 个指令的事件终止（指令不再被闲聊复读）", wrapped)

    def _normalize_llm_tools(self) -> None:
        """把兄弟模块里定义的 @llm_tool 绑定到插件实例。

        AstrBot 只自动绑定「handler.__module__ == 主模块」的工具；兄弟模块定义的
        工具不会被绑定，调用时会报 missing positional argument: 'event'。
        这里手动 partial(self) 绑定，并把 handler_module_path 指向主模块，
        star_manager 随后会按「partial + module_path 命中」再包一层实例，结果仍是绑定方法。
        """
        plugin_package = (self.__class__.__module__ or "").rsplit(".", 1)[0]
        prefixes = tuple(f"{plugin_package}.{name}" for name in ("owner_feature", "group_wake_feature", "humanizer_feature", "interest_feature", "meme_feature", "search_feature", "speaker_feature", "cost_feature", "sleep_feature", "knowledge_feature"))
        try:
            tmgr = self.context.get_llm_tool_manager()
        except Exception as exc:
            logger.warning("[dafeiyu_core] 取 LLM 工具管理器失败: %s", exc)
            return

        def _concrete(items):
            for item in items:
                inner = getattr(item, "_wrapped", None)
                yield item if inner is None else inner
                if inner is not None:
                    yield inner

        adjusted = 0
        for tool in list(_concrete(list(getattr(tmgr, "func_list", []) or []))):
            handler = getattr(tool, "handler", None)
            if handler is None:
                continue
            raw = handler.func if isinstance(handler, functools.partial) else handler
            raw_module = getattr(raw, "__module__", "") or ""
            path = getattr(tool, "handler_module_path", "") or raw_module
            if not (path.startswith(prefixes) or raw_module.startswith(prefixes)):
                continue
            tool.handler = functools.partial(raw, self)
            tool.handler_module_path = self.__class__.__module__
            adjusted += 1
        if adjusted:
            logger.info("[dafeiyu_core] 已把 %s 个 LLM 工具绑定到主模块", adjusted)

    # ------------------------------------------------------------- handlers
    @filter.on_llm_request(priority=4960)
    async def guard_before_llm_request(self, event: AstrMessageEvent, req):
        """上下文用量检查 + 压缩记忆注入 + 人格资料复原注入。"""
        if self._guard is None:
            return
        await self._guard.before_llm_request(event, req)

    @filter.command("压缩")
    async def cmd_compress(self, event: AstrMessageEvent):
        """主人确认后，把前面聊的压缩成一段记忆。"""
        try:
            if not self._is_master_event(event):
                yield event.plain_result("压缩记忆这种事，只给主人做。")
                return
            if self._guard is None:
                yield event.plain_result("记忆管理还没准备好。")
                return
            reply = await self._guard.compress_conversation(event)
            for item in await self.answer_private(event, reply, hint="压缩好了，详情私聊发你了。"):
                yield item
        except Exception as exc:
            logger.warning("[dafeiyu_core] /压缩 出错: %s", exc)
            yield event.plain_result("压缩的时候出错了，等会儿再试试。")

    @filter.command("上下文")
    async def cmd_context(self, event: AstrMessageEvent):
        """看看当前会话的上下文用了多少；加「全部」看每个对话的。"""
        try:
            if self._guard is None:
                yield event.plain_result("上下文管理还没准备好。")
                return
            raw = (event.message_str or "").strip()
            if any(word in raw for word in ("全部", "所有", "每个", "all")):
                data = await self._guard.conversation_rows(40, own_only=False)
                lines = self._guard._report_lines(data)
                rows = data.get("rows") or []
                md = report_feature.context_markdown(rows, title="全部档案的每个对话上下文")
                sent = await self.send_report("\n".join(lines), event, markdown=md)
                hint = "全部档案的每个对话上下文发到官方机器人那边了。" if sent else "没发出去，看看日志。"
                for item in self._report_hint(event, hint):
                    yield item
                return
            status = await self._guard.status(getattr(event, "unified_msg_origin", "") or "")
            ratio = float(status.get("ratio") or 0.0)
            lines = [
                f"上下文大概用了 {ratio * 100:.0f}%（约 {status.get('estimated_tokens', 0)} / {status.get('max_tokens', 0)} token，{status.get('messages', 0)} 条消息）",
            ]
            if status.get("has_summary"):
                lines.append("这段会话已经压缩过一次，之前的记忆还在。")
            if ratio >= float(status.get("warn_ratio") or 0.75):
                lines.append("有点满了，要压就说 /压缩。")
            for item in await self.answer_private(event, "\n".join(lines), hint="上下文用量私聊发你了。"):
                yield item
        except Exception as exc:
            logger.warning("[dafeiyu_core] /上下文 出错: %s", exc)
            yield event.plain_result("查上下文用量的时候出错了。")

    @filter.command("报告")
    async def cmd_report(self, event: AstrMessageEvent):
        """手动发一份整理报告（和每晚 3 点那份一样，走官方机器人 + markdown 表格）。"""
        try:
            if not self._is_master_event(event):
                yield event.plain_result("报告只给主人看。")
                return
            model = await report_feature.build_model(self, result=None, top_context=40, top_cost=5)
            model["title"] = "🐋 鲸鲸的整理报告"
            plain = report_feature.render_plain(model)
            md = report_feature.render_markdown(model)
            sent = await self.send_report(plain, event, markdown=md)
            hint = "整理报告发到官方机器人那边了。" if sent else "没发出去，看看日志。"
            for item in self._report_hint(event, hint):
                yield item
        except Exception as exc:
            logger.warning("[dafeiyu_core] /报告 出错: %s", exc)
            yield event.plain_result("整理报告的时候出错了。")
    @filter.command("费用")
    async def cmd_cost(self, event: AstrMessageEvent):
        """按会话看花了多少钱（私聊优先单独列出）。"""
        try:
            if not self._is_master_event(event):
                yield event.plain_result("费用账单只给主人看。")
                return
            top = None
            for token in (event.message_str or "").replace("／", "/").split():
                if token.isdigit():
                    top = int(token)
            data = await cost_feature.overview(self, own_only=True)
            plain = "\n".join(["🐋 鲸鲸的费用账单", ""] + cost_feature.render_lines(data, top=top))
            md = "## 🐋 鲸鲸的费用账单\n\n" + report_feature.cost_markdown(
                data, int(top or 6)
            ).replace("### 💰 费用\n", "")
            sent = await self.send_report(plain, event, markdown=md)
            hint = "费用账单发到官方机器人那边了。" if sent else "没发出去，看看日志。"
            for item in self._report_hint(event, hint):
                yield item
        except Exception as exc:
            logger.warning("[dafeiyu_core] /费用 出错: %s", exc)
            yield event.plain_result("算费用的时候出错了。")

    @filter.command("备份")
    async def cmd_backup(self, event: AstrMessageEvent):
        """人格资料备份：现在 / 列表 / 恢复 / 删除。"""
        try:
            if not self._is_master_event(event):
                yield event.plain_result("人格资料只给主人备份和恢复。")
                return
            if self._backup is None:
                yield event.plain_result("备份模块还没准备好。")
                return
            raw = (event.message_str or "").strip()
            body = raw
            for prefix_text in ("/备份", "备份"):
                if body.startswith(prefix_text):
                    body = body[len(prefix_text):].strip()
                    break
            if not body or body in ("帮助", "help"):
                yield event.plain_result(BACKUP_HELP)
                return
            parts = body.split(maxsplit=1)
            action = parts[0]
            rest = parts[1].strip() if len(parts) > 1 else ""
            if action in ("现在", "立即", "新建"):
                result = await self._backup.create(reason="command")
                counts = result.get("counts") or {}
                text = (
                    f"备份好了：{result['filename']}（人格 {counts.get('personas', 0)}、"
                    f"配置档案 {counts.get('config_profiles', 0)}、兴趣 {counts.get('interests', 0)}）"
                )
                sent = await self.send_report(text, event)
                for item in self._report_hint(event, "备份好了，结果发到官方机器人那边了。" if sent else "没发出去，看看日志。"):
                    yield item
                return
            if action in ("列表", "list"):
                rows = self._backup.list_backups()
                if not rows:
                    sent = await self.send_report("还没有备份。", event)
                    for item in self._report_hint(event, "备份列表发到官方机器人那边了。" if sent else "没发出去，看看日志。"):
                        yield item
                    return
                lines = [f"  · {row['filename']}（{row['created_text']} · {core_backup._fmt_size(row['size'])}）" for row in rows]
                text = "【备份列表】每晚 3 点自动备份，只留最新 " + str(self._backup._keep()) + " 份\n" + "\n".join(lines)
                sent = await self.send_report(text, event)
                for item in self._report_hint(event, "备份列表发到官方机器人那边了。" if sent else "没发出去，看看日志。"):
                    yield item
                return
            if action in ("恢复", "restore"):
                if not rest:
                    yield event.plain_result("用法：/备份 恢复 文件名")
                    return
                result = await self._backup.restore_file(rest)
                text = (
                    f"恢复完成：人格 {result.get('personas', 0)}、文件夹 {result.get('folders', 0)}、"
                    f"配置档案 {result.get('profiles', 0)}"
                )
                sent = await self.send_report(text, event)
                for item in self._report_hint(event, "恢复完成，结果发到官方机器人那边了。" if sent else "没发出去，看看日志。"):
                    yield item
                return
            if action in ("删除", "delete", "删"):
                ok = self._backup.delete(rest)
                text = "删掉了。" if ok else "没找到这个备份文件。"
                sent = await self.send_report(text, event)
                for item in self._report_hint(event, "结果发到官方机器人那边了。" if sent else "没发出去，看看日志。"):
                    yield item
                return
            yield event.plain_result(BACKUP_HELP)
        except Exception as exc:
            logger.warning("[dafeiyu_core] /备份 出错: %s", exc)
            yield event.plain_result("备份操作出错了，看看日志。")

    def _markdown_ok(self, event) -> bool:
        """这个机器人支持 markdown 吗（QQ 官方机器人支持，个人号 aiocqhttp 不支持）。"""
        try:
            platform = str(getattr(event, "unified_msg_origin", "") or "").split(":", 1)[0]
            for inst in self.context.platform_manager.platform_insts:
                meta = inst.meta()
                if str(getattr(meta, "id", "")) == platform:
                    name = str(getattr(meta, "name", "") or "")
                    return name in ("qq_official", "qqofficial_webhook")
        except Exception:
            pass
        return False

    async def _deliver_help(self, event):
        """命令说明：能 markdown 就发表格版，不能就发纯文本版（群里仍只私聊回）。"""
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            is_private = False
        if self._markdown_ok(event):
            result = event.plain_result(help_markdown())
            try:
                result.use_markdown(True)
            except Exception:
                pass
            return [result]
        if is_private:
            return [event.plain_result(help_plain())]
        return await self.answer_private(event, help_plain(), hint="命令清单私聊发你了。")

    @filter.custom_filter(HelpAliasFilter, False, priority=1000)
    async def help_early(self, event: AstrMessageEvent):
        """抢占 /help：内置 help 是保留命令，会先回 AstrBot 自己的清单。"""
        try:
            ok, reason = self._command_gate(event)
            if not ok:
                self._deny_command_here(event, reason)
                return
            try:
                event.should_call_llm(True)
            except Exception:
                pass
            for item in await self._deliver_help(event):
                yield item
        except Exception as exc:
            logger.warning("[dafeiyu_core] /help 抢先处理出错: %s", exc)
        finally:
            try:
                event.stop_event()
            except Exception:
                pass

    # -------------------------------------------------- 命令清单 / 触发
    @filter.command("命令", alias={"帮助", "指令", "菜单"})
    async def cmd_help(self, event: AstrMessageEvent):
        """给主人看命令清单。"""
        try:
            if not self._is_master_event(event):
                yield event.plain_result("命令清单只给主人看。")
                return
            for item in await self._deliver_help(event):
                yield item
        except Exception as exc:
            logger.warning("[dafeiyu_core] /help 出错: %s", exc)
            yield event.plain_result("取命令清单的时候出错了。")

    async def _dispatch_command(self, event: AstrMessageEvent, command_text: str) -> list:
        """把 /触发 的动作转成对应命令：临时改写消息文本再交给原 handler，返回它产出的回复。"""
        original = getattr(event, "message_str", "")
        outputs: list = []
        try:
            try:
                event.message_str = command_text
            except Exception:
                pass
            if command_text.startswith("/报告"):
                gen = self.cmd_report(event)
            elif command_text.startswith("/备份"):
                gen = self.cmd_backup(event)
            elif command_text.startswith("/压缩"):
                gen = self.cmd_compress(event)
            elif command_text.startswith("/上下文"):
                gen = self.cmd_context(event)
            elif command_text.startswith("/费用"):
                gen = self.cmd_cost(event)
            elif command_text.startswith("/兴趣"):
                gen = self.cmd_interest(event)
            else:
                return outputs
            async for item in gen:
                outputs.append(item)
        except Exception as exc:
            logger.warning("[dafeiyu_core] 触发 %s 失败: %s", command_text, exc)
        finally:
            try:
                event.message_str = original
            except Exception:
                pass
        return outputs

    @filter.command("触发", alias={"trig"})
    async def cmd_trigger(self, event: AstrMessageEvent):
        """主人在私聊里一键让她干活：/触发 报告 / 搜索 xxx / 发图 无语 ……"""
        try:
            if not self._is_master_event(event):
                yield event.plain_result("触发只给主人用。")
                return
            raw = (event.message_str or "").strip().replace("／", "/")
            body = raw
            for prefix_text in ("/触发", "触发", "/trig"):
                if body.startswith(prefix_text):
                    body = body[len(prefix_text):].strip()
                    break
            if not body:
                for item in await self.answer_private(event, TRIGGER_TEXT, hint="触发用法私聊发你了。"):
                    yield item
                return
            parts = body.split(maxsplit=1)
            action = parts[0].strip()
            rest = parts[1].strip() if len(parts) > 1 else ""
            if action in ("帮助", "help", "?", "？", "用法"):
                for item in await self.answer_private(event, TRIGGER_TEXT, hint="触发用法私聊发你了。"):
                    yield item
                return
            if action in ("报告", "report", "整理"):
                for item in await self._dispatch_command(event, "/报告"):
                    yield item
                return
            if action in ("压缩", "compress", "压"):
                for item in await self._dispatch_command(event, "/压缩"):
                    yield item
                return
            if action in ("上下文", "context"):
                for item in await self._dispatch_command(event, "/上下文" + ((" " + rest) if rest else "")):
                    yield item
                return
            if action in ("费用", "cost", "账单"):
                for item in await self._dispatch_command(event, "/费用" + ((" " + rest) if rest else "")):
                    yield item
                return
            if action in ("兴趣", "interest"):
                for item in await self._dispatch_command(event, "/兴趣" + ((" " + rest) if rest else "")):
                    yield item
                return
            if action in ("备份", "backup", "列表", "list", "恢复", "restore", "删除", "delete"):
                if action in ("备份", "backup"):
                    command_text = "/备份" + ((" " + rest) if rest else "")
                elif action in ("列表", "list"):
                    command_text = "/备份 列表"
                elif action in ("恢复", "restore"):
                    command_text = "/备份 恢复 " + rest
                else:
                    command_text = "/备份 删除 " + rest
                for item in await self._dispatch_command(event, command_text):
                    yield item
                return
            if action in ("搜索", "搜", "search"):
                if not rest:
                    yield event.plain_result("用法：/触发 搜索 要搜的东西")
                    return
                async for item in self._trigger_search(event, rest):
                    yield item
                return
            if action in ("发图", "表情", "表情包", "meme"):
                async for item in self._trigger_meme(event, rest):
                    yield item
                return
            if action in ("群压缩", "巡检", "扫群", "group"):
                if self._guard is None:
                    yield event.plain_result("上下文模块还没准备好。")
                    return
                rows = await self._guard.scan_groups_once()
                if not rows:
                    yield event.plain_result("这轮巡检没有需要压缩的群聊（消息不够多，或者刚聊过还在等）。")
                    return
                lines = [f"这轮压缩了 {len(rows)} 个群："]
                for row in rows:
                    lines.append(
                        "- " + str(row.get("umo") or row.get("cid") or "?")
                        + f"（{row.get('reason') or 'auto'}）：压了 {row.get('old', 0)} 条，"
                        + f"留 {row.get('recent', 0)} 条，摘要 {row.get('summary_len', 0)} 字"
                    )
                for item in await self.answer_private(event, "\n".join(lines), hint="群压缩结果私聊发你了。"):
                    yield item
                return
            if action in ("续聊", "engaged"):
                data = self.engaged_status()
                head = (
                    "续聊：" + ("开着" if data.get("enabled") else "关着")
                    + f"（最多连 {data.get('max_turns', 0)} 句，盯 {data.get('window_seconds', 0)} 秒，"
                    + f"概率 {data.get('probability', 0)}%）"
                )
                groups = data.get("groups") or {}
                lines = [head]
                if not groups:
                    lines.append("现在没有正在续聊的群。")
                else:
                    for key, value in groups.items():
                        lines.append(
                            f"- {key}：第 {value.get('turns', 0)} 句，还剩约 {value.get('left_seconds', 0)} 秒"
                        )
                for item in await self.answer_private(event, "\n".join(lines), hint="续聊状态私聊发你了。"):
                    yield item
                return
            yield event.plain_result("不认识这个动作。发「/触发 帮助」看用法。")
        except Exception as exc:
            logger.warning("[dafeiyu_core] /触发 出错: %s", exc)
            yield event.plain_result("触发的时候出错了，看看日志。")

    async def _trigger_search(self, event: AstrMessageEvent, query: str):
        """主人手动触发一次联网搜索（不受兴趣/主人权限限制，仍遵守每日上限）。"""
        if not bool(search_feature._RUNTIME.get("enabled", True)):
            for item in await self.answer_private(event, "联网搜索关着，去插件设置里把 search 段打开。", hint="私聊发你了。"):
                yield item
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        ok, reason = search_feature._rate_ok(umo)
        if not ok:
            for item in await self.answer_private(event, f"{reason}，等会儿再搜。", hint="私聊发你了。"):
                yield item
            return
        want = max(1, min(10, search_feature._as_int(search_feature._RUNTIME.get("count"), 5)))
        results: list[dict] = []
        if str(search_feature._RUNTIME.get("provider") or "deepseek").lower() == "deepseek":
            results = await search_feature._deepseek_search(
                self, query, want, bool(search_feature._RUNTIME.get("need_content", False))
            )
        if not results and bool(search_feature._RUNTIME.get("fallback_baidu", True)):
            results = await search_feature._baidu_search(self, query, want)
        if not results:
            for item in await self.answer_private(event, f"「{query}」没搜到结果，可能是网络或者搜索服务的问题。", hint="私聊发你了。"):
                yield item
            return
        search_feature._remember_topic(query)
        text = f"🔎 「{query}」搜索结果（{len(results)} 条）：\n" + search_feature._format_results(results, want)
        for item in await self.answer_private(event, text, hint="搜索结果私聊发你了。"):
            yield item

    async def _trigger_meme(self, event: AstrMessageEvent, mood: str):
        """主人手动触发发一张表情包（发给当前会话，通常就是私聊）。"""
        if not bool(self._meme_cfg("enabled", True)):
            for item in await self.answer_private(event, "表情包功能关着，去插件设置里把 meme 段打开。", hint="私聊发你了。"):
                yield item
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not meme_feature._allow(umo):
            for item in await self.answer_private(event, "一小时里发太多图了，先聊聊别的。", hint="私聊发你了。"):
                yield item
            return
        pack_id, pack_dir = meme_feature._resolve_pack()
        if pack_dir is None:
            for item in await self.answer_private(event, "表情包库没找到。", hint="私聊发你了。"):
                yield item
            return
        categories = meme_feature._load_categories(pack_dir)
        category = meme_feature._mood_category(mood, categories)
        path = meme_feature._pick_image(pack_dir, category, umo)
        if path is None:
            for item in await self.answer_private(event, "这会儿没找到合适的图。", hint="私聊发你了。"):
                yield item
            return
        from astrbot.api.message_components import Image as _Image

        await event.send(MessageChain([_Image.fromFileSystem(str(path))]))
        meme_feature._record(umo, path)
        logger.info("[dafeiyu_core] /触发 发表情包 %s (%s/%s)", path.name, pack_id, category)
        yield event.plain_result(f"发了一张「{category}」的（{pack_id}）。")
