"""图片/表情包/小黄脸 的区分（media_kind）

用户需求（m13041 / m13090）：
- 「还有很多时候大家发的表情和图片，能区分出来吗」
- 「不是，是那种自己收藏的图片表情包，很多时候人发一句话会跟一张这种表情」

问题：
- AstrBot 把 OneBot 的 image 段统一转成 Image 组件，丢掉 sub_type / summary / emoji_id，
  所以插件这边看起来「照片」和「收藏表情包」一模一样，都记成 [图片]；
- face（QQ 自带小黄脸）段只变成 Face 组件，message_str 是空的，以前直接漏掉不记录。

事实（120 个真实群、10.4 万条样本统计）：
- sub_type=0 + summary=[图片]                → 普通照片/截图
- sub_type=1/13 + summary=[动画表情]         → 收藏/贴图表情包（无名字）
- sub_type=0 + emoji_id 或 summary=名字       → 商城/收藏表情包（summary 常是名字，如 [揪你舌头]）
- sub_type=7 + summary=[愤怒]/[？]            → QQ 表情（小黄脸一类，带名字）
- 全量样本里没有 mface 段：aiocqhttp 都转成 image 了

做法：原始事件（event.message_obj.raw_message）里还留着完整 message 段，按顺序和
Image 组件对上，就能把每张图分成 photo / sticker 并拿到名字；face 段直接用 id 查表。
"""
from __future__ import annotations

from typing import Any

try:
    from astrbot.core.message.components import Image
except Exception:  # pragma: no cover
    Image = None  # type: ignore

# QQ 自带表情 id → 中文名（OneBot v11 的 face.id）。缺的回落成「表情」。
FACE_NAMES: dict[str, str] = {
    "0": "惊讶",
    "1": "撇嘴",
    "2": "色",
    "3": "发呆",
    "4": "得意",
    "5": "流泪",
    "6": "害羞",
    "7": "闭嘴",
    "8": "睡",
    "9": "大哭",
    "10": "尴尬",
    "11": "发怒",
    "12": "调皮",
    "13": "呲牙",
    "14": "微笑",
    "15": "难过",
    "16": "酷",
    "18": "抓狂",
    "19": "吐",
    "20": "偷笑",
    "21": "可爱",
    "22": "白眼",
    "23": "傲慢",
    "24": "饥饿",
    "25": "困",
    "26": "惊恐",
    "27": "流汗",
    "28": "憨笑",
    "29": "悠闲",
    "30": "奋斗",
    "31": "咒骂",
    "32": "疑问",
    "33": "嘘",
    "34": "晕",
    "35": "折磨",
    "36": "衰",
    "37": "骷髅",
    "38": "敲打",
    "39": "再见",
    "41": "发抖",
    "42": "爱情",
    "43": "跳跳",
    "46": "猪头",
    "49": "拥抱",
    "53": "蛋糕",
    "56": "刀",
    "59": "便便",
    "60": "咖啡",
    "63": "玫瑰",
    "64": "凋谢",
    "66": "爱心",
    "67": "心碎",
    "74": "太阳",
    "75": "月亮",
    "76": "赞",
    "77": "踩",
    "78": "握手",
    "79": "胜利",
    "85": "飞吻",
    "86": "怄火",
    "89": "西瓜",
    "96": "冷汗",
    "97": "擦汗",
    "98": "抠鼻",
    "99": "鼓掌",
    "100": "糗大了",
    "101": "坏笑",
    "102": "左哼哼",
    "103": "右哼哼",
    "104": "哈欠",
    "105": "鄙视",
    "106": "委屈",
    "107": "快哭了",
    "108": "阴险",
    "109": "左亲亲",
    "110": "吓",
    "111": "可怜",
    "112": "菜刀",
    "114": "篮球",
    "116": "示爱",
    "118": "抱拳",
    "119": "勾引",
    "120": "拳头",
    "121": "差劲",
    "123": "NO",
    "124": "OK",
    "125": "转圈",
    "129": "挥手",
    "137": "鞭炮",
    "144": "喝彩",
    "146": "爆筋",
    "147": "棒棒糖",
    "169": "手枪",
    "171": "茶",
    "172": "眨眼睛",
    "173": "泪奔",
    "174": "无奈",
    "175": "卖萌",
    "176": "小纠结",
    "177": "喷血",
    "178": "斜眼笑",
    "179": "doge",
    "181": "戳一戳",
    "182": "笑哭",
    "183": "我最美",
    "185": "羊驼",
    "187": "幽灵",
    "201": "点赞",
    "212": "托腮",
    "262": "脑阔疼",
    "263": "沧桑",
    "264": "捂脸",
    "265": "辣眼睛",
    "266": "哦哟",
    "267": "头秃",
    "268": "问号脸",
    "269": "暗中观察",
    "270": "emm",
    "271": "吃瓜",
    "272": "呵呵哒",
    "273": "我酸了",
    "277": "汪汪",
    "281": "无眼笑",
    "282": "敬礼",
    "283": "狂笑",
    "284": "面无表情",
    "285": "摸鱼",
    "286": "魔鬼笑",
    "287": "哦",
    "289": "睁眼",
    "293": "摸锦鲤",
    "294": "期待",
    "295": "拿到红包",
    "297": "拜谢",
    "298": "元宝",
    "299": "牛啊",
    "300": "胖三斤",
    "302": "左拜年",
    "303": "右拜年",
    "305": "右亲亲",
    "306": "牛气冲天",
    "307": "喵喵",
    "311": "打call",
    "312": "变形",
    "314": "仔细分析",
    "317": "菜汪",
    "318": "崇拜",
    "319": "比心",
    "320": "庆祝",
    "323": "嫌弃",
    "324": "吃糖",
    "325": "惊吓",
    "326": "生气",
    "332": "举牌牌",
    "333": "烟花",
    "334": "虎虎生威",
    "336": "豹富",
    "337": "花朵脸",
    "338": "我想开了",
    "339": "舔屏",
    "341": "打招呼",
    "342": "酸Q",
    "343": "我方了",
    "344": "大怨种",
    "345": "红包多多",
    "346": "你真棒棒",
    "347": "大展宏兔",
    "349": "坚强",
    "350": "贴贴",
    "351": "敲敲",
    "352": "咦",
    "353": "拜托",
    "354": "尊嘟假嘟",
    "355": "耶",
    "356": "666",
    "357": "裂开",
    "392": "龙年快乐",
    "393": "新年中龙",
    "394": "新年大龙",
    "395": "略略略",
    "396": "龙年快乐",
    "424": "按钮"
}

