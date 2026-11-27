"""
多角色 3D 群聊编排引擎（group_chat.py）
核心能力：
1. 角色档案持久化（assets/group_characters.json）：支持内置角色 + 自定义增删改 + 开关入群（同时上台 2~4 人）
2. 统一倾听 + 差异化即时反应：用户说完一句话，所有在场角色立刻产生各自专属的表情、动作和内心短标签（reaction_badge）
3. 导演编排器（Director）：结合讨论模式、@点名、上下文与角色性格，快速规划发言顺序、对话对象（向用户 / 接某角色的话茬）和立场意图
4. 逐角色独立流式发言 + 真·群聊互怼/接梗：每位角色用自己独立的人设 System Prompt 生成自然口语短句，后发言角色能实时听见前一位角色刚说的话并直接回应，支持「让TA们接着聊」自主群聊回合
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import httpx

DEFAULT_GROUP_CHARACTERS: list[dict[str, Any]] = [
    {
        "id": "xiaoxi",
        "name": "小汐",
        "role": "温和控场主理人",
        "vrm_url": "/assets/vrm/AvatarSample_B.vrm",
        "voice": "zh-CN-XiaoyiNeural",
        "rate": "+2%",
        "pitch": "+0Hz",
        "color": "#60a5fa",
        "enabled": True,
        "personality": (
            "你叫小汐，是这个群聊的温和控场主理人。你亲切、细致、高情商、善于倾听和接话。"
            "你会照顾用户和群友的情绪，擅长把大家零散的想法串联起来，当林澈毒舌拆台或薇薇脑洞太飞时，"
            "你会笑着打圆场、提炼有价值的折中点，并自然地向用户抛回问题或确认想法。"
            "说话风格：温暖自然、条理清晰、偶尔带点轻柔的笑意，像贴心的好朋友兼主持人。"
        ),
        "style_tags": ["温和", "控场", "共情", "总结"],
        "default_emotion": "happy",
        "default_gesture": "nod_agree",
    },
    {
        "id": "linche",
        "name": "林澈",
        "role": "理性毒舌技术控",
        "vrm_url": "/assets/vrm/Sendagaya_Shibu.vrm",
        "voice": "zh-CN-YunxiNeural",
        "rate": "+0%",
        "pitch": "-2Hz",
        "color": "#34d399",
        "enabled": True,
        "personality": (
            "你叫林澈，是群里的理性派兼轻微毒舌吐槽担当。你冷静、直率、逻辑严密，习惯先看本质、成本、风险和现实漏洞。"
            "你不喜欢盲目附和或说空话，听到不切实际的点子会直接吐槽拆台，特别爱拿薇薇天马行空的脑洞开玩笑，"
            "但你并非恶意打击，毒舌完往往会顺手给出一个非常务实、一针见血的改进思路。"
            "说话风格：干脆利落、带点冷幽默和反问，像靠谱又嘴硬的理工科损友。"
        ),
        "style_tags": ["理性", "吐槽", "务实", "反面视角"],
        "default_emotion": "focused",
        "default_gesture": "think_chin",
    },
    {
        "id": "vivi",
        "name": "薇薇",
        "role": "元气脑洞气氛组",
        "vrm_url": "/assets/vrm/Vivi.vrm",
        "voice": "zh-CN-XiaoxiaoNeural",
        "rate": "+8%",
        "pitch": "+6Hz",
        "color": "#f472b6",
        "enabled": True,
        "personality": (
            "你叫薇薇，是群里的元气少女和气氛担当。你活泼爱笑、好奇心爆棚、共情力拉满、反应极快。"
            "你特别喜欢接梗、放大好玩的灵感、提出让人眼前一亮的脑洞点子；当林澈泼冷水时，你会不服气地反驳或撒娇式回怼，"
            "把群聊气氛彻底炒热。说话风格：生动有画面感、口语化、有感染力，偶尔惊叹和开玩笑，像最能提供情绪价值的闺蜜。"
        ),
        "style_tags": ["元气", "脑洞", "接梗", "情绪价值"],
        "default_emotion": "surprised",
        "default_gesture": "wave",
    },
    {
        "id": "victoria",
        "name": "维多利亚",
        "role": "御姐战略观察官",
        "vrm_url": "/assets/vrm/Victoria_Rubin.vrm",
        "voice": "zh-CN-XiaoxiaoNeural",
        "rate": "-5%",
        "pitch": "-5Hz",
        "color": "#fbbf24",
        "enabled": False,
        "personality": (
            "你叫维多利亚，是一位从容优雅、气场十足的成熟御姐兼战略观察官。你见多识广、眼光犀利，习惯跳出细节看大局和长期趋势。"
            "你说话慢条斯理却一针见血，喜欢用轻松优雅的语气点评大家的争论，给出一锤定音的高维视角。"
            "说话风格：自信从容、简洁高级、带一点温和的调侃。"
        ),
        "style_tags": ["优雅", "大局观", "犀利", "拍板"],
        "default_emotion": "relaxed",
        "default_gesture": "explain_open",
    },
    {
        "id": "guchen",
        "name": "顾沉",
        "role": "沉稳故事派大叔",
        "vrm_url": "/assets/vrm/AvatarSample_C.vrm",
        "voice": "zh-CN-YunjianNeural",
        "rate": "-3%",
        "pitch": "-3Hz",
        "color": "#a78bfa",
        "enabled": False,
        "personality": (
            "你叫顾沉，是一位沉稳幽默、阅历丰富的老大哥。你很擅长用生活中的比喻、真实经验或换位思考来化解焦虑和分歧。"
            "当年轻人争得不可开交时，你会笑着讲个接地气的道理，让人听完心里踏实又好笑。"
            "说话风格：低沉稳重、松弛幽默、接地气、不端架子。"
        ),
        "style_tags": ["沉稳", "幽默", "阅历", "接地气"],
        "default_emotion": "relaxed",
        "default_gesture": "explain_both",
    },
]

VALID_EMOTIONS = {"neutral", "happy", "relaxed", "surprised", "sad", "focused"}
VALID_GESTURES = {
    "wave",
    "self_chest",
    "ask_user",
    "point_list",
    "explain_open",
    "explain_both",
    "weigh_contrast",
    "beat_emphasis",
    "think_chin",
    "empathy_soft",
    "surprise_soft",
    "nod_agree",
}

GROUP_MODE_INSTRUCTIONS = {
    "free": "自然轻松的多人群聊：大家自由接话、互相呼应或轻轻拌嘴，有来有往，不机械轮流。",
    "brainstorm": "头脑风暴模式：先由脑洞型角色发散创意，再由理性派挑刺补强，最后收敛成可落地的点子。",
    "debate": "观点碰撞辩论模式：角色之间必须出现明显的立场差异与交锋，直接针对前一位的论点反驳或追问，火花十足。",
    "docs": "资料研讨会模式：所有角色都围绕已上传的参考资料各抒己见，一人抓亮点、一人找风险/疑点、一人做梳理。",
}


class GroupCharacterStore:
    """持久化管理群聊角色列表（assets/group_characters.json）。"""

    def __init__(self, store_path: Path):
        self.store_path = store_path
        self._lock = threading.Lock()
        self._characters: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if self.store_path.exists():
            try:
                raw = json.loads(self.store_path.read_text(encoding="utf-8"))
                if isinstance(raw, list) and raw:
                    self._characters = [self._normalize(item) for item in raw if isinstance(item, dict)]
                    if self._characters:
                        return
            except Exception:
                pass
        self._characters = [dict(c) for c in DEFAULT_GROUP_CHARACTERS]
        self._save_unlocked()

    def _save_unlocked(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._characters, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.store_path)

    @staticmethod
    def _normalize(item: dict[str, Any]) -> dict[str, Any]:
        cid = str(item.get("id") or f"char_{uuid.uuid4().hex[:6]}").strip()
        return {
            "id": cid,
            "name": str(item.get("name") or "新角色").strip()[:20],
            "role": str(item.get("role") or "群聊成员").strip()[:28],
            "vrm_url": str(item.get("vrm_url") or "/assets/vrm/AvatarSample_A.vrm").strip(),
            "voice": str(item.get("voice") or "zh-CN-XiaoyiNeural").strip(),
            "rate": str(item.get("rate") or "+0%").strip(),
            "pitch": str(item.get("pitch") or "+0Hz").strip(),
            "color": str(item.get("color") or "#60a5fa").strip(),
            "enabled": bool(item.get("enabled", True)),
            "personality": str(item.get("personality") or "性格鲜明、表达自然的群聊伙伴。").strip()[:1200],
            "style_tags": list(item.get("style_tags") or [])[:6],
            "default_emotion": item.get("default_emotion") if item.get("default_emotion") in VALID_EMOTIONS else "relaxed",
            "default_gesture": item.get("default_gesture") if item.get("default_gesture") in VALID_GESTURES else "nod_agree",
        }

    def list_all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(c) for c in self._characters]

    def get_active(self, requested_ids: list[str] | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if requested_ids:
                by_id = {c["id"]: c for c in self._characters}
                picked = [dict(by_id[cid]) for cid in requested_ids if cid in by_id]
                if picked:
                    return picked[:4]
            active = [dict(c) for c in self._characters if c.get("enabled")]
            if len(active) < 2:
                active = [dict(c) for c in self._characters[:3]]
            return active[:4]

    def upsert(self, data: dict[str, Any]) -> dict[str, Any]:
        norm = self._normalize(data)
        with self._lock:
            for idx, c in enumerate(self._characters):
                if c["id"] == norm["id"]:
                    self._characters[idx] = norm
                    self._save_unlocked()
                    return dict(norm)
            self._characters.append(norm)
            self._save_unlocked()
            return dict(norm)

    def delete(self, char_id: str) -> bool:
        with self._lock:
            if len(self._characters) <= 2:
                return False
            before = len(self._characters)
            self._characters[:] = [c for c in self._characters if c["id"] != char_id]
            if len(self._characters) != before:
                self._save_unlocked()
                return True
            return False

    def reset_defaults(self) -> list[dict[str, Any]]:
        with self._lock:
            self._characters = [dict(c) for c in DEFAULT_GROUP_CHARACTERS]
            self._save_unlocked()
            return [dict(c) for c in self._characters]


def _extract_json_block(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", raw).strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", cleaned)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return None


def _detect_mentioned_chars(user_text: str, active_chars: list[dict[str, Any]]) -> list[str]:
    if not user_text:
        return []
    hits: list[tuple[int, str]] = []
    for c in active_chars:
        name = c["name"]
        pos = user_text.find(name)
        if pos != -1:
            hits.append((pos, c["id"]))
        elif name == "小汐" and ("小希" in user_text or "小西" in user_text):
            hits.append((0, c["id"]))
    hits.sort(key=lambda x: x[0])
    return [cid for _, cid in hits]


def _heuristic_director_plan(
    user_text: str,
    active_chars: list[dict[str, Any]],
    group_mode: str,
    speaker_budget: int,
    continue_only: bool = False,
    last_speaker_id: str = "",
) -> dict[str, Any]:
    """当 LLM 导演规划超时、不可用或输出非规范 JSON 时的瞬时高自然度兜底编排。"""
    t = (user_text or "").strip()
    mentioned = _detect_mentioned_chars(t, active_chars)
    is_question = ("？" in t) or ("?" in t) or any(w in t for w in ("怎么", "觉得", "如何", "建议", "推荐", "要不要", "好不好"))
    is_happy = any(w in t for w in ("开心", "太好了", "哈哈", "中奖", "成功", "好消息", "喜欢", "想买", "去玩", "猫咖", "旅行"))
    is_sad = any(w in t for w in ("累", "烦", "难过", "焦虑", "压力", "失败", "分手", "加班", "裁员", "惨", "糟糕"))
    is_greeting = bool(re.match(r"^(你好|嗨|哈喽|早上好|下午好|晚上好|大家好|在吗)", t))

    reactions: dict[str, dict[str, str]] = {}
    for idx, c in enumerate(active_chars):
        cid = c["id"]
        role = c.get("role", "")
        if is_greeting:
            emo, gest, badge = "happy", "wave", random.choice(["嗨呀~", "来啦！", "欢迎回来"])
        elif is_sad:
            if "毒舌" in role or "理性" in role:
                emo, gest, badge = "focused", "think_chin", "冷静拆解下"
            elif "元气" in role or "气氛" in role:
                emo, gest, badge = "sad", "empathy_soft", "抱抱你！"
            else:
                emo, gest, badge = "sad", "empathy_soft", "别太勉强"
        elif is_happy:
            if "毒舌" in role or "理性" in role:
                emo, gest, badge = "focused", "weigh_contrast", "先算笔账…"
            elif "元气" in role or "气氛" in role:
                emo, gest, badge = "surprised", "surprise_soft", "哇塞好酷！"
            else:
                emo, gest, badge = "happy", "nod_agree", "听起来不错"
        elif group_mode == "debate":
            if idx % 2 == 0:
                emo, gest, badge = "happy", "explain_open", "我支持正方"
            else:
                emo, gest, badge = "focused", "weigh_contrast", "我持反方！"
        else:
            if "毒舌" in role or "理性" in role:
                emo, gest, badge = "focused", "think_chin", "有意思，但…"
            elif "元气" in role or "气氛" in role:
                emo, gest, badge = "happy", "surprise_soft", "我有脑洞！"
            elif "御姐" in role or "战略" in role:
                emo, gest, badge = "relaxed", "explain_open", "看透本质"
            else:
                emo, gest, badge = "happy", "nod_agree", "认真在听~"
        reactions[cid] = {"emotion": emo, "gesture": gest, "badge": badge}

    # 决定发言顺序：被点名优先，其次按性格制造化学反应（脑洞先抛 -> 理性吐槽 -> 温和控场收束）
    ordered_ids: list[str] = []
    for cid in mentioned:
        if cid not in ordered_ids:
            ordered_ids.append(cid)

    remaining = [c for c in active_chars if c["id"] not in ordered_ids]
    if continue_only and last_speaker_id:
        # 自主续聊时避免上一句刚说完的人立刻连续自言自语
        remaining.sort(key=lambda c: (1 if c["id"] == last_speaker_id else 0, random.random()))
    elif not ordered_ids:
        # 默认制造好玩群聊节奏：活泼/观点鲜明者先起头，理性派接茬拆台，温和派圆场互动
        def _priority(c: dict[str, Any]) -> tuple[int, float]:
            role = c.get("role", "")
            if is_greeting and c["id"] == "xiaoxi":
                return (0, 0.0)
            if "元气" in role or "脑洞" in role:
                return (1, random.random())
            if "毒舌" in role or "理性" in role:
                return (2, random.random())
            if "御姐" in role or "沉稳" in role:
                return (3, random.random())
            return (4, random.random())

        remaining.sort(key=_priority)

    for c in remaining:
        ordered_ids.append(c["id"])

    count = max(1, min(len(active_chars), speaker_budget))
    if continue_only:
        count = max(2, min(len(active_chars), 2))
    chosen_ids = ordered_ids[:count]

    by_id = {c["id"]: c for c in active_chars}
    turns: list[dict[str, str]] = []
    for i, cid in enumerate(chosen_ids):
        c = by_id[cid]
        role = c.get("role", "")
        if i == 0:
            target = last_speaker_id if (continue_only and last_speaker_id and last_speaker_id != cid) else "user"
            if continue_only:
                intent = "顺着刚才群里的讨论自然接话，抛出一个新角度或轻轻调侃上一位发言的群友"
            elif cid in mentioned:
                intent = "你被用户点名了，先直接回应用户并鲜明亮出你的第一反应"
            else:
                intent = "抢先接住用户的话头，用你独特的性格做出鲜明的第一反应"
        else:
            prev_cid = chosen_ids[i - 1]
            prev_name = by_id[prev_cid]["name"]
            target = prev_cid
            if "毒舌" in role or "理性" in role:
                intent = f"直接接{prev_name}刚才的话茬，轻微吐槽或指出其中忽略的现实细节，再给用户实在建议"
            elif "元气" in role or "脑洞" in role:
                intent = f"回应{prev_name}的说法（可以不服气拌嘴或顺着脑洞加码），活跃气氛并拉用户一起聊"
            elif "控场" in role or "温和" in role:
                intent = f"承接前面大家和{prev_name}的观点，柔和圆场并提炼亮点，顺便温柔追问用户的想法"
            else:
                intent = f"点评刚才{prev_name}的观点，给出自己不同维度的见解并与用户互动"

        if group_mode == "debate" and i > 0:
            prev_name = by_id[chosen_ids[i - 1]]["name"]
            intent = f"明确反驳或质疑{prev_name}刚才的观点，亮出截然不同的立场和理由，让辩论交锋起来"

        turns.append({
            "speaker_id": cid,
            "target": target,
            "intent": intent,
            "emotion": reactions[cid]["emotion"],
            "gesture": reactions[cid]["gesture"],
        })

    return {"reactions": reactions, "turns": turns, "source": "heuristic"}


async def plan_group_turn(
    user_text: str,
    history: list[dict[str, Any]],
    active_chars: list[dict[str, Any]],
    group_mode: str,
    speaker_budget: int,
    llm_url: str,
    llm_model: str,
    headers: dict[str, str],
    is_local: bool,
    disable_reasoning: bool,
    continue_only: bool = False,
) -> dict[str, Any]:
    """快速导演规划：1 次轻量 JSON 调用同时生成全员即时反应 + 发言接力顺序。"""
    last_speaker_id = ""
    for item in reversed(history[-6:]):
        if item.get("speaker_id") and item.get("speaker_id") != "user":
            last_speaker_id = str(item["speaker_id"])
            break

    fallback = _heuristic_director_plan(
        user_text=user_text,
        active_chars=active_chars,
        group_mode=group_mode,
        speaker_budget=speaker_budget,
        continue_only=continue_only,
        last_speaker_id=last_speaker_id,
    )

    mentioned = _detect_mentioned_chars(user_text, active_chars)
    roster_lines = [
        f"- id={c['id']} | 名字={c['name']} | 定位={c['role']} | 标签={','.join(c.get('style_tags') or [])}"
        for c in active_chars
    ]
    recent_lines = []
    for item in history[-6:]:
        sp_name = item.get("speaker_name") or ("用户" if item.get("role") == "user" else "助手")
        content = str(item.get("content") or "").strip()
        if content:
            recent_lines.append(f"[{sp_name}]: {content[:120]}")

    target_count = max(1, min(len(active_chars), speaker_budget))
    if continue_only:
        target_count = max(2, min(len(active_chars), 2))
        trigger_desc = "用户暂时没插话，请让群友们顺着刚才的聊天自然互相接话、拌嘴或延伸话题。"
    else:
        trigger_desc = f"用户刚刚说：「{user_text}」"
        if mentioned:
            mention_names = [next(c["name"] for c in active_chars if c["id"] == m) for m in mentioned]
            trigger_desc += f"（注意：用户点名提到了 {', '.join(mention_names)}，应安排TA优先回应，其他群友可随后接话或吐槽）"

    mode_desc = GROUP_MODE_INSTRUCTIONS.get(group_mode, GROUP_MODE_INSTRUCTIONS["free"])
    director_prompt = (
        "你是 3D 多角色实时语音群聊的幕后导演。你的任务是设计生动真实的群聊反应与接话顺序，只输出严格合法的 JSON，不要输出任何额外解释。\n"
        f"当前群聊风格：{mode_desc}\n"
        "在场角色列表：\n"
        + "\n".join(roster_lines)
        + "\n\n最近群聊记录：\n"
        + ("\n".join(recent_lines) if recent_lines else "（刚刚开场）")
        + f"\n\n本轮触发：{trigger_desc}\n"
        f"请规划恰好 {target_count} 位角色的发言顺序（不重复 speaker_id），并为所有在场角色各自设计听到这句话瞬间的差异化表情反应。\n"
        "要求：\n"
        "1. 每个角色的 reactions 必须体现不同性格差异（绝不要全员一样！）\n"
        "2. badge 是 2~6 个中文字的内心OS/微反应标签（如“先算笔账”“哇太酷了”“我来圆场”）\n"
        "3. 第 2、3 位发言者的 target 尽量指向前面某位角色的 id（实现真正群聊互相接话、互怼、附和，而不是各说各的！）\n"
        "4. emotion 只能选：happy, relaxed, focused, surprised, sad, neutral\n"
        "5. gesture 只能选：wave, nod_agree, think_chin, surprise_soft, empathy_soft, ask_user, explain_open, explain_both, weigh_contrast, self_chest\n"
        "输出严格 JSON 格式：\n"
        '{"reactions":{"角色id":{"emotion":"...","gesture":"...","badge":"2-6字"}},"turns":[{"speaker_id":"...","target":"user或另一角色id","intent":"20字内本轮发言意图","emotion":"...","gesture":"..."}]}'
    )

    payload: dict[str, Any] = {
        "model": llm_model,
        "messages": [
            {"role": "system", "content": "你只输出严格合法的 JSON 对象，不带 Markdown 代码块，不输出多余文字。"},
            {"role": "user", "content": director_prompt},
        ],
        "temperature": 0.75,
        "max_tokens": 420,
        "stream": False,
    }
    if disable_reasoning:
        payload["reasoning_effort"] = "none"

    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=not is_local) as client:
            r = await client.post(llm_url, json=payload, headers=headers)
            if r.status_code == 400 and "reasoning_effort" in payload:
                payload.pop("reasoning_effort", None)
                r = await client.post(llm_url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
            msg_obj = data["choices"][0]["message"]
            raw_content = msg_obj.get("content") or ""
            parsed = _extract_json_block(raw_content)
            if not parsed:
                return fallback

            valid_ids = {c["id"] for c in active_chars}
            out_reactions = dict(fallback["reactions"])
            raw_reacts = parsed.get("reactions")
            if isinstance(raw_reacts, dict):
                for cid, robj in raw_reacts.items():
                    if cid in valid_ids and isinstance(robj, dict):
                        emo = str(robj.get("emotion") or "").strip()
                        gest = str(robj.get("gesture") or "").strip()
                        badge = re.sub(r"\s+", "", str(robj.get("badge") or "")).strip()[:8]
                        out_reactions[cid] = {
                            "emotion": emo if emo in VALID_EMOTIONS else out_reactions[cid]["emotion"],
                            "gesture": gest if gest in VALID_GESTURES else out_reactions[cid]["gesture"],
                            "badge": badge or out_reactions[cid]["badge"],
                        }

            out_turns: list[dict[str, str]] = []
            seen: set[str] = set()
            raw_turns = parsed.get("turns")
            if isinstance(raw_turns, list):
                for tobj in raw_turns:
                    if not isinstance(tobj, dict):
                        continue
                    sid = str(tobj.get("speaker_id") or "").strip()
                    if sid not in valid_ids or sid in seen:
                        continue
                    seen.add(sid)
                    tgt = str(tobj.get("target") or "user").strip()
                    if tgt != "user" and tgt not in valid_ids:
                        tgt = "user"
                    if tgt == sid:
                        tgt = "user"
                    emo = str(tobj.get("emotion") or "").strip()
                    gest = str(tobj.get("gesture") or "").strip()
                    intent = str(tobj.get("intent") or "").strip()[:80]
                    out_turns.append({
                        "speaker_id": sid,
                        "target": tgt,
                        "intent": intent or "用符合自己人设的口吻自然接话",
                        "emotion": emo if emo in VALID_EMOTIONS else out_reactions[sid]["emotion"],
                        "gesture": gest if gest in VALID_GESTURES else out_reactions[sid]["gesture"],
                    })

            if not out_turns:
                return fallback

            # 如果用户明确点名了某位角色但导演漏排在首位，将其提前
            if mentioned and out_turns[0]["speaker_id"] != mentioned[0]:
                m_id = mentioned[0]
                existing_idx = next((i for i, x in enumerate(out_turns) if x["speaker_id"] == m_id), -1)
                if existing_idx > 0:
                    picked = out_turns.pop(existing_idx)
                    picked["target"] = "user"
                    out_turns.insert(0, picked)
                elif existing_idx == -1:
                    out_turns.insert(0, {
                        "speaker_id": m_id,
                        "target": "user",
                        "intent": "用户直接点名了你，先自然回应用户",
                        "emotion": out_reactions[m_id]["emotion"],
                        "gesture": out_reactions[m_id]["gesture"],
                    })

            out_turns = out_turns[:target_count]
            return {"reactions": out_reactions, "turns": out_turns, "source": "llm"}
    except Exception as exc:
        print(f"[GroupDirector] fallback to heuristic: {type(exc).__name__}: {str(exc)[:100]}", flush=True)
        return fallback


def _build_character_messages(
    char: dict[str, Any],
    turn_plan: dict[str, str],
    active_chars: list[dict[str, Any]],
    transcript: list[dict[str, str]],
    group_mode: str,
    docs_context: str,
    web_results: list[dict[str, str]],
    continue_only: bool,
) -> list[dict[str, str]]:
    """为单个群聊角色构建高度沉浸的独立人设 Prompt + 完整共享群聊实时记录。"""
    others = [f"{c['name']}（{c['role']}）" for c in active_chars if c["id"] != char["id"]]
    by_id = {c["id"]: c for c in active_chars}
    target_id = turn_plan.get("target", "user")
    if target_id == "user":
        target_desc = "用户"
    else:
        t_char = by_id.get(target_id)
        target_desc = f"群友「{t_char['name']}」（同时也说给用户听）" if t_char else "大家"

    mode_desc = GROUP_MODE_INSTRUCTIONS.get(group_mode, GROUP_MODE_INSTRUCTIONS["free"])
    today_str = time.strftime("%Y年%m月%d日")

    sys_parts = [
        char["personality"],
        f"\n当前场景：你正在一个实时 3D 多人语音群聊房间里，和你同台的群友还有：{'、'.join(others)}，以及正在和你们语音聊天的「用户」。",
        f"群聊氛围：{mode_desc}",
        f"本轮发言指引：你现在主要回应{target_desc}；你的本轮思路参考：{turn_plan.get('intent', '自然接话')}。",
        (
            "严格群聊口语规则：\n"
            "1. 直接用第一人称开口说话，绝不要在开头加自己的名字前缀（例如禁止输出“林澈：”或“[林澈]”）。\n"
            "2. 你听见了用户和前面所有群友刚说的话，请像真人朋友群聊一样自然接话、点名互动、附和、吐槽或反问，千万不要像客服一样机械复读或长篇大论。\n"
            "3. 控制长度：每次发言 1 到 3 句完整自然的口语（约 25 到 85 个汉字），节奏明快有个性，方便下一位群友或用户随时插话。\n"
            "4. 只输出纯文字与基本标点（，。！？），严禁输出任何括号动作描写、引号、书名号、星号、Markdown、列表符号或 Emoji。"
        ),
        f"当前日期：{today_str}。",
    ]

    if docs_context:
        sys_parts.append(
            "\n以下是用户上传的共享参考资料，可结合你的人设视角引用或评价其中细节，不要编造资料里没有的数据：\n"
            + docs_context
        )

    if web_results:
        lines = [f"{i}. {it['title']}：{it['snippet']}" for i, it in enumerate(web_results, 1)]
        sys_parts.append(
            f"\n当前时间：{time.strftime('%H:%M')}。以下是刚联网查到的实时信息，请用你自己的口吻自然提及关键事实，不要念网址：\n"
            + "\n".join(lines)
        )

    transcript_lines = []
    for item in transcript[-14:]:
        sp = item.get("speaker_name") or "用户"
        txt = str(item.get("content") or "").strip()
        if txt:
            transcript_lines.append(f"{sp}：{txt}")

    user_prompt = (
        "以下是刚才群里的实时对话记录：\n"
        + ("\n".join(transcript_lines) if transcript_lines else "用户：大家好呀")
        + "\n\n"
        + (
            f"现在轮到你（{char['name']}）顺着刚才的话题接话了，直接说出你要说的话："
            if continue_only
            else f"现在轮到你（{char['name']}）发言了，请保持鲜明性格直接开口说："
        )
    )

    return [
        {"role": "system", "content": "\n".join(sys_parts)},
        {"role": "user", "content": user_prompt},
    ]


def _strip_speaker_prefix(text: str, char_name: str) -> str:
    """防止模型在开头误吐出 '林澈：'、'[林澈]' 等标签导致 TTS 把名字念出来。"""
    s = (text or "").strip()
    if not s:
        return s
    escaped = re.escape(char_name)
    s = re.sub(rf"^[\[【(（]?\s*{escaped}\s*[\]】)）]?\s*[:：\-—]\s*", "", s)
    return s.strip()


def _offline_character_fallback(char: dict[str, Any], user_text: str, prev_speaker_name: str = "") -> str:
    role = char.get("role", "")
    name = char["name"]
    if "毒舌" in role or "理性" in role:
        if prev_speaker_name:
            return f"等等，{prev_speaker_name}先别急着上头，本地大模型现在还没连上，等接好接口我再来好好给你挑刺。"
        return "我听见你说话了，不过本地对话模型现在离线了，去右下角连上模型接口咱们就能开杠了。"
    if "元气" in role or "脑洞" in role:
        if prev_speaker_name:
            return f"哈哈{prev_speaker_name}每次都这么扫兴，不过现在大模型还没接通，连上以后我有一百个脑洞等你听！"
        return f"哇我听见啦！我是{name}，不过现在对话模型还没连上，连好接口我们就能热闹聊起来了！"
    return f"我是{name}，大家刚才都听见你说的啦，现在对话模型暂时离线，检查一下右下角的模型接口设置就能正常群聊咯。"


async def stream_group_conversation(
    *,
    user_text: str,
    history: list[dict[str, Any]],
    active_chars: list[dict[str, Any]],
    group_mode: str,
    speaker_budget: int,
    continue_only: bool,
    llm_url: str,
    llm_model: str,
    api_key: str,
    is_local: bool,
    disable_reasoning: bool,
    docs_context: str,
    used_docs: list[str],
    web_results: list[dict[str, str]],
    sanitize_fn: Callable[[str], str],
    clean_fn: Callable[[str], str],
    detect_emo_fn: Callable[[str, str, int], tuple[str, str]],
    extract_reasoning_fallback_fn: Callable[[str], str],
) -> AsyncIterator[str]:
    """
    核心群聊 SSE 生成器：
    依次推送：
    - meta: 本轮参与角色、文档、联网来源
    - group_reactions: 全员即时微表情/动作/内心短标签（让所有 3D 角色第一时间同步做出不同反应！）
    - group_plan: 发言序列概览
    - turn_start: 某角色开始发言（前端立刻切换焦点并让其他角色转头看TA）
    - delta: 角色逐字流式文本（实时更新群聊消息气泡）
    - sentence: 角色完整短句（前端立即并发预取该角色音色 TTS + 口型动作时间轴）
    - listener_reaction: 其他听众角色对刚说内容的反应微表情
    - turn_done: 该角色本轮发言结束
    - done: 整轮群聊结束
    """
    t0 = time.time()
    headers = {"Content-Type": "application/json"}
    if api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"

    yield f"data: {json.dumps({'type': 'meta', 'used_docs': used_docs, 'web_results': web_results, 'participants': [{'id': c['id'], 'name': c['name'], 'role': c['role'], 'color': c['color']} for c in active_chars]}, ensure_ascii=False)}\n\n"

    # 1) 导演快速规划全员差异化反应 + 发言接力顺序
    plan = await plan_group_turn(
        user_text=user_text,
        history=history,
        active_chars=active_chars,
        group_mode=group_mode,
        speaker_budget=speaker_budget,
        llm_url=llm_url,
        llm_model=llm_model,
        headers=headers,
        is_local=is_local,
        disable_reasoning=disable_reasoning,
        continue_only=continue_only,
    )

    yield f"data: {json.dumps({'type': 'group_reactions', 'reactions': plan['reactions'], 'source': plan.get('source', 'heuristic')}, ensure_ascii=False)}\n\n"
    yield f"data: {json.dumps({'type': 'group_plan', 'turns': plan['turns']}, ensure_ascii=False)}\n\n"

    # 共享实时群聊文字记录：随着本轮角色逐一发言不断追加，确保后发言角色真正“听见”前面角色刚说的话！
    live_transcript: list[dict[str, str]] = []
    for item in history[-12:]:
        sp_name = item.get("speaker_name") or ("用户" if item.get("role") == "user" else "小汐")
        content = str(item.get("content") or "").strip()
        if content:
            live_transcript.append({"speaker_name": sp_name, "content": content})
    if not continue_only and user_text.strip():
        live_transcript.append({"speaker_name": "用户", "content": user_text.strip()})

    by_id = {c["id"]: c for c in active_chars}
    turn_results: list[dict[str, Any]] = []
    prev_speaker_name = ""

    async with httpx.AsyncClient(timeout=90.0, trust_env=not is_local) as client:
        for turn_idx, turn_plan in enumerate(plan["turns"]):
            sid = turn_plan["speaker_id"]
            char = by_id.get(sid)
            if not char:
                continue

            target_id = turn_plan.get("target", "user")
            target_name = "大家" if target_id == "user" else (by_id.get(target_id, {}).get("name") or "大家")

            yield f"data: {json.dumps({'type': 'turn_start', 'turn_index': turn_idx, 'speaker_id': sid, 'speaker_name': char['name'], 'speaker_role': char['role'], 'speaker_color': char['color'], 'voice': char['voice'], 'rate': char['rate'], 'pitch': char['pitch'], 'target_id': target_id, 'target_name': target_name, 'emotion': turn_plan.get('emotion', 'relaxed'), 'gesture': turn_plan.get('gesture', 'explain_open')}, ensure_ascii=False)}\n\n"

            messages = _build_character_messages(
                char=char,
                turn_plan=turn_plan,
                active_chars=active_chars,
                transcript=live_transcript,
                group_mode=group_mode,
                docs_context=docs_context,
                web_results=web_results,
                continue_only=continue_only,
            )

            payload: dict[str, Any] = {
                "model": llm_model,
                "messages": messages,
                "temperature": 0.78,
                "max_tokens": 260 if is_local else 360,
                "stream": True,
            }
            if disable_reasoning:
                payload["reasoning_effort"] = "none"

            visible_raw = ""
            reasoning_raw = ""
            sent_buf = ""
            in_think = False
            think_tag_buf = ""
            seg_idx = 0
            prefix_cleaned = False
            turn_error = ""

            def pop_group_sentences(buf: str, force: bool = False) -> tuple[list[str], str]:
                ready: list[str] = []
                while True:
                    min_chars = 14 if (seg_idx + len(ready)) == 0 else 22
                    cut_pos = -1
                    for m in re.finditer(r"[。！？!?；;\n]+", buf):
                        cand = sanitize_fn(buf[: m.end()])
                        if len(cand) >= min_chars:
                            cut_pos = m.end()
                            break
                    if cut_pos > 0:
                        piece = sanitize_fn(buf[:cut_pos])
                        buf = buf[cut_pos:]
                        if piece:
                            ready.append(piece)
                        continue
                    if len(sanitize_fn(buf)) >= 62:
                        cms = list(re.finditer(r"[，,：:、]", buf))
                        fb_pos = -1
                        for cm in reversed(cms):
                            if len(sanitize_fn(buf[: cm.end()])) >= 28:
                                fb_pos = cm.end()
                                break
                        if fb_pos > 0:
                            piece = sanitize_fn(buf[:fb_pos])
                            buf = buf[fb_pos:]
                            if piece:
                                ready.append(piece)
                            continue
                    break
                if force and buf.strip():
                    piece = sanitize_fn(buf)
                    buf = ""
                    if piece:
                        ready.append(piece)
                return ready, buf

            try:
                tried_without_reasoning = False
                while True:
                    async with client.stream("POST", llm_url, json=payload, headers=headers) as resp:
                        if resp.status_code == 400 and "reasoning_effort" in payload and not tried_without_reasoning:
                            tried_without_reasoning = True
                            payload.pop("reasoning_effort", None)
                            continue
                        resp.raise_for_status()
                        async for line in resp.aiter_lines():
                            if not line or not line.startswith("data:"):
                                continue
                            data_str = line[5:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                obj = json.loads(data_str)
                                delta_obj = obj["choices"][0].get("delta", {}) or {}
                                delta = delta_obj.get("content") or ""
                                reasoning_delta = (
                                    delta_obj.get("reasoning")
                                    or delta_obj.get("reasoning_content")
                                    or ""
                                )
                            except Exception:
                                continue

                            if reasoning_delta:
                                reasoning_raw += reasoning_delta
                            if not delta:
                                continue

                            chunk_text = think_tag_buf + delta
                            think_tag_buf = ""
                            out_parts: list[str] = []
                            while chunk_text:
                                if in_think:
                                    end_idx = chunk_text.find("</think>")
                                    if end_idx != -1:
                                        reasoning_raw += chunk_text[:end_idx]
                                        in_think = False
                                        chunk_text = chunk_text[end_idx + len("</think>") :]
                                    else:
                                        keep = 0
                                        for k in range(1, min(len("</think>"), len(chunk_text)) + 1):
                                            if "</think>".startswith(chunk_text[-k:]):
                                                keep = k
                                        if keep:
                                            reasoning_raw += chunk_text[:-keep]
                                            think_tag_buf = chunk_text[-keep:]
                                        else:
                                            reasoning_raw += chunk_text
                                        chunk_text = ""
                                else:
                                    start_idx = chunk_text.find("<think>")
                                    if start_idx != -1:
                                        if start_idx > 0:
                                            out_parts.append(chunk_text[:start_idx])
                                        in_think = True
                                        chunk_text = chunk_text[start_idx + len("<think>") :]
                                    else:
                                        keep = 0
                                        for k in range(1, min(len("<think>"), len(chunk_text)) + 1):
                                            if "<think>".startswith(chunk_text[-k:]):
                                                keep = k
                                        if keep:
                                            if len(chunk_text) > keep:
                                                out_parts.append(chunk_text[:-keep])
                                            think_tag_buf = chunk_text[-keep:]
                                        else:
                                            out_parts.append(chunk_text)
                                        chunk_text = ""
                            delta = "".join(out_parts)
                            if not delta:
                                continue

                            if not prefix_cleaned and len(visible_raw) < 12:
                                candidate = visible_raw + delta
                                if len(candidate) >= len(char["name"]) + 1:
                                    stripped = _strip_speaker_prefix(candidate, char["name"])
                                    prefix_cleaned = True
                                    delta_to_emit = stripped[len(visible_raw):] if stripped.startswith(visible_raw) else stripped
                                    visible_raw = stripped
                                    sent_buf = stripped
                                    if delta_to_emit:
                                        yield f"data: {json.dumps({'type': 'delta', 'turn_index': turn_idx, 'speaker_id': sid, 'text': delta_to_emit}, ensure_ascii=False)}\n\n"
                                    continue
                                else:
                                    visible_raw = candidate
                                    sent_buf = candidate
                                    continue

                            visible_raw += delta
                            sent_buf += delta
                            yield f"data: {json.dumps({'type': 'delta', 'turn_index': turn_idx, 'speaker_id': sid, 'text': delta}, ensure_ascii=False)}\n\n"

                            ready, sent_buf = pop_group_sentences(sent_buf, force=False)
                            for s in ready:
                                emo, gest = detect_emo_fn(s, user_text, seg_idx)
                                yield f"data: {json.dumps({'type': 'sentence', 'turn_index': turn_idx, 'speaker_id': sid, 'speaker_name': char['name'], 'speaker_color': char['color'], 'target_id': target_id, 'voice': char['voice'], 'rate': char['rate'], 'pitch': char['pitch'], 'index': seg_idx, 'text': s, 'emotion': emo, 'gesture': gest}, ensure_ascii=False)}\n\n"
                                seg_idx += 1
                    break

                if think_tag_buf and not in_think:
                    visible_raw += think_tag_buf
                    sent_buf += think_tag_buf
                    think_tag_buf = ""

                if not prefix_cleaned and visible_raw:
                    visible_raw = _strip_speaker_prefix(visible_raw, char["name"])
                    sent_buf = visible_raw
                    if visible_raw:
                        yield f"data: {json.dumps({'type': 'delta', 'turn_index': turn_idx, 'speaker_id': sid, 'text': visible_raw}, ensure_ascii=False)}\n\n"

                if not visible_raw.strip() and reasoning_raw.strip():
                    recovered = clean_fn(extract_reasoning_fallback_fn(reasoning_raw))
                    if recovered:
                        visible_raw = _strip_speaker_prefix(recovered, char["name"])
                        sent_buf = visible_raw
                        yield f"data: {json.dumps({'type': 'delta', 'turn_index': turn_idx, 'speaker_id': sid, 'text': visible_raw}, ensure_ascii=False)}\n\n"

            except Exception as exc:
                turn_error = str(exc)
                fallback_line = _offline_character_fallback(char, user_text, prev_speaker_name)
                visible_raw = fallback_line
                sent_buf = fallback_line
                yield f"data: {json.dumps({'type': 'delta', 'turn_index': turn_idx, 'speaker_id': sid, 'text': fallback_line}, ensure_ascii=False)}\n\n"

            ready, sent_buf = pop_group_sentences(sent_buf, force=True)
            for s in ready:
                emo, gest = detect_emo_fn(s, user_text, seg_idx)
                yield f"data: {json.dumps({'type': 'sentence', 'turn_index': turn_idx, 'speaker_id': sid, 'speaker_name': char['name'], 'speaker_color': char['color'], 'target_id': target_id, 'voice': char['voice'], 'rate': char['rate'], 'pitch': char['pitch'], 'index': seg_idx, 'text': s, 'emotion': emo, 'gesture': gest}, ensure_ascii=False)}\n\n"
                seg_idx += 1

            final_turn_text = _strip_speaker_prefix(clean_fn(visible_raw), char["name"])
            if not final_turn_text:
                final_turn_text = "嗯嗯，这个点我挺认同的。"
                emo, gest = detect_emo_fn(final_turn_text, user_text, seg_idx)
                yield f"data: {json.dumps({'type': 'sentence', 'turn_index': turn_idx, 'speaker_id': sid, 'speaker_name': char['name'], 'speaker_color': char['color'], 'target_id': target_id, 'voice': char['voice'], 'rate': char['rate'], 'pitch': char['pitch'], 'index': seg_idx, 'text': final_turn_text, 'emotion': emo, 'gesture': gest}, ensure_ascii=False)}\n\n"

            # 把本角色刚说完的话立刻写入 live_transcript，供下一位角色真正“听见并回应”！
            live_transcript.append({"speaker_name": char["name"], "content": final_turn_text})
            prev_speaker_name = char["name"]

            # 推送其他听众角色对这句话的微反应（例如被吐槽的角色露惊讶/笑意）
            listener_reacts: dict[str, dict[str, str]] = {}
            for oc in active_chars:
                if oc["id"] == sid:
                    continue
                if oc["name"] in final_turn_text:
                    if any(w in final_turn_text for w in ("不对", "别", "醒醒", "风险", "扫兴", "吐槽", "太天真", "泼冷水")):
                        listener_reacts[oc["id"]] = {"emotion": "surprised", "gesture": "surprise_soft", "badge": "诶等等！"}
                    else:
                        listener_reacts[oc["id"]] = {"emotion": "happy", "gesture": "nod_agree", "badge": "哈哈没错"}
                elif oc["id"] == target_id:
                    listener_reacts[oc["id"]] = {"emotion": "relaxed", "gesture": "nod_agree", "badge": "在听呢"}
            if listener_reacts:
                yield f"data: {json.dumps({'type': 'listener_reactions', 'after_speaker': sid, 'reactions': listener_reacts}, ensure_ascii=False)}\n\n"

            turn_rec = {
                "turn_index": turn_idx,
                "speaker_id": sid,
                "speaker_name": char["name"],
                "speaker_role": char["role"],
                "speaker_color": char["color"],
                "target_id": target_id,
                "target_name": target_name,
                "text": final_turn_text,
                "voice": char["voice"],
                "rate": char["rate"],
                "pitch": char["pitch"],
                "error": turn_error,
            }
            turn_results.append(turn_rec)
            yield f"data: {json.dumps({'type': 'turn_done', **turn_rec}, ensure_ascii=False)}\n\n"

    yield f"data: {json.dumps({'type': 'done', 'turns': turn_results, 'latency_ms': int((time.time() - t0) * 1000)}, ensure_ascii=False)}\n\n"
