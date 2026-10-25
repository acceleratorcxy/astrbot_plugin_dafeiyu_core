# 大肥鱼人格核心（astrbot_plugin_dafeiyu_core）

把原来四个独立插件合并成一个，并补上人格资料备份与上下文压缩。

> 命令与使用手册（命令速查、页面使用、配置项全表、常见问题、一键回滚）见 `docs/使用手册.md`。

| 模块 | 内容 |
| --- | --- |
| owner | 主人身份识别、听令规则、`/主人` 命令 |
| group_wake | 群聊关键词唤醒 + 随机搭话 + 回完之后的续聊 + 群里热闹时加成 |
| humanizer | 人味化输出（拆段、去 Markdown/旁白/AI 腔、限图、概率配图） |
| interest | 兴趣库（短期观察 → 升级 → 自动清理）、倾向性接话、`/兴趣` 命令与 6 个 LLM 工具 |
| search | 联网搜索：DeepSeek 官方 Search/Fetch（百度 AI 搜索兜底）、`web_search` / `web_fetch` 工具、频率限制 |
| meme | 主动发表情包工具 `send_meme`（只读 meme_manager 图包） |
| speaker | 群聊发言记录（带名字、多模态主模型直接看图）+ 按需补上下文 + 主人没头没尾时补别人在聊什么 |
| cost | 费用统计：按会话算 token 花费（可选峰谷计价），页面/报告/命令三处展示 |
| sleep | 睡眠模式：1 点起只记不回、2 点后连主人消息也排队、早上 6 点后随机醒来补回；主人说晚安只回「安」，主人也回「安」才真睡；主人可强制叫醒 |
| knowledge | 群档案知识库：每晚总结每个群的昵称/外号/黑话/近期话题，落 knowledge.json，聊天时自动注入（`/群档案` 命令） |
| backup | 全量资料备份（人格/兴趣/会话/发言记录/表情包清单…）统一导出导入、睡眠时间自动备份、只留最新 N 份 |
| context_guard | 私聊快满提醒 + `/压缩`、群聊自动压缩、每会话上下文报告、压缩后从最新备份恢复人格资料 |
| page | 统一配置与查看页面（插件详情页的「View」标签，6 个 tab：总览 / 命令 / 兴趣 / 备份 / 配置 / 上下文） |

## 安装/升级

1. 目录放在 `data/plugins/astrbot_plugin_dafeiyu_core/`。
2. 权限：文件 644、目录 a+rX，删掉 `__pycache__`。
3. 在 AstrBot WebUI 插件页重载本插件。
4. 首次加载会自动迁移旧插件配置（`astrbot_plugin_dafeiyu_*` 的 `*_config.json`）与旧兴趣库；确认无问题后再停用旧的四个插件。

## 命令

> 指令只在**官方机器人**（QQ 官方机器人 `qq_official_bot`）那边生效；aiocqhttp 个人号的群聊/私聊里发指令一律不执行（私聊回一句「指令得在官方机器人那边发」，群里不吭声）。
> 入口和开关都在插件页面「配置」→「主人私聊通知」：`command_platforms`（哪些机器人能用，默认只有官方机器人，`*` 不限制）、`command_private_enabled`（私聊，默认开）、`command_group_enabled`（群聊，默认关）、`command_master_only`（只允许主人，默认开）、`command_group_master_ids`（群聊里额外认作主人的成员 ID，群里发 /sid 查）。
> 页面也有「命令」tab：三张表说明怎么设置、怎么调用、`/触发` 有哪些动作。

