"""
中文语音口型（声母双唇闭合 + 复韵母过渡）与词级表情/手势编排器
输入：单句文本 + Edge TTS WordBoundary 时间戳（或音频总时长）
输出：
  - visemes: [{t, d, v: {aa, ih, ou, ee, oh}, bmp}]  毫秒级精准口型曲线
  - actions: [{t, d, gesture, face: {smile, browUp, browFurrow, browSad, eyeSmile, eyeWide, tilt, nod}}] 词级神态与手势关键帧
"""
from __future__ import annotations

import re
from typing import Any
from pypinyin import Style, pinyin

BILABIALS = {"b", "p", "m"}
LABIODENTALS = {"f"}

# 韵母 -> 一段或两段（复韵母平滑过渡）VRM 五元音权重
FINAL_TO_VISEMES: dict[str, list[dict[str, float]]] = {
    "a": [{"aa": 0.88}],
    "ia": [{"ih": 0.52, "ee": 0.25}, {"aa": 0.85}],
    "ua": [{"ou": 0.68}, {"aa": 0.85}],
    "ai": [{"aa": 0.80}, {"ih": 0.48, "ee": 0.32}],
    "uai": [{"ou": 0.65}, {"aa": 0.76, "ih": 0.35}],
    "ao": [{"aa": 0.82}, {"oh": 0.72, "ou": 0.25}],
    "iao": [{"ih": 0.50}, {"aa": 0.75, "oh": 0.55}],
    "an": [{"aa": 0.74, "ee": 0.22}],
    "ian": [{"ih": 0.48}, {"ee": 0.60, "aa": 0.38}],
    "uan": [{"ou": 0.66}, {"aa": 0.70}],
    "van": [{"ou": 0.62, "ih": 0.25}, {"ee": 0.55}],
    "üan": [{"ou": 0.62, "ih": 0.25}, {"ee": 0.55}],
    "ang": [{"aa": 0.84, "oh": 0.22}],
    "iang": [{"ih": 0.48}, {"aa": 0.80, "oh": 0.20}],
    "uang": [{"ou": 0.68}, {"aa": 0.80, "oh": 0.22}],
    "o": [{"oh": 0.78}],
    "uo": [{"ou": 0.68}, {"oh": 0.76}],
    "ou": [{"oh": 0.68}, {"ou": 0.72}],
    "iou": [{"ih": 0.48}, {"ou": 0.74}],
    "iu": [{"ih": 0.48}, {"ou": 0.74}],
    "ong": [{"oh": 0.66, "ou": 0.52}],
    "iong": [{"ih": 0.42}, {"oh": 0.62, "ou": 0.50}],
    "e": [{"ee": 0.58, "oh": 0.24}],
    "er": [{"ee": 0.52, "aa": 0.30}],
    "ie": [{"ih": 0.52}, {"ee": 0.66}],
    "ve": [{"ou": 0.58}, {"ee": 0.64}],
    "üe": [{"ou": 0.58}, {"ee": 0.64}],
    "ei": [{"ee": 0.64}, {"ih": 0.52}],
    "uei": [{"ou": 0.64}, {"ee": 0.52, "ih": 0.40}],
    "ui": [{"ou": 0.64}, {"ee": 0.52, "ih": 0.40}],
    "en": [{"ee": 0.52, "aa": 0.20}],
    "eng": [{"ee": 0.56, "oh": 0.24}],
    "in": [{"ih": 0.66, "ee": 0.25}],
    "ing": [{"ih": 0.68, "ee": 0.28}],
    "un": [{"ou": 0.68}, {"ee": 0.42}],
    "uen": [{"ou": 0.68}, {"ee": 0.42}],
    "vn": [{"ou": 0.62, "ih": 0.30}],
    "ün": [{"ou": 0.62, "ih": 0.30}],
    "i": [{"ih": 0.68, "ee": 0.22}],
    "u": [{"ou": 0.82}],
    "v": [{"ou": 0.68, "ih": 0.28}],
    "ü": [{"ou": 0.68, "ih": 0.28}],
}


def _char_visemes(ch: str) -> tuple[str, list[dict[str, float]]]:
    """返回单字 (closure_type, vowel_targets)。"""
    if not ("\u4e00" <= ch <= "\u9fff"):
        if ch.lower() in "bpm":
            return "bmp", [{"aa": 0.35}]
        if ch.lower() in "aeiou":
            mp = {"a": "a", "e": "e", "i": "i", "o": "o", "u": "u"}[ch.lower()]
            return "", FINAL_TO_VISEMES[mp]
        return "", [{"ih": 0.32, "aa": 0.22}]

    init = pinyin(ch, style=Style.INITIALS, strict=False)[0][0].lower()
    fin = pinyin(ch, style=Style.FINALS, strict=False)[0][0].lower()
    closure = "bmp" if init in BILABIALS else ("f" if init in LABIODENTALS else "")

    # 整体认读音节 zhi/chi/shi/ri/zi/ci/si 的 i 开口度比普通齐齿呼更轻
    if fin == "i" and init in {"zh", "ch", "sh", "r", "z", "c", "s"}:
        shapes = [{"ih": 0.42, "ee": 0.18}]
    else:
        shapes = FINAL_TO_VISEMES.get(fin, [{"aa": 0.50, "ee": 0.25}])
    return closure, shapes


