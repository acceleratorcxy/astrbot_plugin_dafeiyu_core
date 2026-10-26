# 大肥鱼人格核心（AstrBot 插件）

![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.16%2C%20%3C5-4B8BBE)
![version](https://img.shields.io/badge/version-2.3.0-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![python](https://img.shields.io/badge/python-3.10%2B-yellow)

把「人格机器人」做完整的一套 AstrBot 插件：**主人识别、兴趣机制、群聊唤醒与续聊、人味化输出、联网搜索、睡眠模式、上下文压缩、全量资料备份、费用统计、群档案知识库**——全部收在一个插件、一套配置、一个页面里。

原本是四个独立插件（owner / group_wake / humanizer / interest）拼起来的，后来合并成一个包，配置和查看统一到插件详情页的「View」标签。

## 目录结构

```
.
├── astrbot_plugin_dafeiyu_core/     # 插件本体（安装时复制这个目录）
│   ├── main.py                      # 入口：命令、权限门、汇报、子模块装配
│   ├── _conf_schema.json            # 13 段配置定义（页面「配置」tab 由它生成）
│   ├── metadata.yaml                # 插件元信息（AstrBot 识别用）
│   ├── owner_feature.py             # 主人身份与听令规则
│   ├── group_wake_feature.py        # 群聊唤醒 / 随机搭话 / 续聊 / 热闹加成
│   ├── humanizer_feature.py         # 人味化输出、配图频率
│   ├── interest_feature.py          # 兴趣机制（观察中 → 兴趣 → 自动清理）
│   ├── meme_feature.py              # 主动发表情包（接 meme_manager 图包）
│   ├── search_feature.py            # 联网搜索（DeepSeek Search/Fetch + 百度兜底）
│   ├── speaker_feature.py           # 群聊发言记录、看人看全（认名字/转述图片）、按需补上下文
│   ├── knowledge_feature.py         # 群档案知识库（每晚总结群黑话/昵称/话题，聊天时注入）
│   ├── cost_feature.py              # 按会话算钱（支持峰谷价）
│   ├── sleep_feature.py             # 睡眠模式（安静期 / 睡眠期 / 早上补回）
│   ├── core_context.py              # 上下文守护：提醒、压缩、群聊自动压缩、每会话报告
│   ├── core_backup.py               # 全量资料备份 / 恢复 + 每晚整理报告
│   ├── core_page.py                 # 插件页面后端接口
│   ├── report_feature.py            # 汇报投递（官方机器人 + markdown 表格）
│   ├── README.md                    # 插件说明（开发者向：模块、配置、坑）
│   └── views/panel/index.html       # 插件页面（总览/命令/兴趣/备份/配置/上下文）
├── docs/
│   └── 使用手册.md                   # 给使用者的手册：命令、页面、13 段配置全表、FAQ、回滚
├── CHANGELOG.md
├── LICENSE
└── README.md                        # 本文件
```

> 插件放在子目录里，方便和文档、开发资料一起管理。**安装时复制的是 `astrbot_plugin_dafeiyu_core/` 目录**，不是仓库根目录。

## 功能

| 模块 | 做什么 |
|---|---|
| 主人识别 | 认主人、认听令范围，别人下的指令不乱听；每轮标注说话人身份 |
| 群聊唤醒 | 关键词唤醒、随机搭话、被叫到才回、续聊、群里热闹时提高主动概率 |
| 人味化 | 去 AI 口吻、拆句、控制配图频率，说话更像真人 |
| 兴趣机制 | 兴趣库 + 临时话题观察：聊得多才升级成兴趣，久不聊自动清理；标记为固定、和主人相关的兴趣不自动清；每晚（或手动）先让模型挑出重复的兴趣/临时话题再合并，模型不可用时退回规则合并；人设固定的兴趣默认不自动搜、也不留搜索记录 |
| 联网搜索 | 不知道的实时信息自己查（DeepSeek 官方 Search/Fetch，百度兜底） |
| 表情包 | 接 meme_manager 图包，按语境主动发表情包 |
| 发言记录 | 记住群里最近谁说了什么，答复时能分清人和上下文；别人的图片/表情用视觉模型转述后再答 |
| 群档案 | 每晚 3:40 把每个群的群友昵称外号、黑话梗、近期话题总结成知识库，聊天时自动调用；/群档案 查看或即时更新 |
| 费用 | 按会话统计 token 与费用，支持峰谷计价 |
| 睡眠模式 | 1 点后只记不回、2 点后连主人消息也排队、早上 6 点后随机醒来补回 |
| 上下文 | 私聊快满提醒 → 确认后压缩；群聊按余量/静默/每日自动压缩；压缩后可复原人格资料 |
| 备份 | 每晚睡眠时间全量备份（人格/兴趣/会话/发言记录/压缩状态/表情包清单），只留最新 3 份 |
| 汇报 | 每晚 + `/报告` 生成 markdown 表格汇报，走指定机器人发主人私聊 |
| 页面 | 6 个标签页：总览、命令、兴趣、备份、配置、上下文 |

## 安装

1. 把本仓库的 `astrbot_plugin_dafeiyu_core/` 整个目录复制到 AstrBot 的插件目录：

```
   data/plugins/astrbot_plugin_dafeiyu_core/
```

2. 修权限（AstrBot 在容器里跑时常见）：

```bash
   chmod -R a+rX data/plugins/astrbot_plugin_dafeiyu_core
   rm -rf data/plugins/astrbot_plugin_dafeiyu_core/__pycache__
```

3. 在 AstrBot 面板重载插件（或重启 AstrBot）。

4. 打开插件详情页的 **View**，按提示填：
   - 「主人身份」：`master_ids`（你的 QQ 号 / openid / 用户名）、`master_name`
   - 各功能段的 `bot_filter`：哪些机器人（umo 前缀）生效
   - 其余按需开关，保存后立即生效。

## 常用命令

| 命令 | 说明 |
|---|---|
| `/help` `/命令` `/帮助` | 命令清单（markdown 表格版 / 纯文本版自动切换） |
| `/触发 <动作>` | 让机器人执行动作：报告 / 压缩 / 上下文 / 费用 / 备份 / 兴趣 / 搜索 / 发图 / 群压缩 / 续聊 |
| `/主人` | 看当前会话的说话人身份 |
| `/兴趣 看|加|删|搜|记|整理` | 兴趣库管理（整理 = 立刻跑一次模型整理合并） |
| `/上下文` `/上下文 全部` | 当前会话 / 每个会话的上下文用量 |
| `/压缩` | 压缩当前会话上下文 |
| `/报告` `/费用 [N]` | 整理报告 / 费用账单 |
| `/备份 现在|列表|恢复|删除` | 全量资料备份管理 |
| `/睡眠` `/睡眠 睡|醒` | 睡眠状态 / 手动睡下 / 叫醒 |

> 指令默认只认主人，并且默认只在配置里指定的机器人上生效（私聊/群聊可分别开关）。

## 文档

- 使用手册：[docs/使用手册.md](docs/使用手册.md)（命令、页面、13 段配置全表、FAQ、一键回滚）
- 插件说明：[astrbot_plugin_dafeiyu_core/README.md](astrbot_plugin_dafeiyu_core/README.md)（模块结构、LLM 工具、配置项、开发坑）
- 更新记录：[CHANGELOG.md](CHANGELOG.md)

## 环境要求

- AstrBot `>= 4.16, < 5`（开发验证于 4.29.0）
- Python 3.10+
- 可选依赖：
  - **meme_manager** 插件（AstrBot 插件市场可搜）：想要主动发表情包/配图时安装，并准备图包
  - DeepSeek API Key 或百度 AI 搜索 Key：想要联网搜索时配置（也可用 AstrBot 内置搜索）
  - QQ 官方机器人（支持 markdown 汇报）或 OneBot / aiocqhttp（个人号）

## 数据与隐私

插件的数据都放在 AstrBot 的 `data/plugin_data/astrbot_plugin_dafeiyu_core/` 下（兴趣库、上下文压缩状态、发言记录、群档案、睡眠状态、备份），不修改 AstrBot 源码。
备份文件包含人格、兴趣、会话历史等资料，**注意不要提交或外传**（`.gitignore` 已排除 `data/`、`plugin_data/`、`backups/`）。

## 许可

MIT，见 [LICENSE](LICENSE)。