- `/help`（或 `/命令` `/帮助` `/指令` `/菜单`）：主人看命令表（`/help` 已专门抢在 AstrBot 内置 help 前面处理）。**双版**：在官方机器人发是 markdown 表格版（常用指令 / `/触发` 动作 / 怎么设置三张表 + 例子），在个人 QQ 等不支持 markdown 的聊天里发结构化纯文本版，群里则私聊发给主人；`/触发 <动作>`（或 `/trig`）在私聊里一键让她干活（报告/备份/压缩/上下文/费用/兴趣/搜索/发图/群压缩/续聊）
- `/主人`：确认身份（主人 / 非主人各一句）
- `/兴趣`：兴趣列表；`/兴趣 加 名字 别名`、`/兴趣 删 名字`、`/兴趣 搜 话题`、`/兴趣 记 名字 内容`、`/兴趣 看 名字`（写操作仅主人）
- `/上下文`：当前会话上下文用量；`/上下文 全部`：每个对话的用量一遍列出来
- `/压缩`：主人确认后压缩当前会话历史，并从最新备份恢复人格资料
- `/费用 [N]`：费用账单（累计/今日/本月 + 每个会话花多少），N 控制每个会话明细条数
- `/报告`：整理报告（备份 + 资料 + 夜里 + 每个对话的上下文 + 费用），**以 markdown 表格发到官方机器人那边**（QQ 官方机器人会渲染表格），个人 QQ 只回一句提示；每晚 3 点也会自动发一份；**只算鲸鲸自己配置档案的会话**（别的档案里的助手2号/伊蕾娜/webchat 不列出来），会话名会显示群名/私聊昵称
- `/备份`：`现在` / `列表` / `恢复 <文件名>` / `删除 <文件名>`
- `/睡眠`：看睡眠状态；`/睡眠 睡` 手动入睡（回「安」）；`/睡眠 醒` 强制叫醒
- `/群档案`（或 `/群知识`）：看当前群的档案（群友称呼对照、黑话、最近话题）；`/群档案 更新` 立刻重新总结本群；`/群档案 全部` 列出所有群

## LLM 工具（写操作仅主人可用；搜索/发图不受限）

`interest_list`、`interest_add`、`interest_remove`、`interest_search`、`interest_remember`、`interest_recall`、`send_meme`、`web_search`、`web_fetch`

需要在人格的「工具」里勾选这些工具才会生效（人格 tools 为空列表时一个插件工具都用不了）。

## 备份

- 位置：`data/plugin_data/astrbot_plugin_dafeiyu_core/backups/dafeiyu-backup-YYYYmmdd-HHMMSS.json`
- 默认每晚睡眠时间 3:00 自动备份（趁睡着做，不打扰）。自动备份只保留最新 `keep`（3）份，手动/页面备份另留最新 `manual_keep`（20）份；页面里改 hour/minute/keep 会立刻重启定时循环生效（不用重载插件）
- 备份完会把整理结果（文件名、人格/档案/兴趣数、保留份数、已压缩会话数、备份列表）**私聊发给主人**
- 权限：备份目录 0700、备份文件 0600（里面有人格 prompt、会话历史、主人 ID、搜索 key，同机其他账号读不到）
- 内容（全量，不只人格描述）：人格与人格夹、配置档案（含路由）、本插件 13 段配置、兴趣库（含观察中候选）、群聊发言记录、群档案知识库（knowledge.json）、上下文压缩状态、搜索状态、睡眠状态、**所有会话历史**、meme_manager 图包注册与文件清单（不嵌图片二进制）
- 恢复范围：人格与人格夹、配置档案与路由、兴趣库、群聊发言记录 / 群档案知识库 / 压缩状态 / 搜索状态 / 睡眠状态、13 段配置；**会话历史与表情包清单只存档不写回**（避免覆盖线上聊天 / 别人的图包，要找回直接看备份 JSON）。
- 恢复是事务化的：中途出错会回滚并报 `ok=false` + 原因，不会留下「一半新一半旧」；恢复完自动热重载，失败会给出手动重载提示。
- 时间配置写错（比如 hour=3:00、minute=abc）不会让定时循环崩掉：自动回落 3:00 并打一条 warning。
- 页面里可「立即备份 / 下载 / 导入 / 恢复 / 删除」；`/备份 恢复 <文件>` 也行

## 上下文压缩

1. 估算用量超过 `warn_ratio`（默认 0.75）时，插件会在聊天里提醒主人；
2. 主人回 `/压缩` 后：把旧历史用当前 provider 总结成一段「【压缩记忆】」，只保留最近一小段原文，写回会话；
3. 若 `auto_reload_persona` 开启，自动读取**最新备份**里的人格 prompt、主人 ID、兴趣摘要，拼成「【人格资料复原】」临时注入后几轮对话（不写进历史）。

## 群聊自动压缩 / 上下文报告（context_guard 段）

- 私聊：用量到 `warn_ratio`（默认 0.75）**私聊提醒主人**，回 `/压缩` 才压；压完从最新备份恢复人格资料。
- 群聊：不提醒主人，自己按三个条件压（满足任一即可，且消息数 ≥ `group_min_messages`=20、安静 ≥ `group_pause_seconds`=45 秒，正在聊的天不动）：
  1. 余量 `used/limit >= group_auto_ratio`（默认 0.7）；
  2. 安静超过 `group_idle_minutes`（默认 30 分钟）；
  3. 每天到 `group_daily_hour`（默认 4 点）做一次日常压缩（填 -1 关闭）。