def _expand_word_to_visemes(word: str, start_s: float, dur_s: float) -> list[dict[str, Any]]:
    chars = [c for c in word if "\u4e00" <= c <= "\u9fff" or c.isalnum()]
    if not chars or dur_s <= 0.02:
        return []

    # 合并连续英文字母为一个音节块
    tokens: list[str] = []
    ebuf = ""
    for c in chars:
        if "\u4e00" <= c <= "\u9fff":
            if ebuf:
                tokens.append(ebuf)
                ebuf = ""
            tokens.append(c)
        else:
            ebuf += c
    if ebuf:
        tokens.append(ebuf)

    per_dur = dur_s / len(tokens)
    events: list[dict[str, Any]] = []
    cur = start_s

    for tok in tokens:
        lead_char = tok[0]
        closure, shapes = _char_visemes(lead_char)
        t_tok = cur
        avail = per_dur

        # 双唇音 b/p/m 先做 42ms 真实闭唇动作，视觉立刻像真人在说话！
        if closure == "bmp" and avail > 0.08:
            c_dur = min(0.045, avail * 0.30)
            events.append({"t": round(t_tok, 3), "d": round(c_dur, 3), "v": {"aa": 0, "ih": 0, "ou": 0.06, "ee": 0, "oh": 0}, "bmp": True})
            t_tok += c_dur
            avail -= c_dur
        elif closure == "f" and avail > 0.08:
            c_dur = min(0.035, avail * 0.24)
            events.append({"t": round(t_tok, 3), "d": round(c_dur, 3), "v": {"aa": 0.05, "ih": 0.22, "ou": 0.12, "ee": 0, "oh": 0}})
            t_tok += c_dur
            avail -= c_dur

        sub_dur = avail / len(shapes)
        for sh in shapes:
            full_v = {"aa": 0.0, "ih": 0.0, "ou": 0.0, "ee": 0.0, "oh": 0.0}
            full_v.update(sh)
            events.append({"t": round(t_tok, 3), "d": round(sub_dur, 3), "v": full_v})
            t_tok += sub_dur

        cur += per_dur

    return events


# 词级动作与微表情规则库：(正则, 手势名, 微表情参数, 持续秒数)
PHRASE_RULES: list[tuple[re.Pattern[str], str, dict[str, float], float]] = [
    (
        re.compile(r"你好|嗨|哈喽|早上好|下午好|晚上好|欢迎|再见|拜拜"),
        "wave",
        {"smile": 0.72, "eyeSmile": 0.65, "browUp": 0.35, "tilt": 0.09, "joyPulse": 0.45},
        1.9,
    ),
    (
        re.compile(r"第一|首先|先看|先说|其一|一方面"),
        "point_list",
        {"smile": 0.22, "browUp": 0.38, "eyeWide": 0.15, "nod": 0.55},
        1.8,
    ),
    (
        re.compile(r"第二|其次|另外|再看|另一方面|此外|接着"),
        "explain_open",
        {"smile": 0.26, "browUp": 0.30, "tilt": -0.06, "nod": 0.45},
        1.8,
    ),
    (
        re.compile(r"总结|总的来说|核心|重点|关键|最重要|包含|分为|整体|方案"),
        "explain_both",
        {"smile": 0.28, "browUp": 0.42, "eyeWide": 0.18, "nod": 0.70},
        2.2,
    ),
    (
        re.compile(r"但是|不过|然而|相反|对比|区别|不同|虽然"),
        "weigh_contrast",
        {"smile": 0.12, "browFurrow": 0.28, "tilt": -0.08, "nod": 0.35},
        2.0,
    ),
    (
        re.compile(r"思考|分析|可能|大概|也许|需要考虑|推敲|假设|看看"),
        "think_chin",
        {"smile": 0.10, "browFurrow": 0.32, "tilt": -0.10, "gazeAway": 1.0},
        2.1,
    ),
    (
        re.compile(r"抱歉|不好意思|遗憾|可惜|注意风险|小心|问题|困难|不足"),
        "empathy_soft",
        {"smile": 0.0, "browSad": 0.52, "tilt": 0.08, "nod": 0.25},
        2.0,
    ),
    (
        re.compile(r"太好了|很棒|非常棒|不错|恭喜|完美|成功|太棒|哈哈|开心"),
        "nod_agree",
        {"smile": 0.80, "eyeSmile": 0.72, "browUp": 0.35, "nod": 0.75, "joyPulse": 0.50},
        1.9,
    ),
    (
        re.compile(r"居然|竟然|没想到|哇|惊讶|不可思议|高达|暴涨|翻倍"),
        "surprise_soft",
        {"smile": 0.18, "browUp": 0.75, "eyeWide": 0.55, "tilt": 0.05},
        1.6,
    ),
    (
        re.compile(r"我帮|我觉得|我认为|我来|我可以|在我看来|我建议|我们"),
        "self_chest",
        {"smile": 0.48, "eyeSmile": 0.38, "browUp": 0.20, "tilt": 0.05, "nod": 0.40},
        1.7,
    ),
    (
        re.compile(r"你觉得|你看|你想|你可以|需要我|好吗|对吧|怎么样|如何|哪部分|什么"),
        "ask_user",
        {"smile": 0.45, "eyeSmile": 0.30, "browUp": 0.48, "eyeWide": 0.22, "tilt": 0.09},
        2.0,
    ),
    (
        re.compile(r"百分之|\d+(\.\d+)?"),
        "beat_emphasis",
        {"smile": 0.22, "browUp": 0.36, "nod": 0.65},
        1.3,
    ),
]


