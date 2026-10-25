"""大肥鱼人味化（astrbot_plugin_dafeiyu_humanizer）

在 AstrBot 框架内，把 LLM 的回复整理成更像真人用 QQ 聊天的样子：

1. 清洗 markdown、括号动作旁白、AI 腔/客服腔、思考过程泄漏；
2. 把长回复拆成 1~3 条短气泡，让 QQ 上看起来像连发几条短消息；
3. 限制每条回复里保留的表情包/图片数量（默认 1 张）；
4. 强制纯文本发送（关闭 markdown 渲染与文转图）；
5. 轻量清理历史上下文中的格式残留，减少一点 token 消耗；
6. 在 webchat 等会显示思考过程的平台上隐藏 reasoning。

只对 bot_filter 里配置的 bot 生效（默认 qq_official_bot，即“大肥鱼”），
webchat 测试会话默认也生效，便于调试。
"""

# """统一插件 astrbot_plugin_dafeiyu_core 的 humanizer 功能模块。
#
# 由旧插件 astrbot_plugin_dafeiyu_humanizer/main.py 机械转换而来：Main 类改名为
# HumanizerFeature（普通类，去掉 Star 基类与 __init__/initialize/terminate），
# 方法上的 filter/llm_tool 装饰器保留，由统一插件的 Main 多继承并复用。"""
from __future__ import annotations
import asyncio
import random
import re
import time
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import ResultContentType
BT = chr(96)
DEFAULTS = {'enabled': True, 'bot_filter': [], 'also_webchat': True, 'reply_delay_seconds': 0.8, 'max_segments': 3, 'max_chars_per_segment': 30, 'max_images': 1, 'image_reply_probability': 45, 'image_min_gap_seconds': 120, 'strip_markdown': True, 'strip_parenthetical': True, 'strip_ai_talk': True, 'suppress_reasoning_display': True, 'force_plain_text': True, 'style_reminder': True, 'sanitize_context': True, 'context_sanitize_max_messages': 24}
STYLE_REMINDER = '\n\n【聊天风格】像真人用 QQ 聊天：一次回一两句话就行，不要用 markdown、列表、标题，不要写括号动作旁白或内心戏，不要自称 AI/助手/模型，不要输出思考过程。'
SENT_END = '。！？!?~…'
SPLIT_RE = re.compile('[^。！？!?~…\\n]*[。！？!?~…]+|[^。！？!?~…\\n]+')
# 单条气泡太长时优先在这些字符后面断开（尽量落在标点/空格处，别把话切碎）
SEG_BREAKS = set('，,。.！!？?；;：:、~～…）)」』】 \t\u3000')
PAREN_RE = re.compile('[（(]([^（）()]{0,120})[）)]')
ACTION_WORDS = ('叹', '笑', '摸', '歪', '看', '盯', '眨', '甩', '伸', '摊', '挠', '抱', '捏', '愣', '沉默', '无奈', '小声', '嘀咕', '翻白眼', '挑眉', '耸肩', '低头', '抬头', '脸红', '嘴角', '尾巴', '耳朵', '鲸', '鱼', '挥舞', '拍', '戳', '咬', '嚼', '摇', '蹦', '转圈', '趴', '躺', '跪', '捂', '扶额', '目光', '眼神', '动作', '表情', '心里', '内心', '心想', '想着', '随后', '于是', '然后', '顿了顿', '顿了顿', '轻笑', '轻笑', '哼了', '哼了')
AI_SENT_RE = re.compile('(作为(一个)?(?:AI|人工智能|语言模型|助手|机器人|虚拟助手)|我是(一个)?(?:AI|人工智能|语言模型|AI助手)|有什么(?:可以|能)帮|需要我(?:帮忙|帮你|帮)|能帮到你|希望对你有帮助|很高兴(?:能)?为你|还有什么(?:可以|需要)|您可以(?:随时)?(?:告诉|询问|联系))', re.I)
GREETING_PREFIX_RE = re.compile('^(?:好的|好滴|好呀|收到|收到啦|明白了|了解|没问题|当然可以)[，,。！!～~]\\s*')

_LAST_IMAGE_AT: dict[str, float] = {}