- 巡检间隔 `group_check_interval_seconds`（默认 120 秒），每次最多看 `group_scan_limit`（200）个会话。
- 压下来的记忆写成【压缩记忆】注入后续对话，最近的消息原样保留（`compress_keep_recent_ratio`，默认 0.15）。
- 每晚 3 点（跟自动备份同一时刻，睡眠中）私聊给主人一份「每个对话的上下文」；随时可以发 `/报告` 或 `/上下文 全部`。
- 页面「上下文」标签：统计磁贴（对话总数/已压缩/接近阈值/消息总数）+ 群聊自动压缩状态 + 每个对话的余量进度条 + 已压缩记忆列表。
- 想要回滚：把 `group_auto_compress` 关掉即可（群聊既不提醒也不自动压）。

## 配图自然度（humanizer 段）

- meme_manager 自己按 `generation.emotion.probability`（当前 100）给几乎每条回复配图；本插件在装饰阶段再按概率摘掉一部分，**不改 meme_manager 的全局配置**（别的 bot 不受影响）。
- `humanizer.image_reply_probability`（默认 45）：这条回复保留表情包的概率（%）。0 = 从不带图，100 = 有图必带。
- `humanizer.image_min_gap_seconds`（默认 120）：同一个对话两张图的最短间隔，间隔内即使掷中也不带图。
- 纯图片回复（没有正文）不会因为摘图变成空消息；摘完没正文就保持原样。
- 表情包库当前默认用 `qq-emoji-collection`（主人整理的 103 张 QQ 收藏表情，15 个分类）；`deepseek-fanart-meme-pack` 仍装着，表情包页面「选择规则」里可切回。

## 兴趣记录的编辑与删除

- 页面「兴趣」标签里每条记录右侧有「改」「删」：可改标题/正文/链接，也能直接删掉。
- 兴趣卡片上的「编辑」可改兴趣度、别名、追加一条记录；「删除」删整个兴趣。
- 页面接口：`page/interest/note/update`、`page/interest/note/delete`（body 用 interest_id/id/name + note_id）。
- 页面用的是自绘弹窗：插件页 iframe 带 sandbox，原生 `prompt`/`confirm` 会被浏览器忽略（报 'allow-modals' not set）。

## 联网搜索（search 段）

- 她不知道、或时效性强的东西（比赛/新闻/天气/价格/版本/人物近况）会自己上网查：`web_search` 走 DeepSeek 官方 `POST https://api.deepseek.com/v1/tools/search`，失败兜底百度 AI 搜索；`web_fetch` 走 DeepSeek `POST https://api.deepseek.com/v1/tools/fetch` 抓正文（截前 4000 字）。
- API key 优先取 `search.api_key`，为空时从 `cmd_config.json` 的 `provider_sources` 里找 deepseek 的 key。
- 限额：`daily_limit`（默认 80 次/天）+ `cooldown_seconds`（默认 6 秒，同会话冷却），状态在 `plugin_data/astrbot_plugin_dafeiyu_core/search_state.json`。
- 普通群友问公开信息也允许她自己搜（owner 段只拦跑命令/改配置/翻隐私/要提示词）；搜不到要直说没找到，不许编、不许让对方贴资料。
- 搜索到的一次性话题不会直接进兴趣库，只进「还在观察」候选（见下节）。

## 兴趣短期化（观察中 → 兴趣 → 自动清理）

- 临时聊到/搜到的话题先记进 `candidates`（观察区）：同名/别名每命中一次 mentions+1，达到 `promote_mentions`（默认 3）自动升级为兴趣（kind=auto，affinity 0.55）。
- 自动清理每小时跑一次：候选超过 `candidate_ttl_days`（默认 3 天）删除；未固定兴趣超过 `interest_ttl_days`（默认 7 天）删除；闲置超过 1 天的未固定兴趣按 `affinity_decay_per_day`（默认 0.02）衰减，最低 0.3。
- 永不清理：`kind=persona`（人设自带的鲸鱼/白饭/摸鱼/表情包）与 `kind=master`（主人用 `/兴趣 加` / `interest_remember` / **主人聊到的**）都 `pinned=True`，`prune()` 跳过。
- 页面「兴趣」标签新增「还在观察」卡片，可手动「收进兴趣」或「忘掉」；兴趣库条目带「人设固定」/「主人相关」/「自动」标记。接口 `page/interest/candidate/promote`、`page/interest/candidate/forget`。
- 主人加成：群里主人说的话更容易触发兴趣接话——`master_wake_threshold_scale`（默认 0.7，阈值乘这个数）+ `master_wake_boost`（默认 1.6，概率乘这个数，强匹配封顶 98%、弱相关 95%）。
- 相关话题也能接话：即使没提名字/别名，只要消息内容跟她兴趣（含备忘里的关键词）相关度够高，也会按 `wake_related_probability`（默认 35%，按相关度打折）掷骰子主动接一句；门槛 `wake_related_min_score`（默认 0.35），可用 `wake_related_enabled` 关掉。弱相关不会被记进兴趣、也不会触发自动搜索。