def build_speech_timeline(
    text: str,
    word_marks: list[dict[str, Any]] | None,
    total_duration_s: float,
) -> dict[str, Any]:
    """生成与音频秒数严格对齐的 visemes 与词级神态/动作关键帧。"""
    visemes: list[dict[str, Any]] = []
    word_spans: list[tuple[float, float, str]] = []

    if word_marks:
        for mk in word_marks:
            t_s = float(mk.get("offset", 0)) / 10_000_000.0
            d_s = max(0.04, float(mk.get("duration", 0)) / 10_000_000.0)
            w_txt = str(mk.get("text", "")).strip()
            if not w_txt:
                continue
            word_spans.append((t_s, d_s, w_txt))
            visemes.extend(_expand_word_to_visemes(w_txt, t_s, d_s))
        if word_spans:
            total_duration_s = max(total_duration_s, word_spans[-1][0] + word_spans[-1][1] + 0.12)
    else:
        # 无 WordBoundary（如本地 Kokoro / SAPI）：按标点停顿与字数分配时长
        clauses = [c for c in re.split(r"([，。！？；、,!?])", text) if c]
        units: list[tuple[str, float]] = []
        for seg in clauses:
            if seg in "，、,":
                units.append((seg, 0.20))
            elif seg in "。！？；!?":
                units.append((seg, 0.28))
            else:
                clean = [c for c in seg if "\u4e00" <= c <= "\u9fff" or c.isalnum()]
                if clean:
                    units.append((seg, max(0.16, len(clean) * 0.18)))
        weight_sum = sum(w for _, w in units) or 1.0
        scale = max(0.6, total_duration_s - 0.10) / weight_sum
        cur_t = 0.04
        for seg, w in units:
            dur = w * scale
            if seg not in "，。！？；、,!?":
                word_spans.append((cur_t, dur, seg))
                visemes.extend(_expand_word_to_visemes(seg, cur_t, dur))
            cur_t += dur

    # 扫描子句与词跨度，生成贴合台词含义的动作与微表情时间轴
    actions: list[dict[str, Any]] = []
    # 将相邻词拼成滑动窗口，定位关键词出现的精确音频时刻
    full_rebuilt = ""
    char_times: list[float] = []
    for t_s, d_s, w_txt in word_spans:
        n = max(1, len(w_txt))
        for idx, ch in enumerate(w_txt):
            full_rebuilt += ch
            char_times.append(t_s + (d_s * idx / n))

    last_action_t = -9.0
    if full_rebuilt:
        # 按时间顺序查找所有命中的语义触发点
        hits: list[tuple[float, str, dict[str, float], float]] = []
        for pattern, gesture, face, dur in PHRASE_RULES:
            for m in pattern.finditer(full_rebuilt):
                pos = min(m.start(), len(char_times) - 1)
                hits.append((char_times[pos], gesture, face, dur))
        hits.sort(key=lambda x: x[0])

        for hit_t, gesture, face, dur in hits:
            # 防止相隔 0.8s 内连续抢动作导致抽搐
            if hit_t - last_action_t >= 0.85:
                actions.append({
                    "t": round(max(0.0, hit_t - 0.08), 3),  # 动作比发音提前 80ms 起手（符合真实人类说话预备拍！）
                    "d": round(dur, 2),
                    "gesture": gesture,
                    "face": face,
                })
                last_action_t = hit_t

    # 如果整句是普通陈述没命中特殊词，也给一个温和自然的开口讲解拍点（杜绝木头桩子）
    if not actions:
        is_q = ("？" in text) or ("?" in text)
        actions.append({
            "t": 0.0,
            "d": round(min(2.4, max(1.4, total_duration_s * 0.85)), 2),
            "gesture": "ask_user" if is_q else "explain_open",
            "face": (
                {"smile": 0.42, "eyeSmile": 0.28, "browUp": 0.42, "tilt": 0.07}
                if is_q
                else {"smile": 0.36, "eyeSmile": 0.26, "browUp": 0.22, "nod": 0.45}
            ),
        })

    return {
        "duration": round(total_duration_s, 3),
        "visemes": visemes,
        "actions": actions,
    }