class HumanizerFeature:

    def _cfg(self, key, default=None):
        try:
            value = self.config.get(key)
        except Exception:
            return DEFAULTS.get(key, default)
        if value is None:
            return DEFAULTS.get(key, default)
        return value

    def _should_handle(self, event: AstrMessageEvent) -> bool:
        if not bool(self._cfg('enabled', True)):
            return False
        umo = getattr(event, 'unified_msg_origin', '') or ''
        platform = ''
        try:
            platform = event.get_platform_name() or ''
        except Exception:
            platform = ''
        bot_filter = self._cfg('bot_filter', DEFAULTS['bot_filter']) or []
        if not bot_filter:
            return True
        if isinstance(bot_filter, str):
            bot_filter = [bot_filter]
        if any((str(b) and str(b) in umo for b in bot_filter)):
            return True
        if platform == 'webchat' and bool(self._cfg('also_webchat', True)):
            return True
        return False

    @filter.on_llm_request(priority=5100)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self._should_handle(event):
            return
        try:
            if bool(self._cfg('suppress_reasoning_display', True)):
                event.set_extra('enable_reasoning', False)
                event.set_extra('_llm_reasoning_content', None)
            if bool(self._cfg('style_reminder', True)):
                hint = STYLE_REMINDER
                sys_prompt = req.system_prompt or ''
                if hint.strip() not in sys_prompt:
                    req.system_prompt = sys_prompt + hint
            if bool(self._cfg('sanitize_context', True)):
                self._sanitize_context(req)
        except Exception as exc:
            logger.warning(f'[dafeiyu_humanizer] on_llm_request 处理失败: {exc}')

    @filter.on_llm_response(priority=5100)
    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse):
        if not self._should_handle(event):
            return
        try:
            event.set_extra('_llm_reasoning_content', None)
            chain = getattr(response, 'result_chain', None)
            if chain is not None and getattr(chain, 'chain', None):
                cleaned = self._process_chain(list(chain.chain), split=False)
                if cleaned:
                    chain.chain = cleaned
        except Exception as exc:
            logger.warning(f'[dafeiyu_humanizer] on_llm_response 处理失败: {exc}')

    @filter.on_decorating_result(priority=5000)
    async def on_decorating_result(self, event: AstrMessageEvent):
        if not self._should_handle(event):
            return
        result = event.get_result()
        if result is None:
            return
        if getattr(result, 'result_content_type', None) == ResultContentType.STREAMING_RESULT:
            # 流式结果由平台适配器边生成边发（chain 是空的、内容在 async_stream 里），这里拆不了条；
            # 能做的是把 markdown 渲染和文转图关掉，保证吐给平台的是纯文本。
            try:
                if bool(self._cfg('force_plain_text', True)):
                    result.use_markdown_ = False
                    result.use_t2i_ = False
                logger.debug('[dafeiyu_humanizer] 流式模式跳过拆条/清洗: %s', getattr(event, 'unified_msg_origin', '') or '')
            except Exception as exc:
                logger.warning(f'[dafeiyu_humanizer] 流式模式强制纯文本失败: {exc}')
            return
        if not getattr(result, 'chain', None):
            return
        try:
            try:
                is_llm = result.is_llm_result() or result.is_model_result()
            except Exception:
                is_llm = True
            if not is_llm:
                return
            delay = float(self._cfg('reply_delay_seconds', 0.0) or 0.0)
            if delay > 0:
                await asyncio.sleep(random.uniform(0.1, delay))
            new_chain = self._process_chain(list(result.chain), split=True)
            if not new_chain:
                return
            new_chain = self._maybe_trim_images(event, new_chain)
            result.chain = new_chain
            if bool(self._cfg('force_plain_text', True)):
                result.use_markdown_ = False
                result.use_t2i_ = False
            event.set_extra('_llm_reasoning_content', None)
        except Exception as exc:
            logger.warning(f'[dafeiyu_humanizer] on_decorating_result 处理失败: {exc}')

    def _maybe_trim_images(self, event, chain):
        """配图自然度：按概率/间隔决定这条回复要不要带表情包。"""
        try:
            if not any(isinstance(c, Image) for c in chain):
                return chain
            prob = float(self._cfg('image_reply_probability', DEFAULTS['image_reply_probability']) or 0.0)
            prob = max(0.0, min(100.0, prob))
            gap = float(self._cfg('image_min_gap_seconds', DEFAULTS['image_min_gap_seconds']) or 0.0)
            umo = getattr(event, 'unified_msg_origin', '') or ''
            now = time.time()
            last = _LAST_IMAGE_AT.get(umo, 0.0)
            reason = ''
            if prob <= 0:
                reason = 'probability=0'
            elif gap > 0 and last and (now - last) < gap:
                reason = 'gap %.0fs < %.0fs' % (now - last, gap)
            elif random.uniform(0, 100) >= prob:
                reason = 'roll >= %.0f%%' % prob
            if not reason:
                _LAST_IMAGE_AT[umo] = now
                if len(_LAST_IMAGE_AT) > 500:
                    for key in list(_LAST_IMAGE_AT.keys())[:-200]:
                        _LAST_IMAGE_AT.pop(key, None)
                return chain
            kept = [c for c in chain if not isinstance(c, Image)]
            if not any(isinstance(c, Plain) and (c.text or '').strip() for c in kept):
                return chain
            logger.debug(f'[dafeiyu_humanizer] 这次不带图（{reason}）')
            return kept
        except Exception as exc:
            logger.warning(f'[dafeiyu_humanizer] 配图概率处理失败（保留原样）: {exc}')
            return chain

    def _process_chain(self, chain, split: bool):
        texts = []
        head = []
        tail = []
        images = []
        for comp in chain:
            if isinstance(comp, Plain):
                if comp.text and comp.text.strip():
                    texts.append(comp.text)
            elif isinstance(comp, Image):
                images.append(comp)
            elif isinstance(comp, (Reply, At)):
                head.append(comp)
            else:
                tail.append(comp)
        raw = '\n'.join(texts)
        cleaned = self._clean_text(raw)
        if not cleaned.strip():
            return chain
        segments = self._split_segments(cleaned) if split else [cleaned]
        segments = [s for s in segments if s and s.strip()]
        if not segments:
            return chain
        max_images = int(self._cfg('max_images', 1) or 0)
        if max_images >= 0:
            images = images[:max_images]
        else:
            images = images
        keep_head = head
        keep_tail = tail
        return keep_head + [Plain(s) for s in segments] + keep_tail + images

    def _split_segments(self, text: str):
        max_seg = int(self._cfg('max_segments', 3) or 1)
        if max_seg < 1:
            max_seg = 1
        parts = [p.strip() for p in SPLIT_RE.findall(text) if p and p.strip()]
        if not parts:
            return [text.strip()] if text.strip() else []
        # 先按标点断句，再把过长的句子按 max_chars_per_segment 切短：
        # max_segments 只管「最多几条」，单条太长时不会再整段糊成一条几百字的气泡。
        max_chars = int(self._cfg('max_chars_per_segment', DEFAULTS['max_chars_per_segment']) or 0)
        if max_chars > 0:
            pieces = []
            for part in parts:
                pieces.extend(self._chunk_text(part, max_chars))
        else:
            pieces = parts
        pieces = [p for p in pieces if p and p.strip()]
        if not pieces:
            return [text.strip()] if text.strip() else []
        if len(pieces) <= max_seg:
            return pieces
        # 超过条数上限：把碎片均分成 max_seg 条（不再「前几条短、最后一条巨长」）
        total = sum(len(p) for p in pieces)
        target = max(1, -(-total // max_seg))
        groups = []
        buf = ''
        for piece in pieces:
            if buf and len(buf) + len(piece) > target:
                groups.append(buf)
                buf = piece
            else:
                buf += piece
        if buf:
            groups.append(buf)
        while len(groups) > max_seg:
            tail = groups.pop()
            if not groups:
                groups = [tail]
                break
            groups[-1] = groups[-1] + tail
        return [g for g in groups if g]

    def _chunk_text(self, text: str, max_chars: int) -> list[str]:
        """把一段话按 max_chars 切成几条：中文按字符切，尽量断在标点/空格后面。"""
        text = (text or '').strip()
        if not text:
            return []
        if max_chars <= 0 or len(text) <= max_chars:
            return [text]
        out = []
        rest = text
        floor = max(1, max_chars // 2)
        while len(rest) > max_chars:
            cut = 0
            for idx in range(max_chars - 1, floor - 1, -1):
                if rest[idx] in SEG_BREAKS:
                    cut = idx + 1
                    break
            if cut <= 0:
                cut = max_chars
            head = rest[:cut].strip()
            if head:
                out.append(head)
            rest = rest[cut:].lstrip()
        if rest.strip():
            out.append(rest.strip())
        return out

    def _clean_text(self, text: str) -> str:
        if not text:
            return ''
        t = text.replace('\r\n', '\n').replace('\r', '\n')
        t = re.sub('^\\s*🤔\\s*思考[:：][\\s\\S]*?────\\s*', '', t)
        t = re.sub('<think(?:ing)?>[\\s\\S]*?</think(?:ing)?>', '', t, flags=re.I)
        t = re.sub('</?think(?:ing)?>', '', t, flags=re.I)
        t = t.replace('────', '')
        if bool(self._cfg('strip_markdown', True)):
            t = re.sub(BT * 3 + '[\\s\\S]*?' + BT * 3, '', t)
            t = re.sub(BT + '([^' + BT + '\\n]+)' + BT, '\\1', t)
            t = t.replace(BT, '')
            t = re.sub('\\*\\*([^*\\n]+)\\*\\*', '\\1', t)
            t = re.sub('__([^_\\n]+)__', '\\1', t)
            t = re.sub('(?<![*\\w])\\*([^*\\n]+)\\*(?![*\\w])', '\\1', t)
            t = re.sub('^\\s{0,3}#{1,6}\\s*', '', t, flags=re.M)
            t = re.sub('^\\s{0,3}>\\s?', '', t, flags=re.M)
            t = re.sub('^\\s{0,3}[-*+]\\s+', '', t, flags=re.M)
            t = re.sub('^\\s{0,3}\\d+[.、)]\\s+', '', t, flags=re.M)
            t = re.sub('^\\s{0,3}(?:-{3,}|\\*{3,}|_{3,})\\s*$', '', t, flags=re.M)
            t = re.sub('\\[([^\\]\\n]{1,80})\\]\\([^)\\n]+\\)', '\\1', t)
            # 只删 HTML 标签式的尖括号内容；<https://...>、<@123> 这类正常内容留着
            t = re.sub('</?(?![a-zA-Z][a-zA-Z0-9+.-]*://)[a-zA-Z][^<>]{0,50}>', '', t)
            t = t.replace('|', ' ')
        if bool(self._cfg('strip_parenthetical', True)):
            t = self._strip_parens(t)
        if bool(self._cfg('strip_ai_talk', True)):
            t = self._strip_ai_sentences(t)
        t = re.sub('[\\[【](?:图片|表情包?|语音|视频|文件|动画表情|image|img|sticker)[\\]】]', '', t, flags=re.I)
        t = re.sub('&&[^&\\n]{1,20}&&', '', t)
        t = re.sub('(?<![A-Za-z0-9]):[a-zA-Z0-9_+-]{1,20}:(?![A-Za-z0-9])', '', t)
        t = re.sub('[ \\t\\u3000]{2,}', ' ', t)
        t = re.sub('\\n{3,}', '\n\n', t)
        early = GREETING_PREFIX_RE.sub('', t, count=1)
        if early.strip():
            t = early
        return t.strip()

    def _strip_parens(self, text: str) -> str:

        def _repl(m):
            inner = (m.group(1) or '').strip()
            if not inner:
                return ''
            if re.search('(?:图片|表情|发送|image|img)', inner, re.I):
                return ''
            if any((w in inner for w in ACTION_WORDS)):
                return ''
            before = text[:m.start()].rstrip()
            at_sentence_start = not before or before[-1] in SENT_END + '\n'
            if len(inner) <= 12 and at_sentence_start:
                return ''
            return m.group(0)
        try:
            return PAREN_RE.sub(_repl, text)
        except Exception:
            return text

    def _strip_ai_sentences(self, text: str) -> str:
        parts = re.split('(?<=[。！？!?~…\\n])', text)
        kept = [p for p in parts if not AI_SENT_RE.search(p)]
        result = ''.join(kept).strip()
        return result if result else text

    def _sanitize_context(self, req: ProviderRequest):
        ctxs = req.contexts or []
        if not ctxs:
            return
        limit = int(self._cfg('context_sanitize_max_messages', 24) or 24)
        start = max(0, len(ctxs) - limit)
        for ctx in ctxs[start:]:
            if not isinstance(ctx, dict):
                continue
            if ctx.get('role') != 'assistant':
                continue
            content = ctx.get('content')
            if isinstance(content, str) and content.strip():
                cleaned = self._clean_text(content)
                if cleaned:
                    ctx['content'] = cleaned
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get('type') == 'text':
                        txt = part.get('text') or ''
                        if txt.strip():
                            cleaned = self._clean_text(txt)
                            if cleaned:
                                part['text'] = cleaned