## 兴趣整理：晚上自动合并相似的（interest 段）

- 每晚整理（默认 03:00 的自动备份循环）会**先合并很像的兴趣**，再备份、再发报告——所以备份里就是合并后的状态，报告里会写「整理时合并 N 组：A ← B」。
- 判定相似（满足任一，默认阈值 `merge_threshold` = 0.82）：
  - 名字/别名很像：去掉标点小写后完全相同 = 1.0，一个包含另一个 = 0.9，其余按 difflib 相似度；
  - 共享 ≥ 2 条来源 URL（记 0.85，防止同一个话题换两种写法堆两条）。
- 合并规则：人设固定(`persona`) > 主人相关(`master`) > 自动(`auto`)；同优先级再比 pinned、兴趣度、提到次数、创建时间，赢的那条留下；被并掉的名字进 `aliases`，来源记进 `merged_from`，笔记合并去重（上限 `merge_max_notes`），提到次数相加。
- 每晚最多合并 `merge_max_pairs` 组；`merge_enabled=false` 可整段关掉。
- 手动整理：页面「兴趣」tab 的「整理合并」按钮，或 `/兴趣 整理`（主人专属）。页面上还会显示上次合并了几组。

## 主动发表情包（meme 段）

- 内置 LLM 工具 `send_meme(mood)`：想配图、被要图时直接往当前会话发一张表情包（只读 meme_manager 的图包，不改它配置）。
- 按心情词匹配当前默认图包的分类，分类内随机取图；同一会话避免连发同一张；`meme.max_per_hour`（默认 20）限流。
- 发不发图由模型自己决定：插件只提供工具和限流，不在人格里写任何要求；想固定发图风格，就在你自己的人格设定里写。
- 兄弟模块里的 `@llm_tool` 需要 `main.py` 的 `_normalize_llm_tools()` 手动 `functools.partial` 绑定，否则会报 `missing 1 required positional argument: 'event'`；重载日志应有「已把 9 个 LLM 工具绑定到主模块」。

## 群聊续聊 / 热闹加成（group_wake 段）

- 她回完一句后进入「续聊盯梢」`engaged_window_seconds`（默认 240 秒）：这几秒里如果群里接着聊的话题跟她刚说的/兴趣相关（不必叫名字），按 `engaged_probability`（80%）× 相关度继续接，最多连 `engaged_max_turns`（6 句）；连续 2 次不相关或没人说话就退出。相关度算法：关键词 1.0、与她上一句 bigram 重合、与最近 3 条群友消息重合、兴趣相关、问句/接茬/指代加分。
- 续聊宽限 `engaged_grace_seconds`（默认 90 秒）：过了盯梢窗口但没过宽限时不清状态，只要这条消息还挺相关（相关度 ≥0.45）就再跟一句，治「刚好在窗口边缘到的消息永远接不上」（慢半拍）。
- 群里热闹加成：`lively_window_seconds`（120 秒）内消息数超过 `lively_min_messages`（6）条后，随机搭话/续聊/兴趣接话的概率乘 `1+(超出数/门槛)`，最多乘 `lively_max_boost`（3 倍）；`lively_boost_enabled` 可关。
- 坑：续聊不能用 `event.is_wake` 判断（别的插件 filter 通过也会置 True），只看 `is_at_or_wake_command` 和真唤醒 extra 标记。

## 社交分寸（治「自作多情」）

群里不是在跟她说话时别插嘴，说多了自动让一让，被群友明说「没跟你说话 / 别插嘴 / 自作多情」就安静一阵子。