_PHOTO_SUMMARIES = {'', '[图片]', '[Image]', '图片', '[图片消息]'}
_STICKER_NAMES_BLANK = {'动画表情', '表情', '图片', '', '[动画表情]', '[表情]'}


def _raw_segments(event) -> list[dict]:
    """原始事件里的 message 段（aiocqhttp 下是 [{'type','data'}]）；拿不到就空。"""
    try:
        raw = getattr(getattr(event, 'message_obj', None), 'raw_message', None)
        if isinstance(raw, dict):
            segs = raw.get('message') or raw.get('messages') or []
            return [seg for seg in segs if isinstance(seg, dict)]
    except Exception:
        pass
    return []


def _component_images(event) -> list[Any]:
    try:
        return [comp for comp in event.get_messages() if Image is not None and isinstance(comp, Image)]
    except Exception:
        return []


def _ref_of(comp) -> str:
    return (
        str(getattr(comp, 'path', '') or '')
        or str(getattr(comp, 'url', '') or '')
        or str(getattr(comp, 'file', '') or '')
    )


def _kind_of_segment(seg: dict) -> tuple[str, str]:
    """一个 image 段 → ('photo'|'sticker', 名字)。"""
    data = seg.get('data') if isinstance(seg, dict) else None
    if not isinstance(data, dict):
        return 'photo', ''
    sub_type = str(data.get('sub_type') or data.get('subType') or '').strip()
    summary = str(data.get('summary') or '').strip()
    name = summary.strip('[]［］').strip() if summary else ''
    if name in _STICKER_NAMES_BLANK:
        name = ''
    has_emoji = bool(data.get('emoji_id') or data.get('emoji_package_id') or data.get('key'))
    if sub_type in ('0', '') and not has_emoji and summary in _PHOTO_SUMMARIES:
        return 'photo', ''
    # 其余都当表情包：sub_type 1/7/11/13、带 emoji_id、或者 summary 本身是名字
    return 'sticker', name


def image_infos(event) -> list[dict]:
    """这条消息里的图片：按组件顺序返回 [{'ref','kind','name'}]。

    组件下标和原始 image 段下标不一定一一对应：适配器会跳过 mface / 解析失败的段，
    也可能一个段拆成多个组件，这时两边数量不一致，按下标硬套会把表情包名字标到别的图上。
    所以数量对不上时只按顺序配对到较短的那一侧，多出来的图片没有可信的段可对，
    给保守值 kind='sticker'、name=''（宁可少解说，也别把真照片当表情包或反过来）。
    """
    infos: list[dict] = []
    comps = _component_images(event)
    segs = [seg for seg in _raw_segments(event) if str(seg.get('type') or '') == 'image']
    paired = min(len(comps), len(segs))
    for index, comp in enumerate(comps):
        ref = _ref_of(comp)
        if not ref:
            continue
        kind, name = ('photo', '')
        if index < paired:
            kind, name = _kind_of_segment(segs[index])
        elif segs:
            # 段数是有的、只是数量对不上（多出来的组件）→ 保守当表情包
            kind, name = ('sticker', '')
        infos.append({'ref': ref, 'kind': kind, 'name': name})
    return infos


def face_names(event) -> list[str]:
    """这条消息里的 QQ 小黄脸：[{'id','name'}] 的名字列表。"""
    names: list[str] = []
    try:
        comps = event.get_messages()
    except Exception:
        return names
    for comp in comps:
        try:
            if getattr(comp, 'type', None) is None:
                continue
            if str(getattr(comp.type, 'value', comp.type)) != 'Face':
                continue
            fid = str(getattr(comp, 'id', '') or '')
            names.append(FACE_NAMES.get(fid, '') or '表情')
        except Exception:
            continue
    return names


def compose_label(infos: list[dict], faces: list[str], sticker_enabled: bool = True, face_enabled: bool = True) -> str:
    """拼一句短标签，例如 [表情包：揪你舌头] / [图片] / [表情：捂脸]。"""
    parts: list[str] = []
    stickers = 0
    photos = 0
    for info in infos:
        if info.get('kind') == 'sticker':
            stickers += 1
            name = str(info.get('name') or '').strip()
            if sticker_enabled:
                parts.append('[表情包]' if not name else '[表情包：%s]' % name)
        else:
            photos += 1
    if photos:
        parts.append('[图片]' if photos == 1 else '[图片×%d]' % photos)
    if face_enabled:
        for name in faces[:2]:
            parts.append('[表情：%s]' % (name or '表情'))
    return ' '.join(parts)


def sticker_names(event, enabled: bool = True) -> list[str]:
    if not enabled:
        return []
    return [str(info.get('name') or '').strip() for info in image_infos(event) if info.get('kind') == 'sticker']