| 配置（「配置 → 群聊唤醒」） | 默认 | 作用 |
| --- | --- | --- |
| random_skip_directed | true | 有 @别人 或引用回复、又没提她名字的消息，不触发随机搭话 |
| engaged_directed_min_score | 0.6 | 上面这类消息走续聊时要求的相关度 |
| self_share_enabled | true | 她在群里发言占比过高时先不开口 |
| self_share_window_seconds | 300 | 算占比的时间窗口 |
| self_share_min_messages | 10 | 窗口内消息太少就不算占比 |
| self_share_max_ratio | 0.35 | 占比超过这个值就自动让麦 |
| retort_silence_enabled | false | 被嫌弃后这个群安静一会儿（默认关：m12630 要求不管这类话） |
| retort_silence_minutes | 10 | 安静多少分钟 |

数据依据：真实群 1991 条里她占 6.6%，别人真正冲她来的只有 5.5%，但她 132 次回复里有 28.8% 是插进别人对话；另 23 个真实群 10.4 万条里 @别人/引用别人的定向发言占 7.7%。详见补充文档 20.36。

## 协议端命令静默词表（silent_guard）

群里出现协议端自带的命令（aiocqhttp 的 #sl、NapCat 的 #napcat 之类）时，一律当没看见：不唤醒、不回复，也不写进群聊发言记录（否则模型会追问「刚才那条 #sl 是什么意思」）。

- silent_prefixes：命中前缀，不区分大小写，默认 #sl / #napcat / #status / #version / #nb；
- silent_hash_message：以 # 或 ＃ 开头的短消息一律不回（默认开），长度上限 silent_hash_max_chars（默认 24 字）。

三项都在「配置 → 群聊唤醒」里。只对群聊生效；私聊里发这些仍会被正常回一句。

## 群聊发言记录（speaker 段）

- 群聊里每条消息都会记一笔：昵称、发送者 id 后 6 位、是不是主人、内容（截 40 字）、时间；同人同文 30 秒去重。记录存 `plugin_data/astrbot_plugin_dafeiyu_core/speaker_log.json`。
- 每次请求 LLM 前，把最近 `window_minutes`（默认 120 分钟）内最近 `max_lines`（默认 8）条以【群聊发言记录】临时注入（不写历史），所以她能回答「刚才谁说啥了」，也能分清现在是在跟谁说话。
- 每个群最多记 `max_log_per_umo`（40）条、最多记 `max_umos`（60）个群；`only_group` 默认只记群聊（`record_private` 可开私聊）。
- 页面「上下文」tab 顶部会显示「群聊发言记录：开着（记了 N 个群 / M 条…）」。
- **看人看全**：记录里带名字（主人显示「主人」，别人带群昵称）；别人发的图片/表情默认**把原图直接附在这次请求里**给多模态主模型看（`image_direct_enabled` 默认开、`image_direct_max` 默认 3 张/次），不额外花一次转述的钱；只有主模型看不了图（provider 的 `modalities` 里没有 `image`）时才退回「先用视觉模型转述成一句 `［图：描述］`」（`image_caption_*` 那几个配置，`image_caption_ttl_minutes` 是转述缓存）。`context_always` 默认开=每次回复都带记录；关掉则退回下面「按需」模式。
- **按需补上下文**（`context_on_demand`，默认开）：只有她真要回、且这句话「看不懂」（指代/短问句/问句结尾，见 `context_short_chars`=6、`context_question_chars`=12）时才把群聊记录注入；能看懂的完整句子不读记录（日志「这句能看懂，不读群聊上下文」）。
- **新鲜度块**（`fresh_block_enabled`，默认开）：每次群聊回复前都单独注入一小块「现在是 HH:MM + 最近 `fresh_minutes`（15）分钟内最后 `fresh_lines`（4）条（含她没回的）」，并且如果她要回的那条已经过去 ≥5 秒，会明确提示「这期间群里又说了上面这些，回话以最新的为准，别拿更早的旧话题套上」。治「话题已经换了，她还在接旧话题」的慢半拍。
- **主人没头没尾**（`master_context_enabled`，默认开）：群聊里的主人发短句（≤`master_context_short_chars`=8 且含问词代词）、带指代的短句（≤20 字）、追问词（`_FOLLOWUP`：说明白/说清楚/展开/细说/然后呢/怎么回事…）或 ≤6 字又不是纯语气词（「好的」「嗯」这类不补）时，自动补「最近 `master_context_minutes`（30 分钟）内别人在聊的最多 `master_context_lines`（12）条」，让她先猜主人指哪件事再回；猜不准要直说没看懂。

- **看得懂表情**（v2.3.4）：记录里的图片会带标签——[图片] 是照片/截图，[表情包] 或 [表情包：揪你舌头] 是收藏表情包，[表情：捂脸] 是 QQ 自带小黄脸；注入时还会告诉她「表情包别当照片解说画了什么」。具名表情包默认既不给模型看原图也不做视觉转述（sticker_direct_enabled 默认关，省 token），照片仍直接给多模态主模型。开关在「配置 → 群聊发言记录」：sticker_label_enabled / sticker_direct_enabled / face_label_enabled。

## 群档案知识库（knowledge 段）

- 每晚 `nightly_hour:nightly_minute`（默认 3:40，排在 3:00 全量备份之后）把每个群的发言记录 + 会话历史交给模型，总结成结构化档案：**群成员昵称/外号/身份**（例：「奈白的雪子 → 大家叫 tom」）、**群内黑话与梗**、**最近聊的话题**；落 `plugin_data/astrbot_plugin_dafeiyu_core/knowledge.json`。
- 聊天时（仅群聊）在请求 LLM 前注入【本群档案】临时块（`inject_enabled` 默认开，成员最多 12 条 / 黑话 10 条 / 话题 5 条），不写历史、不破坏提示词缓存；所以她听得懂外号和黑话，不会把 tom 当陌生人。
- `/群档案`：看当前群档案；`/群档案 更新` 立即总结本群；`/群档案 全部` 列所有群（仅主人）。
- 配置（`knowledge` 段 14 项）：`enabled` / `bot_filter` / `nightly_enabled` / `nightly_hour`(3) / `nightly_minute`(40) / `max_groups`(20) / `max_lines`(120，喂给模型的原文行数) / `provider_id`（留空=当前模型）/ `inject_enabled` / `inject_max_members`(12) / `inject_max_slang`(10) / `inject_max_topics`(5) / `retain_days`(90，久不活跃的群自动清) / `prompt`（自定义总结提示词）。
- 页面「上下文」tab 有一行状态「群档案知识库：开着 · N 个群 / 成员对照 M 条 / 黑话 K 条 · 每晚 3:40 整理」；每日汇报的「资料」表也多一行。全量备份包含 knowledge.json。
- 回滚：`knowledge.enabled=false`（连注入一起关）；只关夜里整理 `nightly_enabled=false`；只关注入 `inject_enabled=false`。

## 费用统计（cost 段）

- 数据来自 AstrBot 的 `provider_stats` 表（每次 agent 跑完一条，含 input_other / input_cached / output），**不是** conversations.token_usage（那只是最近一次的 total）。
- 按会话（umo）分组累计：每个私聊、每个群分开算，所以「每个私聊单独费用」天然就有。
- 牌价可在配置里改：`input_price`（默认 2 元/百万）、`cached_input_price`（0.2）、`output_price`（3）。
- **峰谷计价（可选，默认关）**：`peak_valley_enabled` 勾上后按 DeepSeek 官方 2026 峰谷价算（deepseek-flash：峰 2/0.04/8、空闲 1/0.02/4 元每百万 token，输入未命中/命中/输出）。峰时 = 北京时间工作日 `peak_ranges`（09:00-12:00,14:00-18:00），周末与 `peak_holidays`（逗号分隔日期）全天算空闲。实现上按 `provider_stats.created_at`（UTC）逐条判峰谷再算钱，所以历史账单也会按峰谷重算；关掉就回到上面的单档牌价。
- 展示：插件页面「上下文」tab 的「费用（按会话算）」卡片（累计/今日/本月磁贴 + 逐会话明细，余量列表每行也直接带 ¥；这个卡片是全量诊断视角）；每晚 3 点私聊报告带一段；命令 `/费用 [N]`。**报告和 `/费用` 默认只算自己配置档案**（`notify.report_only_own_profile`），会话名显示群名/昵称。
- 想关掉：`cost.enabled=false`（页面/命令/报告都会显示「关着」）。

## 睡眠模式（sleep 段）

- 三个时段：**醒着**（正常）、**安静期**（`quiet_hour` 默认 1 点 → `sleep_hour` 默认 2 点：别人的消息只记不回，主人照常）、**睡着了**（全体消息排队，只有主人指令/叫醒词能过）。
- 入睡方式（v2.3.7 起是「对暗号」）：① 主人私聊说晚安类词（晚安/睡了/去睡/困了/先睡…≤12 字）→ 只回一个字「安」，**不睡**；主人下一条**只发「安」**（或 `sleep_confirm_words` 里配的词）才真睡（回「嗯」），不回「安」就按正常聊天走；暗号有效期 `sleep_confirm_seconds`（600 秒），过期作废。② 到 `sleep_hour` 还没睡 → 自动睡并私聊主人一条「安」；③ `/睡眠 睡`。
- 醒来：`wake_hour`（6 点）+ 随机 `wake_jitter_minutes`（45 分钟）内自然醒；或主人私聊发 起床/醒醒/别睡/强制唤醒，或 `/睡眠 醒` 强制叫醒（叫醒词只在私聊认，群里叫不算）。
- 睡眠不会无限长：`max_sleep_hours`（10 小时）封顶，跨天算错时自动压到上限并记日志；刚醒 `min_awake_minutes`（30 分钟）内再说晚安不会马上又被哄睡。
- 醒来补回：主人排队的私聊逐条回；别人的私聊（`wake_other_private`）；每个群只看睡着以后的消息，**被 @ 的、带唤醒词的、聊到兴趣的**才回（`catchup_interest_score`）；最后私聊主人一份「🐋 早上好，我醒了（HH:MM）」摘要。错过醒来时间超过 4 小时只清队列不补回。
- 睡眠期间别人的私聊说晚安 → 只回一个「安」；群里被 @ 也不当场回，留到早上。
- 拦截用同步 CustomFilter + `event.stop_event()`（私聊在 AstrBot 里默认无条件唤醒，只能这样拦）；注意唤醒前缀会被剥掉，所以命令识别还要按命令名再认一次。
- 主动发消息（早安补回/报告）走 `send_to_umo()`：先 `context.send_message`，多 OneBot 连接抛 `ApiNotAvailable` 时再直连协议端（私聊 `send_private_msg`、群聊 `send_group_msg`，显式指定 self_id）。
- 备份与整理都放在睡眠时间（`backup.hour`=3）；页面「上下文」tab 有「睡眠模式」卡片（现在/排队/群里攒着/预计醒来 + 夜里拦下统计）。
- 配置（`sleep` 段，27 项）：`enabled` / `bot_filter` / `quiet_hour`(1) / `sleep_hour`(2) / `wake_hour`(6) / `wake_jitter_minutes`(45) / `goodnight_keywords` / `goodnight_reply`(安) / `sleep_confirm_enabled`(开) / `sleep_confirm_seconds`(600) / `sleep_confirm_words`(安) / `sleep_confirm_reply`(嗯) / `wake_keywords` / `other_goodnight_reply` / `wake_other_private` / `catchup_enabled` / `catchup_max_groups`(8) / `catchup_max_messages`(30) / `catchup_interest_score`(0.2) / `morning_report` / `notify_on_auto_sleep` / `max_pending_per_umo`(20) / `max_group_buffer`(40) / `max_sleep_hours`(10) / `min_awake_minutes`(30) / `check_interval_seconds`(30) / `reply_style`。
- 关掉对暗号（回到「说晚安立刻睡」）：`sleep_confirm_enabled=false`。
- 回滚：关 `sleep.enabled`（或先 `/睡眠 醒`），拦截与巡检立刻停。

## 人格

- 本项目**不规定、也不限制人格**：性格、语气、称呼、口头禅全部由你在 AstrBot「人格设定」里写的那份 prompt 决定，插件不写死任何人设文本，也不会因为插件而改你的性格。
- 插件只做两件跟人格有关的事：① 读取当前人格做备份 / 恢复（`/备份`、`/压缩` 后复原）；② 在每轮请求里补「谁是主人、该听谁的话、别泄露主人隐私」这类身份与权限规则（`owner` 段可整体关掉）。语气怎么说话，插件一句都不管。

## 隐私：敏感信息只私聊主人

- 上下文用量（`/上下文`）、备份列表/详情（`/备份`）、压缩结果（`/压缩`）、上下文快满提醒、每晚整理报告，**一律只私聊发给主人**。
- 群里只会收到「…私聊发你了」这类提示，不含任何具体内容；发不出去时群里也只提示一句，绝不外泄。
- 配置在 `notify` 段：`private_only`（默认开）、`private_umos`（主人私聊会话）、`nightly_report`（默认开）、`report_only_own_profile`（默认开，汇报只算自己配置档案）、`report_profile_ids`（默认空，留空按机器人平台自动识别）、`report_channel`（默认 `official` = 官方机器人 markdown，可选 `event` / `private`）、`report_platform_id`（默认 `qq_official_bot`）、`report_target`（默认主人 openid）、`report_use_markdown`（默认开）、`report_fallback`（默认开，官方通道失败退回主人私聊）、`command_platforms`（默认 `["qq_official_bot"]`，只有官方机器人能触发指令；填 `*` 不限制）、`command_private_enabled`（默认开，私聊能用指令）、`command_group_enabled`（默认关，群聊能用指令）、`command_master_only`（默认开，只允许主人；关掉后该机器人上所有成员都能用，不建议关）、`command_group_master_ids`（默认空，群聊里额外认作主人的成员 ID —— QQ 官方机器人的群成员 openid 和私聊不一样，群里发 /sid 查）。
- qq_official（QQ 官方机器人）平台不支持主动消息；主人从 QQ 官方那边触发时会改走 aiocqhttp(aiocqhttp) 私聊兜底。

## 汇报通道：官方机器人 + markdown 表格

- 个人 QQ（aiocqhttp → OneBot v11）不渲染 markdown，所以 `/报告`、每晚 3 点整理、`/费用`、`/上下文 全部`、`/备份 列表|恢复|删除`、早安报告都改成从 **QQ 官方机器人「官方机器人」**（`platform_id=qq_official_bot`）以 markdown 发出；个人 QQ 只回一句「…发到官方机器人那边了。」（群里不吭声）。
- 汇报是「结构化模型 + 双渲染」：`report_feature.build_model()` 抓数据 → `render_markdown()` 排表格（官方机器人用）/ `render_plain()` 缩进列表（通道不支持 markdown 时降级）。表格：🗂 备份、📦 资料、🌙 夜里、💬 每个对话的上下文、💰 费用、🗃 备份里还有。
- 指令不会再被大模型闲聊复读：`_wrap_command_handlers()` 给所有指令 handler 前置 `event.should_call_llm(True)`，只靠 `stop_event()` 不可靠（LLM 判断早于生成器收尾）。
- 回滚：`notify.report_channel=private` 回到原主人私聊纯文本；`report_use_markdown=false` 官方机器人也发纯文本。

## 稳定性与容错（v2.4.0）

- 指令平台限制现在也管住 AstrBot 内置指令：`/help` `/reset` `/new` `/stop` `/stats` `/sid` `/name` `/provider` `/set` `/unset` 在非白名单平台（默认只有官方机器人）一律拦下（主人私聊收到提示、其他人静默），避免在个人号上误触发 `/reset` 清会话。
- `/help` 别名只在「整条消息就是一个命令词」时生效：`help me 看下这个报错`、`帮助我看看这个` 这类正常聊天不会再被当成命令吞掉。
- 页面保存配置做类型校验与量纲钳制（比例 0~1、百分比 0~100、小时/分钟、份数上限），非法项进 `rejected` 不落盘。
- 定时循环更抗错：备份时间、巡检间隔写错自动回落默认值并 warning，循环不会因一次异常停掉；群聊压缩循环可重启、异常后 60 秒自愈。
- 上下文状态按会话保存人格复原块（旧全局格式自动丢弃并警告），状态文件超 200 条按时间淘汰。
- 群档案整理：每次每群尝试失败不影响其他群，多群时按游标轮转；总结有超时（`summary_timeout_seconds`）。
- 发言记录：超长 base64 引用换成占位符（不再因单条消息过大丢整条记录），表情包/小黄脸标签与图片下标不再错位；视觉转述有超时（`caption_timeout_seconds`）。
- 兴趣：合并同名时保留 `master`/`persona` 身份与别名，正则缓存有上限；唤醒缓存会过期清理。
- 费用：时间统一按北京时间显示，节假日峰谷价配置支持多种写法。
- 联网搜索：支持手填 `api_key`，状态文件原子写；抓取与搜索各自独立限额（`fetch_daily_limit`）。

## 配置

统一在插件配置里分 13 段：`owner` / `group_wake` / `humanizer` / `interest` / `meme` / `search` / `speaker` / `cost` / `sleep` / `knowledge` / `backup` / `context_guard` / `notify`。
页面「配置」标签也能直接改并保存：保存时按 `_conf_schema.json` 逐项校验类型与量纲（写错的项不写入并在返回里列出 `rejected`），改完立即热应用；备份/群聊压缩的开关与时间会立即生效。
