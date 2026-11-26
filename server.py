"""
小汐 · 资料沟通与音画同步视频对话助手后端（集成小智同款本地语音栈）
启动方式：python server.py  (默认 http://127.0.0.1:8765)

语音与对话架构（参考开源项目 xiaozhi-esp32-server）：
1. VAD（人声活动检测）：Silero VAD (ONNX)，32ms 帧级人声检测 + 说话打断（Barge-in）
2. ASR（语音识别）：阿里 FunASR SenseVoiceSmall (sherpa-onnx int8)，毫秒级中英文+标点识别（兜底 Whisper）
3. LLM（流式对话 + 资料库检索）：SSE 逐字流式输出 + 标点智能分句（边生成边播报，首句 ~1s 开口）
4. TTS（高自然度语音）：Edge TTS 7.3.1 神经音色（自动兼容系统代理）+ Kokoro v1.1-zh 本地离线神经音色 + 自定义接口 + SAPI 兜底
5. 真·音画同步：ComfyUI MiniMax H3 AudioConditioning (lock_source) 按实际 TTS 音频逐句生成对口型视频
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
import wave
try:
    import winreg
except ImportError:
    winreg = None
from pathlib import Path
from typing import Any, AsyncIterator

import edge_tts
import httpx
import numpy as np
import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
CLIPS_DIR = ASSETS_DIR / "clips"
VRM_DIR = ASSETS_DIR / "vrm"
STATIC_DIR = BASE_DIR / "static"
MODELS_DIR = BASE_DIR / "models"
DOCS_STORE_PATH = ASSETS_DIR / "docs_store.json"

ASSETS_DIR.mkdir(parents=True, exist_ok=True)
CLIPS_DIR.mkdir(parents=True, exist_ok=True)
VRM_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
DEFAULT_LLM_URL = os.environ.get("LLM_URL", "http://127.0.0.1:8080/v1/chat/completions")
DEFAULT_LLM_MODEL = os.environ.get("LLM_MODEL", "local-model")
DEFAULT_VOICE = os.environ.get("TTS_VOICE", "zh-CN-XiaoyiNeural")

SILERO_VAD_PATH = MODELS_DIR / "silero_vad.onnx"
SENSEVOICE_DIR = MODELS_DIR / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
KOKORO_DIR = MODELS_DIR / "kokoro-int8-multi-lang-v1_1"

DEFAULT_SYSTEM_PROMPT = (
    "你叫小汐，是一位专业、亲切、表达清晰的实时视频沟通助手，擅长陪用户一起阅读、梳理和讨论各种文档资料。"
    "规则：用自然流畅的中文口语直接回复，逻辑清楚、重点突出；"
    "当用户提供了参考资料时，优先依据资料中的具体内容回答，不要编造资料里没有的数字或事实；"
    "每次回复请把观点讲透、句子说完整连贯，避免零碎短语或半截话，根据问题需要充分展开；"
    "只使用纯文字与基本逗号句号问号感叹号，严禁输出任何引号、括号、书名号、破折号、斜杠、星号、井号、列表符号、Markdown 格式或表情符号。"
)

NORMAL_FALLBACK_LINES = [
    "我收到你的消息啦！不过本机对话模型暂时未连接，请在右下角模型设置里检查 8080 端口或配置外部 API。",
    "当前处于离线保底模式，连接本地大模型或外部 OpenAI 兼容接口后，我就能结合你的资料深入交流了。",
]

LIPSYNC_PRESETS = {
    "realtime": {
        "width": 448,
        "height": 256,
        "steps": 4,
        "lora": "minimax_h3_turbo_4步加速ema_comfyui.safetensors",
        "lora_strength": 1.0,
    },
    "fast": {
        "width": 512,
        "height": 288,
        "steps": 4,
        "lora": "minimax_h3_turbo_4步加速ema_comfyui.safetensors",
        "lora_strength": 1.0,
    },
    "hd": {
        "width": 768,
        "height": 448,
        "steps": 4,
        "lora": "minimax_h3_turbo_4步加速ema_comfyui.safetensors",
        "lora_strength": 1.0,
    },
}

KOKORO_VOICE_MAP = {
    "zh-CN-XiaoyiNeural": 50,
    "zh-CN-XiaoxiaoNeural": 46,
    "zh-CN-XiaohanNeural": 54,
    "zh-CN-YunxiNeural": 92,
}

class SmartGZipMiddleware(GZipMiddleware):
    """对 JS/HTML/JSON/CSS 开启 GZip 压缩加速公网/花生壳加载，跳过 SSE 与已压缩二进制文件（保留精确 Content-Length 供进度条使用）。"""

    _SKIP_EXTS = (".vrm", ".mp4", ".png", ".jpg", ".jpeg", ".webp", ".mp3", ".wav", ".ogg", ".zip")

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = (scope.get("path") or "").lower()
            if path.startswith("/api/chat/stream") or path.endswith(self._SKIP_EXTS):
                await self.app(scope, receive, send)
                return
        await super().__call__(scope, receive, send)


app = FastAPI(title="小汐 · 资料沟通与音画同步视频对话")
app.add_middleware(SmartGZipMiddleware, minimum_size=512)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/vendor/") or path.startswith("/assets/vrm/"):
        response.headers["Cache-Control"] = "public, max-age=604800, immutable"
    elif path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=3600"
    return response

regen_state: dict[str, Any] = {
    "running": False,
    "quality": "debug",
    "message": "idle",
    "started_at": None,
    "finished_at": None,
    "logs": [],
}

_sensevoice_recognizer = None
_sensevoice_lock = threading.Lock()
_kokoro_tts = None
_kokoro_lock = threading.Lock()
_whisper_model = None
_whisper_lock = threading.Lock()
_lipsync_lock = asyncio.Lock()
_lipsync_busy = False
_edge_tts_disabled_until = 0.0
_docs_lock = threading.Lock()
_docs_store: list[dict[str, Any]] = []
_https_runtime: dict[str, Any] = {"enabled": False, "port": int(os.environ.get("HTTPS_PORT", "8766"))}


def _windows_http_proxy() -> str | None:
    if winreg is None:
        return (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy")
            or None
        )
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enabled:
                return None
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return None
    server = str(server).strip()
    if not server:
        return None
    if "=" not in server:
        return server if "://" in server else "http://" + server
    for part in server.split(";"):
        if "=" in part:
            proto, addr = part.split("=", 1)
            if proto.strip().lower() in ("https", "http"):
                addr = addr.strip()
                return addr if "://" in addr else "http://" + addr
    return None


def _has_silero_vad() -> bool:
    return SILERO_VAD_PATH.exists()


def _has_sensevoice() -> bool:
    return SENSEVOICE_DIR.exists() and (SENSEVOICE_DIR / "tokens.txt").exists()


def _has_kokoro() -> bool:
    return KOKORO_DIR.exists() and (KOKORO_DIR / "model.int8.onnx").exists()


def create_silero_vad(threshold: float = 0.48, min_silence: float = 1.20):
    import sherpa_onnx

    cfg = sherpa_onnx.VadModelConfig()
    cfg.silero_vad.model = str(SILERO_VAD_PATH)
    cfg.silero_vad.threshold = threshold
    cfg.silero_vad.min_silence_duration = min_silence
    cfg.silero_vad.min_speech_duration = 0.25
    cfg.silero_vad.max_speech_duration = 35.0
    cfg.silero_vad.window_size = 512
    cfg.sample_rate = 16000
    return sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=45)


def _to_pinyin_set(text: str) -> list[str]:
    from pypinyin import Style, pinyin

    chars = re.findall(r"[\u4e00-\u9fff]", text or "")
    if not chars:
        return []
    return [item[0].lower() for item in pinyin("".join(chars), style=Style.NORMAL, strict=False) if item and item[0]]


def _is_pinyin_self_echo(asr_text: str, recent_avatar_texts: list[str]) -> bool:
    """用拼音音节比对识别并过滤失真的扬声器同音自回声（如“战斗”被回声听成“占斗”）。"""
    clean = re.sub(r"[^\u4e00-\u9fffa-zA-Z0-9]", "", asr_text or "")
    if not clean:
        return True
    # 明确打断词或唤醒词一律放行
    if re.match(r"^(停|停一下|等一下|等等|别说了|打住|先停|暂停|小汐|小希|听我说|问一下|换一个|不对)", clean):
        return False
    if len(clean) <= 2:
        return True
    if not recent_avatar_texts:
        return False

    cand_py = _to_pinyin_set(clean)
    if not cand_py:
        return False
    pool_py = set(_to_pinyin_set(" ".join(recent_avatar_texts[-8:])))
    if not pool_py:
        return False
    hits = sum(1 for s in cand_py if s in pool_py)
    return (hits / len(cand_py)) >= 0.42


def get_sensevoice_recognizer():
    global _sensevoice_recognizer
    if _sensevoice_recognizer is not None:
        return _sensevoice_recognizer
    with _sensevoice_lock:
        if _sensevoice_recognizer is None:
            import sherpa_onnx

            model_file = SENSEVOICE_DIR / "model.int8.onnx"
            if not model_file.exists():
                model_file = next(SENSEVOICE_DIR.glob("*.onnx"))
            _sensevoice_recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(model_file),
                tokens=str(SENSEVOICE_DIR / "tokens.txt"),
                num_threads=4,
                use_itn=True,
                language="auto",
            )
    return _sensevoice_recognizer


def get_kokoro_tts():
    global _kokoro_tts
    if _kokoro_tts is not None:
        return _kokoro_tts
    with _kokoro_lock:
        if _kokoro_tts is None:
            import sherpa_onnx

            lexicons = ",".join(str(p) for p in sorted(KOKORO_DIR.glob("lexicon-*.txt")))
            cfg = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                        model=str(KOKORO_DIR / "model.int8.onnx"),
                        voices=str(KOKORO_DIR / "voices.bin"),
                        tokens=str(KOKORO_DIR / "tokens.txt"),
                        data_dir=str(KOKORO_DIR / "espeak-ng-data"),
                        dict_dir=str(KOKORO_DIR / "dict"),
                        lexicon=lexicons,
                    ),
                    num_threads=min(8, os.cpu_count() or 4),
                    provider="cpu",
                ),
                max_num_sentences=1,
            )
            _kokoro_tts = sherpa_onnx.OfflineTts(cfg)
    return _kokoro_tts


def transcribe_float32_16k(samples: np.ndarray) -> tuple[str, str, int]:
    """优先使用小智同款 SenseVoiceSmall (70~250ms)，兜底 Whisper。返回 (text, engine, ms)。"""
    if len(samples) < 2400:
        return "", "none", 0
    rms = float(np.sqrt(np.mean(samples * samples)))
    if rms < 0.003:
        return "", "silence", 0

    t0 = time.time()
    if _has_sensevoice():
        try:
            rec = get_sensevoice_recognizer()
            stream = rec.create_stream()
            stream.accept_waveform(16000, samples.astype(np.float32))
            rec.decode_stream(stream)
            text = (stream.result.text or "").strip()
            # 清理 SenseVoice 可能携带的特殊标签如 <|zh|><|NEUTRAL|>
            text = re.sub(r"<\|[^|]+\|>", "", text).strip()
            ms = int((time.time() - t0) * 1000)
            if text in ("。", "，", ".", "?", "！", "嗯", "啊") and len(samples) < 8000:
                return "", "sensevoice", ms
            return text, "sensevoice", ms
        except Exception as exc:
            print(f"[SenseVoice] fallback to whisper: {exc}", flush=True)

    global _whisper_model
    import whisper

    with _whisper_lock:
        if _whisper_model is None:
            _whisper_model = whisper.load_model("tiny", device="cpu")
        res = _whisper_model.transcribe(
            samples.astype(np.float32),
            language="zh",
            initial_prompt="以下是中文普通话的日常交流和资料讨论。",
            fp16=False,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
        )
    text = (res.get("text") or "").strip()
    noise_phrases = {"谢谢观看", "感谢观看", "请订阅", "字幕由", "字幕提供", "谢谢大家", "Thank you.", "...", "。"}
    if text in noise_phrases:
        text = ""
    return text, "whisper", int((time.time() - t0) * 1000)


def _load_docs_store() -> None:
    global _docs_store
    if DOCS_STORE_PATH.exists():
        try:
            data = json.loads(DOCS_STORE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                _docs_store = data
        except Exception:
            _docs_store = []


def _save_docs_store() -> None:
    tmp = DOCS_STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(_docs_store, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DOCS_STORE_PATH)


_load_docs_store()


class ChatRequest(BaseModel):
    text: str = ""
    history: list[dict[str, str]] = []
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    llm_url: str = DEFAULT_LLM_URL
    llm_model: str = DEFAULT_LLM_MODEL
    api_key: str = ""
    use_docs: bool = True
    web_search: str = "auto"  # "auto" | "always" | "off"


class TTSRequest(BaseModel):
    text: str
    voice: str = DEFAULT_VOICE
    engine: str = "auto"  # "auto" | "edge" | "kokoro" | "sapi" | "custom"
    custom_tts_url: str = ""
    custom_api_key: str = ""


class LipSyncRequest(BaseModel):
    text: str
    voice: str = DEFAULT_VOICE
    engine: str = "auto"
    quality: str = "realtime"
    emotion: str = "neutral"
    custom_tts_url: str = ""
    custom_api_key: str = ""


class DocPathRequest(BaseModel):
    path: str


class DocTextRequest(BaseModel):
    name: str = "粘贴资料.txt"
    content: str


class DocToggleRequest(BaseModel):
    enabled: bool


class RegenRequest(BaseModel):
    quality: str = "debug"
    states: list[str] = ["idle", "listening", "talking"]
    skip_anchor: bool = True
    anchor_prompt: str = ""


def extract_text_from_bytes(filename: str, raw: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw))
        pages = []
        for idx, page in enumerate(reader.pages, 1):
            content = (page.extract_text() or "").strip()
            if content:
                pages.append(f"[第{idx}页]\n{content}")
        return "\n\n".join(pages).strip()

    if suffix == ".docx":
        from docx import Document

        doc = Document(io.BytesIO(raw))
        parts: list[str] = []
        for p in doc.paragraphs:
            if p.text.strip():
                parts.append(p.text.strip())
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        return "\n".join(parts).strip()

    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "utf-16"):
        try:
            return raw.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").strip()


def _doc_summary(doc: dict[str, Any]) -> dict[str, Any]:
    content = doc.get("content", "")
    return {
        "id": doc["id"],
        "name": doc["name"],
        "chars": len(content),
        "enabled": bool(doc.get("enabled", True)),
        "source": doc.get("source", "upload"),
        "preview": content[:140].replace("\n", " "),
        "added_at": doc.get("added_at", ""),
    }


def _tokenize_for_match(text: str) -> set[str]:
    text = text.lower()
    words = set(re.findall(r"[a-z0-9_]{2,}", text))
    cjk = re.findall(r"[\u4e00-\u9fff]+", text)
    for seg in cjk:
        if len(seg) <= 2:
            words.add(seg)
        else:
            for i in range(len(seg) - 1):
                words.add(seg[i : i + 2])
    return words


def build_docs_context(query: str, max_chars: int = 3600) -> tuple[str, list[str]]:
    with _docs_lock:
        active_docs = [d for d in _docs_store if d.get("enabled", True) and d.get("content")]
    if not active_docs:
        return "", []

    total_chars = sum(len(d["content"]) for d in active_docs)
    if total_chars <= max_chars:
        blocks = [f"【资料：{d['name']}】\n{d['content']}" for d in active_docs]
        return "\n\n".join(blocks), [d["name"] for d in active_docs]

    q_tokens = _tokenize_for_match(query)
    scored_chunks: list[tuple[float, str, str]] = []
    for doc in active_docs:
        name = doc["name"]
        text = doc["content"]
        paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
        chunks: list[str] = []
        buf = ""
        for p in paras:
            if len(buf) + len(p) + 1 <= 420:
                buf = (buf + "\n" + p).strip()
            else:
                if buf:
                    chunks.append(buf)
                if len(p) <= 420:
                    buf = p
                else:
                    for i in range(0, len(p), 380):
                        chunks.append(p[i : i + 400])
                    buf = ""
        if buf:
            chunks.append(buf)

        for idx, chunk in enumerate(chunks):
            c_tokens = _tokenize_for_match(chunk)
            overlap = len(q_tokens & c_tokens) if q_tokens else 0
            head_bonus = 1.5 if idx == 0 else (0.5 if idx == 1 else 0.0)
            scored_chunks.append((overlap + head_bonus, name, chunk))

    scored_chunks.sort(key=lambda item: item[0], reverse=True)
    selected: list[str] = []
    used_names: list[str] = []
    used_chars = 0
    for _score, name, chunk in scored_chunks:
        piece = f"【资料片段：{name}】\n{chunk}"
        if used_chars + len(piece) > max_chars and selected:
            break
        selected.append(piece)
        used_chars += len(piece)
        if name not in used_names:
            used_names.append(name)

    return "\n\n".join(selected), used_names


def clean_spoken_text(raw: str) -> str:
    text = raw.strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"```[\s\S]*?```", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    # 去除括号内动作描写
    text = re.sub(r"[（(【\[][^）)】\]]{1,24}[）)】\]]", "", text)
    # 去除所有 Emoji / 图形符号（避免 TTS 把表情读出来）
    text = re.sub(r"[\U00010000-\U0010FFFF\u2600-\u27BF\uFE00-\uFE0F\u200D]", "", text)
    # 去除常见会被 TTS 念出来的符号：引号、书名号、括号、破折号、斜杠、Markdown/数学/列表符号
    text = re.sub(r"[#*_`~>|\\^@&=+•·▪●○◆◇★☆→←↑↓\"'“”‘’「」『』《》〈〉（）()【】\[\]{}<>]", "", text)
    text = re.sub(r"[—–―…]+", "，", text)
    text = re.sub(r"\.{2,}", "，", text)
    text = re.sub(r"(?<!\d)[/:-](?!\d)", "，", text)
    text = text.replace("：", "，").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[，,、]{2,}", "，", text)
    text = re.sub(r"[。]{2,}", "。", text)
    text = re.sub(r"[！!]{2,}", "！", text)
    text = re.sub(r"[？?]{2,}", "？", text)
    text = re.sub(r"^[，,、。；;：:\s]+", "", text)
    return text.strip()


def sanitize_for_tts(raw: str) -> str:
    """专供 TTS 朗读的强清洗：绝不念出任何符号名称，只保留自然语调所需的停顿标点。"""
    text = clean_spoken_text(raw)
    # 百分比转自然中文读法
    text = re.sub(r"(\d+(?:\.\d+)?)%", r"百分之\1", text)
    # 仅保留中英文、数字、小数点(数字间)和基本停顿标点（，。！？；、）
    text = re.sub(r"(?<!\d)\.(?!\d)", "，", text)
    text = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9\s，。！？；、.,]", "", text)
    text = text.replace(",", "，")
    text = re.sub(r"[，、]{2,}", "，", text)
    text = re.sub(r"[，、]\s*([。！？；])", r"\1", text)
    text = re.sub(r"^[，、。！？；\s]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def split_speech_segments(text: str, min_len: int = 28, max_len: int = 86) -> list[str]:
    text = text.strip()
    if not text:
        return []
    sentences = [s.strip() for s in re.split(r"(?<=[。！？!?；;])", text) if s.strip()]
    segments: list[str] = []
    buf = ""
    for s in sentences:
        if buf and len(buf) >= min_len and len(buf) + len(s) > max_len:
            segments.append(buf)
            buf = s
        else:
            buf = (buf + s).strip()
    if buf:
        if segments and len(buf) < min_len // 2 and len(segments[-1]) + len(buf) <= max_len + 20:
            segments[-1] = (segments[-1] + buf).strip()
        else:
            segments.append(buf)
    return segments or [text]


def detect_emotion_and_gesture(text: str, user_text: str = "", seg_idx: int = 0) -> tuple[str, str]:
    """根据中文语义实时匹配 3D 虚拟人像的情绪与上半身讲解手势。"""
    t = text.strip()
    u = user_text.strip()
    if seg_idx == 0 and any(w in t for w in ("你好", "嗨", "哈喽", "欢迎", "很高兴", "早上好", "晚上好", "下午好", "拜拜", "再见")):
        return "happy", "wave"
    if any(w in t for w in ("哇", "竟然", "居然", "没想到", "天哪", "不可思议", "惊讶", "原来如此", "真的吗")):
        return "surprised", "surprise"
    if any(w in t for w in ("抱歉", "遗憾", "可惜", "不好意思", "失败", "没找到", "风险", "问题比较严重", "担忧", "困难")):
        return "sad", "explain_one"
    if any(w in t for w in ("思考", "分析", "假设", "可能", "不确定", "需要考虑", "仔细看看", "推敲", "怎么看")):
        return "focused", "think"
    if any(w in t for w in ("哈哈", "太好了", "不错", "很棒", "恭喜", "没问题", "当然可以", "开心", "赞", "出色", "成功")):
        return "happy", "nod_agree"
    if any(w in t for w in ("第一", "第二", "首先", "其次", "总结", "要点", "核心", "包含", "分为", "对比", "区别", "建议", "方案")):
        return "focused", "explain_both"
    if any(w in t for w in ("是的", "没错", "对的", "确实", "明白", "好的", "收到", "了解")):
        return "happy", "nod_agree"
    if "?" in t or "？" in t or any(w in u for w in ("为什么", "怎么", "如何", "总结", "分析")):
        return "relaxed", "explain_one"
    return ("happy" if seg_idx % 2 == 0 else "relaxed"), ("explain_both" if seg_idx % 2 == 1 else "explain_one")


def _kokoro_synthesize_wav(text: str, voice: str = DEFAULT_VOICE) -> bytes:
    tts = get_kokoro_tts()
    sid = KOKORO_VOICE_MAP.get(voice, 50)
    if sid >= tts.num_speakers:
        sid = min(50, max(0, tts.num_speakers - 1))
    audio = tts.generate(text, sid=sid, speed=1.06)
    samples = np.clip(np.array(audio.samples, dtype=np.float32), -1.0, 1.0)
    pcm16 = (samples * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(audio.sample_rate))
        wf.writeframes(pcm16.tobytes())
    return buf.getvalue()


def _sapi_synthesize_wav_utf8(text: str) -> bytes:
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
        wav_path = Path(tmp.name)
    try:
        ps_script = (
            "Add-Type -AssemblyName System.Speech;"
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            "try { $s.SelectVoice('Microsoft Huihui Desktop') } catch {};"
            "$s.Rate = 1;"
            f"$s.SetOutputToWaveFile('{str(wav_path)}');"
            f"$txt = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}'));"
            "$s.Speak($txt);"
            "$s.Dispose();"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        if proc.returncode != 0 or not wav_path.exists() or wav_path.stat().st_size < 100:
            raise RuntimeError(proc.stderr.decode("utf-8", errors="ignore") or "SAPI empty wav")
        return wav_path.read_bytes()
    finally:
        wav_path.unlink(missing_ok=True)


async def synthesize_tts_with_marks(
    text: str,
    voice: str = DEFAULT_VOICE,
    engine: str = "auto",
    custom_tts_url: str = "",
    custom_api_key: str = "",
) -> tuple[bytes, str, str, list[dict[str, Any]]]:
    """返回 (audio_bytes, media_type, engine_used, word_marks)。"""
    global _edge_tts_disabled_until
    text = sanitize_for_tts(text)
    if not text:
        raise ValueError("empty text")

    if engine == "custom" and custom_tts_url.strip():
        url = custom_tts_url.strip()
        is_local = "127.0.0.1" in url or "localhost" in url
        headers = {"Content-Type": "application/json"}
        if custom_api_key.strip():
            headers["Authorization"] = f"Bearer {custom_api_key.strip()}"
        async with httpx.AsyncClient(timeout=25.0, trust_env=not is_local) as client:
            r = await client.post(
                url,
                json={"input": text, "voice": voice or "alloy", "model": "tts-1"},
                headers=headers,
            )
            r.raise_for_status()
            return r.content, r.headers.get("content-type", "audio/mpeg"), "custom", []

    if engine in ("auto", "edge") and time.time() >= _edge_tts_disabled_until:
        proxy = _windows_http_proxy()
        for attempt_proxy in ([proxy, None] if proxy else [None]):
            try:
                async def _fetch_edge() -> tuple[bytes, list[dict[str, Any]]]:
                    comm = edge_tts.Communicate(
                        text=text,
                        voice=voice or DEFAULT_VOICE,
                        rate="+6%",
                        pitch="+0Hz",
                        boundary="WordBoundary",
                        proxy=attempt_proxy,
                    )
                    chunks: list[bytes] = []
                    marks: list[dict[str, Any]] = []
                    async for chunk in comm.stream():
                        if chunk["type"] == "audio":
                            chunks.append(chunk["data"])
                        elif chunk["type"] == "WordBoundary":
                            marks.append(chunk)
                    return b"".join(chunks), marks

                audio_bytes, marks = await asyncio.wait_for(_fetch_edge(), timeout=6.5)
                if audio_bytes:
                    return audio_bytes, "audio/mpeg", "edge-neural", marks
            except Exception:
                continue
        _edge_tts_disabled_until = time.time() + 45
        if engine == "edge":
            raise RuntimeError("Edge TTS 暂时不可用，请切换为自动或本地 Kokoro 神经音色")

    if engine in ("auto", "kokoro") and _has_kokoro():
        try:
            wav_bytes = await asyncio.to_thread(_kokoro_synthesize_wav, text, voice)
            return wav_bytes, "audio/wav", "kokoro-local", []
        except Exception as exc:
            if engine == "kokoro":
                raise RuntimeError(f"Kokoro 本地合成失败: {exc}") from exc

    if sys.platform == "win32":
        wav_bytes = await asyncio.to_thread(_sapi_synthesize_wav_utf8, text)
        return wav_bytes, "audio/wav", "windows-sapi", []
    raise RuntimeError("Linux 环境下 Edge TTS 与本地 Kokoro 均不可用，请检查网络或执行 python setup_voice_models.py 安装 Kokoro 本地离线音色")


async def synthesize_tts_audio(
    text: str,
    voice: str = DEFAULT_VOICE,
    engine: str = "auto",
    custom_tts_url: str = "",
    custom_api_key: str = "",
) -> tuple[bytes, str, str]:
    audio_bytes, media_type, eng, _ = await synthesize_tts_with_marks(
        text, voice=voice, engine=engine, custom_tts_url=custom_tts_url, custom_api_key=custom_api_key
    )
    return audio_bytes, media_type, eng


def _ensure_wav_and_duration(audio_bytes: bytes, media_type: str) -> tuple[bytes, float]:
    if "wav" in media_type.lower() or audio_bytes[:4] == b"RIFF":
        with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
            dur = wf.getnframes() / float(wf.getframerate())
        return audio_bytes, max(1.0, dur)

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as src_tmp:
            src_tmp.write(audio_bytes)
            src_path = Path(src_tmp.name)
        dst_path = src_path.with_suffix(".wav")
        try:
            subprocess.run(
                [ffmpeg, "-y", "-i", str(src_path), "-ar", "24000", "-ac", "1", str(dst_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=True,
            )
            wav_bytes = dst_path.read_bytes()
            with wave.open(str(dst_path), "rb") as wf:
                dur = wf.getnframes() / float(wf.getframerate())
            return wav_bytes, max(1.0, dur)
        finally:
            src_path.unlink(missing_ok=True)
            dst_path.unlink(missing_ok=True)

    # Edge TTS MP3 常见码率为 48kbps (~6000 B/s)
    est = max(1.5, len(audio_bytes) / 6000.0)
    return audio_bytes, est


def _prune_old_clips(keep: int = 24) -> None:
    clips = sorted(CLIPS_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in clips[keep:]:
        old.unlink(missing_ok=True)


_comfy_anchor_uploaded: str | None = None
_comfy_loras_cache: list[str] | None = None


def _get_comfy_loras(client: httpx.Client) -> list[str]:
    global _comfy_loras_cache
    if _comfy_loras_cache is not None:
        return _comfy_loras_cache
    try:
        r = client.get(f"{COMFY_URL}/object_info/LoraLoaderModelOnly", timeout=8.0)
        if r.status_code == 200:
            data = r.json()
            loras = data.get("LoraLoaderModelOnly", {}).get("input", {}).get("required", {}).get("lora_name", [[]])[0]
            if isinstance(loras, list):
                _comfy_loras_cache = [str(x) for x in loras]
                return _comfy_loras_cache
    except Exception:
        pass
    return []


def _run_comfy_lipsync_sync(
    wav_bytes: bytes,
    is_wav: bool,
    duration_s: float,
    quality: str,
    emotion: str = "neutral",
) -> tuple[Path, float]:
    global _comfy_anchor_uploaded
    anchor = ASSETS_DIR / "anchor.png"
    if not anchor.exists():
        raise RuntimeError("未找到角色锚点图 assets/anchor.png")

    q_key = quality if quality in LIPSYNC_PRESETS else "fast"
    preset = dict(LIPSYNC_PRESETS[q_key])
    # 实时模式限制单段最长 6.5s 以保障连续低延迟流式产出；其他模式支持最长 8.0s
    max_dur = 6.5 if q_key == "realtime" else 8.0
    clamped_dur = min(max(duration_s + 0.12, 1.5), max_dur)
    frames = max(22, int(math.ceil((clamped_dur * 24.0 - 5) / 17.0)) * 17 + 5)
    frames = min(frames, 192)

    emotion_cue = {
        "happy": "bright warm smile, lively eyes, gentle cheerful head nods",
        "focused": "attentive thoughtful expression, clear explanatory cadence, slight purposeful nods",
        "surprised": "mildly raised eyebrows, bright curious eyes, engaged expression",
        "sad": "soft empathetic expression, calm gentle gaze, subtle reassuring nod",
        "relaxed": "warm relaxed smile, friendly natural eye contact, effortless poise",
    }.get(emotion, "warm natural smile, gentle eye contact, subtle relaxed head nods")

    clip_id = f"sync_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    aud_ext = "wav" if is_wav else "mp3"
    aud_mime = "audio/wav" if is_wav else "audio/mpeg"

    t0 = time.time()
    with httpx.Client(timeout=240.0, trust_env=False) as client:
        if not _comfy_anchor_uploaded:
            r_img = client.post(
                f"{COMFY_URL}/upload/image",
                files={"image": ("avatar_anchor_sync.png", anchor.read_bytes(), "image/png")},
                data={"overwrite": "true"},
            )
            r_img.raise_for_status()
            _comfy_anchor_uploaded = r_img.json()["name"]
        img_name = _comfy_anchor_uploaded

        # 自动匹配云端可用 Turbo LoRA（4步优先；若无则自动切换可用 LoRA 或无 LoRA 8步兜底）
        avail_loras = _get_comfy_loras(client)
        chosen_lora = preset["lora"]
        if avail_loras and chosen_lora not in avail_loras:
            turbo4 = [x for x in avail_loras if "turbo" in x.lower() and "4" in x]
            turbo_any = [x for x in avail_loras if "turbo" in x.lower() or "h3" in x.lower()]
            chosen_lora = (turbo4 or turbo_any or [""])[0]
        use_lora = bool(chosen_lora and (not avail_loras or chosen_lora in avail_loras))
        steps = preset["steps"] if use_lora else max(8, preset["steps"])

        r_aud = client.post(
            f"{COMFY_URL}/upload/image",
            files={"image": (f"{clip_id}.{aud_ext}", wav_bytes, aud_mime)},
            data={"overwrite": "true", "type": "input"},
        )
        r_aud.raise_for_status()
        aud_name = r_aud.json()["name"]

        model_ref = ["2", 0] if use_lora else ["1", 0]
        graph: dict[str, Any] = {
            "1": {
                "class_type": "UNETLoader",
                "inputs": {
                    "unet_name": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
                    "weight_dtype": "default",
                },
            },
            "3": {
                "class_type": "CLIPLoader",
                "inputs": {
                    "clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                    "type": "minimax",
                    "device": "default",
                },
            },
            "4": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
            "5": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
            "6": {"class_type": "LoadImage", "inputs": {"image": img_name}},
            "17": {"class_type": "LoadAudio", "inputs": {"audio": aud_name}},
            "7": {
                "class_type": "MiniMaxH3AudioConditioningT8",
                "inputs": {
                    "clip": ["3", 0],
                    "video_vae": ["4", 0],
                    "audio_vae": ["5", 0],
                    "prompt": (
                        "Static tripod camera, exact same cozy study room and warm lighting. "
                        f"The young woman speaks warmly, clearly, and naturally to the viewer with {emotion_cue}, "
                        "in exact lip synchronization with <Audio 1>, expressive natural mouth articulation, "
                        "and smoothly returning to her initial calm resting pose at the end."
                    ),
                    "width": preset["width"],
                    "height": preset["height"],
                    "length": frames,
                    "task_type": "auto",
                    "audio_mode": "lock_source",
                    "audio_denoise_strength": 0.0,
                    "add_source_as_reference": True,
                    "prompt_primary_audio_ordinal": 1,
                    "strict_prompt_tags": False,
                    "ref_image_size": "match",
                    "reference_video_policy": "model_minimum",
                    "drive_audio": ["17", 0],
                    "final_audio": ["17", 0],
                    "first_frame": ["6", 0],
                    "last_frame": ["6", 0],
                },
            },
            "8": {"class_type": "RandomNoise", "inputs": {"noise_seed": random.randint(1, 2**31 - 1)}},
            "9": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
            "10": {
                "class_type": "BasicScheduler",
                "inputs": {"model": model_ref, "scheduler": "simple", "steps": steps, "denoise": 1.0},
            },
            "11": {"class_type": "BasicGuider", "inputs": {"model": model_ref, "conditioning": ["7", 0]}},
            "12": {
                "class_type": "SamplerCustomAdvanced",
                "inputs": {
                    "noise": ["8", 0],
                    "guider": ["11", 0],
                    "sampler": ["9", 0],
                    "sigmas": ["10", 0],
                    "latent_image": ["7", 1],
                },
            },
            "13": {
                "class_type": "MiniMaxH3AVDecodeT8",
                "inputs": {"av_latent": ["12", 0], "video_vae": ["4", 0], "audio_vae": ["5", 0]},
            },
            "15": {
                "class_type": "CreateVideo",
                "inputs": {"images": ["13", 0], "audio": ["7", 2], "fps": 24.0, "bit_depth": 8},
            },
            "16": {
                "class_type": "SaveVideo",
                "inputs": {
                    "video": ["15", 0],
                    "filename_prefix": f"video/{clip_id}",
                    "format": "mp4",
                    "codec": "h264",
                },
            },
        }
        if use_lora:
            graph["2"] = {
                "class_type": "LoraLoaderModelOnly",
                "inputs": {
                    "model": ["1", 0],
                    "lora_name": chosen_lora,
                    "strength_model": preset["lora_strength"],
                },
            }

        rp = client.post(f"{COMFY_URL}/prompt", json={"prompt": graph})
        if rp.status_code != 200:
            raise RuntimeError(f"ComfyUI 提交失败 ({rp.status_code}): {rp.text[:400]}")
        pid = rp.json()["prompt_id"]

        while True:
            if time.time() - t0 > 300:
                raise TimeoutError("音画同步视频生成超时")
            hr = client.get(f"{COMFY_URL}/history/{pid}")
            hr.raise_for_status()
            hist = hr.json()
            if pid in hist:
                entry = hist[pid]
                st = entry.get("status", {})
                if st.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI 生成报错: {json.dumps(st, ensure_ascii=False)[:500]}")
                if st.get("completed"):
                    outputs = entry.get("outputs", {})
                    file_info = None
                    for node_out in outputs.values():
                        if not isinstance(node_out, dict):
                            continue
                        for items in node_out.values():
                            if isinstance(items, list):
                                for item in items:
                                    if isinstance(item, dict) and "filename" in item:
                                        file_info = item
                                        break
                    if not file_info:
                        raise RuntimeError("未找到 ComfyUI 输出视频")
                    params = urllib.parse.urlencode(
                        {
                            "filename": file_info["filename"],
                            "subfolder": file_info.get("subfolder", ""),
                            "type": file_info.get("type", "output"),
                        }
                    )
                    dest = CLIPS_DIR / f"{clip_id}.mp4"
                    with client.stream("GET", f"{COMFY_URL}/view?{params}") as resp:
                        resp.raise_for_status()
                        with dest.open("wb") as f:
                            for chunk in resp.iter_bytes(65536):
                                f.write(chunk)
                    _prune_old_clips()
                    return dest, round(time.time() - t0, 1)
            time.sleep(0.25)


@app.on_event("startup")
async def _warmup_voice_models() -> None:
    """后台预热 SenseVoiceSmall，确保第一句话就能毫秒级识别。"""
    if _has_sensevoice():
        threading.Thread(target=get_sensevoice_recognizer, daemon=True).start()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    assets = {
        "anchor": (ASSETS_DIR / "anchor.png").exists(),
        "idle": (ASSETS_DIR / "idle.mp4").exists(),
        "listening": (ASSETS_DIR / "listening.mp4").exists(),
        "talking": (ASSETS_DIR / "talking.mp4").exists(),
    }
    manifest = {}
    mf = ASSETS_DIR / "manifest.json"
    if mf.exists():
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}

    comfy_ok = False
    llm_ok = False
    async with httpx.AsyncClient(timeout=1.5, trust_env=False) as client:
        try:
            r = await client.get(f"{COMFY_URL}/system_stats")
            comfy_ok = r.status_code == 200
        except Exception:
            pass
        try:
            base_llm = DEFAULT_LLM_URL.split("/v1/")[0]
            for probe_path in ("/health", "/api/tags", "/v1/models"):
                r = await client.get(base_llm + probe_path)
                if r.status_code == 200:
                    llm_ok = True
                    break
        except Exception:
            pass

    with _docs_lock:
        enabled_docs = [d for d in _docs_store if d.get("enabled", True)]
        docs_count = len(enabled_docs)
        docs_chars = sum(len(d.get("content", "")) for d in enabled_docs)

    return {
        "assets": assets,
        "ready": all(assets[k] for k in ("idle", "talking")),
        "manifest": manifest,
        "comfy_online": comfy_ok,
        "llm_online": llm_ok,
        "default_llm_url": DEFAULT_LLM_URL,
        "default_llm_model": DEFAULT_LLM_MODEL,
        "default_voice": DEFAULT_VOICE,
        "regen": regen_state,
        "docs_count": docs_count,
        "docs_chars": docs_chars,
        "lipsync_busy": _lipsync_busy,
        "https_enabled": bool(_https_runtime.get("enabled")),
        "https_port": int(_https_runtime.get("port", 8766)),
        "voice_stack": {
            "vad": "Silero VAD" if _has_silero_vad() else "Energy VAD",
            "asr": "SenseVoiceSmall" if _has_sensevoice() else "Whisper",
            "tts": "Edge 7.3.1 + Kokoro ZH" if _has_kokoro() else "Edge 7.3.1 / SAPI",
        },
    }


@app.websocket("/ws/voice")
async def ws_voice(websocket: WebSocket) -> None:
    """小智同款实时音频流 WebSocket：接收 16kHz Int16 PCM -> Silero VAD 检测起止 -> SenseVoice 毫秒级识别。"""
    await websocket.accept()
    if not _has_silero_vad():
        await websocket.send_json({"type": "error", "message": "Silero VAD 模型未就绪"})
        await websocket.close()
        return

    vad = create_silero_vad(threshold=0.48, min_silence=1.20)
    pcm_buf = np.zeros(0, dtype=np.float32)
    in_speech = False
    avatar_speaking = False
    last_speak_end = 0.0
    recent_avatar_texts: list[str] = []
    await websocket.send_json({"type": "ready", "vad": "Silero VAD", "asr": "SenseVoiceSmall" if _has_sensevoice() else "Whisper"})

    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if "text" in msg and msg["text"]:
                try:
                    cmd = json.loads(msg["text"])
                    ctype = cmd.get("type")
                    if ctype == "reset":
                        vad.reset()
                        pcm_buf = np.zeros(0, dtype=np.float32)
                        in_speech = False
                    elif ctype == "config":
                        ms = float(cmd.get("min_silence", 1.20))
                        ms = min(2.5, max(0.6, ms))
                        is_mobile = bool(cmd.get("is_mobile", False))
                        vad_th = 0.34 if is_mobile else 0.45
                        vad = create_silero_vad(threshold=vad_th, min_silence=ms)
                        pcm_buf = np.zeros(0, dtype=np.float32)
                        in_speech = False
                        print(f"[VoiceWS] config min_silence={ms}s threshold={vad_th} mobile={is_mobile}", flush=True)
                    elif ctype == "avatar_text":
                        txt = str(cmd.get("text", "")).strip()
                        if txt:
                            recent_avatar_texts.append(txt)
                            recent_avatar_texts = recent_avatar_texts[-10:]
                    elif ctype == "speaking_state":
                        new_state = bool(cmd.get("speaking", False))
                        if avatar_speaking and not new_state:
                            last_speak_end = time.time()
                            if not in_speech:
                                vad.reset()
                                pcm_buf = np.zeros(0, dtype=np.float32)
                        avatar_speaking = new_state
                except Exception:
                    pass
                continue

            raw_bytes = msg.get("bytes")
            if not raw_bytes:
                continue
            chunk = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            pcm_buf = np.concatenate([pcm_buf, chunk])

            while len(pcm_buf) >= 512:
                window = pcm_buf[:512]
                pcm_buf = pcm_buf[512:]
                vad.accept_waveform(window)

                speaking_now = vad.is_speech_detected()
                if speaking_now and not in_speech:
                    in_speech = True
                    await websocket.send_json({"type": "vad_start", "during_speaking": avatar_speaking})
                elif not speaking_now and in_speech:
                    in_speech = False
                    await websocket.send_json({"type": "vad_end", "during_speaking": avatar_speaking})

                while not vad.empty():
                    seg = np.array(vad.front.samples, dtype=np.float32)
                    vad.pop()
                    in_echo_win = avatar_speaking or (time.time() - last_speak_end < 1.8)
                    if in_echo_win:
                        seg_rms = float(np.sqrt(np.mean(seg * seg))) if len(seg) else 0.0
                        if len(seg) < 4800 or seg_rms < 0.014:
                            continue
                    text, engine, ms = await asyncio.to_thread(transcribe_float32_16k, seg)
                    if text:
                        if in_echo_win and _is_pinyin_self_echo(text, recent_avatar_texts):
                            print(f"[VoiceWS] filtered self-echo: {text[:40]}", flush=True)
                            continue
                        print(f"[VoiceWS] ASR ({engine} {ms}ms): {text[:60]}", flush=True)
                        await websocket.send_json({
                            "type": "asr",
                            "text": text,
                            "engine": engine,
                            "ms": ms,
                            "during_speaking": avatar_speaking,
                        })
                    else:
                        await websocket.send_json({"type": "vad_silence"})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass


@app.get("/api/docs")
async def api_docs_list() -> dict[str, Any]:
    with _docs_lock:
        return {"docs": [_doc_summary(d) for d in _docs_store]}


@app.post("/api/docs/upload")
async def api_docs_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")
    name = file.filename or "未命名资料.txt"
    try:
        content = extract_text_from_bytes(name, raw)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"解析文件失败: {exc}") from exc
    if not content:
        raise HTTPException(status_code=400, detail="未从文件中提取到有效文字（如果是纯图片扫描 PDF，请先 OCR）")

    entry = {
        "id": uuid.uuid4().hex[:10],
        "name": name,
        "content": content[:200000],
        "enabled": True,
        "source": "upload",
        "added_at": time.strftime("%H:%M:%S"),
    }
    with _docs_lock:
        _docs_store.append(entry)
        _save_docs_store()
    return {"ok": True, "doc": _doc_summary(entry)}


@app.post("/api/docs/path")
async def api_docs_path(req: DocPathRequest) -> dict[str, Any]:
    raw_path = req.path.strip().strip('"').strip("'")
    if not raw_path:
        raise HTTPException(status_code=400, detail="文件路径不能为空")
    p = Path(raw_path)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail=f"未找到本机文件: {raw_path}")
    if p.stat().st_size > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="单个文件超过 20MB 限制")
    try:
        content = extract_text_from_bytes(p.name, p.read_bytes())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"读取文件失败: {exc}") from exc
    if not content:
        raise HTTPException(status_code=400, detail="未提取到有效文字内容")

    entry = {
        "id": uuid.uuid4().hex[:10],
        "name": p.name,
        "content": content[:200000],
        "enabled": True,
        "source": str(p),
        "added_at": time.strftime("%H:%M:%S"),
    }
    with _docs_lock:
        _docs_store.append(entry)
        _save_docs_store()
    return {"ok": True, "doc": _doc_summary(entry)}


@app.post("/api/docs/text")
async def api_docs_text(req: DocTextRequest) -> dict[str, Any]:
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="资料文本不能为空")
    name = req.name.strip() or f"速记资料_{time.strftime('%H%M%S')}.txt"
    entry = {
        "id": uuid.uuid4().hex[:10],
        "name": name,
        "content": content[:200000],
        "enabled": True,
        "source": "paste",
        "added_at": time.strftime("%H:%M:%S"),
    }
    with _docs_lock:
        _docs_store.append(entry)
        _save_docs_store()
    return {"ok": True, "doc": _doc_summary(entry)}


@app.patch("/api/docs/{doc_id}")
async def api_docs_toggle(doc_id: str, req: DocToggleRequest) -> dict[str, Any]:
    with _docs_lock:
        for d in _docs_store:
            if d["id"] == doc_id:
                d["enabled"] = req.enabled
                _save_docs_store()
                return {"ok": True, "doc": _doc_summary(d)}
    raise HTTPException(status_code=404, detail="资料不存在")


@app.delete("/api/docs/{doc_id}")
async def api_docs_delete(doc_id: str) -> dict[str, Any]:
    with _docs_lock:
        before = len(_docs_store)
        _docs_store[:] = [d for d in _docs_store if d["id"] != doc_id]
        if len(_docs_store) != before:
            _save_docs_store()
    return {"ok": True}


@app.delete("/api/docs")
async def api_docs_clear() -> dict[str, Any]:
    with _docs_lock:
        _docs_store.clear()
        _save_docs_store()
    return {"ok": True}


def should_trigger_web_search(user_text: str, mode: str, has_active_docs: bool) -> bool:
    if mode == "off":
        return False
    if mode == "always":
        return bool(user_text.strip())
    t = user_text.strip()
    if len(t) < 2:
        return False
    # 明确要求联网/查询的口令（哪怕加载了本地资料也优先联网）
    explicit_web = re.search(
        r"联网|网上|搜一下|搜索|查一下|帮我查|查查|查询|最新|新闻|热搜|天气|气温|股价|股票|汇率|金价|价格|多少钱|发布会|上映|上线了吗|比赛|比分|今天|今日|实时|2025|2026",
        t,
    )
    if explicit_web:
        return True
    # 纯打招呼/控制语不联网
    if re.match(r"^(你好|嗨|哈喽|早上好|晚上好|谢谢|好的|明白|哈哈|嗯|再见|拜拜|你是谁|介绍一下你自己)", t):
        return False
    # 明显在讨论本地上传资料时不联网
    if has_active_docs and re.search(r"资料|文档|文件|报告|上面|这段|第一|第二|第三|总结|大纲|里面", t):
        return False
    return False


def _search_cn_bing(query: str, max_results: int = 4) -> list[dict[str, str]]:
    """国内云主机直连 cn.bing.com 实时检索（免代理，亚秒级返回）。"""
    import html as html_mod

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    try:
        r = httpx.get(
            "https://cn.bing.com/search",
            params={"q": query, "setlang": "zh-Hans"},
            headers=headers,
            timeout=7.0,
            follow_redirects=True,
        )
        if r.status_code != 200:
            return []
        raw_items = re.findall(
            r'<li class="b_algo"[\s\S]*?<h2><a[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a></h2>[\s\S]*?<p[^>]*>([\s\S]*?)</p>',
            r.text,
        )
        results: list[dict[str, str]] = []
        for href, raw_t, raw_p in raw_items:
            title = html_mod.unescape(re.sub(r"<[^>]+>", "", raw_t)).strip()
            body = html_mod.unescape(re.sub(r"<[^>]+>", "", raw_p)).strip()
            body = re.sub(r"\s+", " ", body)
            if (title or body) and href.startswith("http"):
                results.append({"title": title[:80], "snippet": body[:240], "url": href})
            if len(results) >= max_results:
                break
        return results
    except Exception as exc:
        print(f"[WebSearch:cn_bing] {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        return []


def web_search_realtime(query: str, max_results: int = 4) -> list[dict[str, str]]:
    """实时联网检索：国内无代理环境优先直连 cn.bing.com，有代理环境优先 ddgs(yahoo) 并以 cn.bing / ddgs(auto) 兜底。"""
    q = re.sub(r"^(请|帮我|麻烦|联网|网上|搜一下|搜索|查一下|查查|查询)+", "", query.strip()).strip()
    if not q:
        q = query.strip()
    proxy = _windows_http_proxy()

    if not proxy:
        cn_rows = _search_cn_bing(q, max_results=max_results)
        if cn_rows:
            return cn_rows

    try:
        from ddgs import DDGS
    except Exception:
        return _search_cn_bing(q, max_results=max_results)

    for backend in ("yahoo", "auto"):
        try:
            with DDGS(proxy=proxy, timeout=8) as ddgs:
                rows = list(ddgs.text(q, region="wt-wt", backend=backend, max_results=max_results))
            results: list[dict[str, str]] = []
            for r in rows:
                title = str(r.get("title") or "").strip()
                body = str(r.get("body") or "").strip()
                href = str(r.get("href") or "").strip()
                if title or body:
                    results.append({
                        "title": title[:80],
                        "snippet": re.sub(r"\s+", " ", body)[:240],
                        "url": href,
                    })
            if results:
                return results
        except Exception as exc:
            print(f"[WebSearch:{backend}] {type(exc).__name__}: {str(exc)[:120]}", flush=True)
    if proxy:
        return _search_cn_bing(q, max_results=max_results)
    return []


def _prepare_messages(req: ChatRequest, web_results: list[dict[str, str]] | None = None) -> tuple[list[dict[str, str]], list[str]]:
    now_str = time.strftime("%Y年%m月%d日 %H:%M")
    system_text = (req.system_prompt or DEFAULT_SYSTEM_PROMPT) + f"\n当前本机时间：{now_str}。"
    used_docs: list[str] = []
    # 当已触发实时联网检索且用户未提及本地资料时，跳过本地资料库注入，避免无关文档稀释上下文和拖慢推理
    mentions_docs = bool(re.search(r"资料|文档|文件|报告|上面|这段|大纲|里面|结合", req.text))
    if req.use_docs and (not web_results or mentions_docs):
        docs_context, used_docs = build_docs_context(req.text)
        if docs_context:
            system_text += (
                "\n\n以下是用户已上传并勾选的参考资料，请结合这些资料准确回答或讨论：\n"
                + docs_context
            )
    if web_results:
        lines = []
        for idx, item in enumerate(web_results, 1):
            lines.append(f"{idx}. {item['title']}：{item['snippet']}")
        system_text += (
            "\n\n以下是刚刚联网检索到的实时信息，请严格基于这些检索摘要用自然简洁的中文口语回答用户；"
            "如果摘要中未给出具体数值，请不要编造，直接概括摘要要点并提示用户可点击下方来源链接查看详情（严禁念出网址或 Markdown 符号）：\n"
            + "\n".join(lines)
        )
    messages: list[dict[str, str]] = [{"role": "system", "content": system_text}]
    for item in req.history[-8:]:
        role = item.get("role", "")
        content = item.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": req.text.strip()})
    return messages, used_docs


@app.post("/api/chat/stream")
async def api_chat_stream(req: ChatRequest) -> StreamingResponse:
    """小智同款流式分句 SSE：按需实时联网检索 + 资料库注入 + 边收 LLM token 边切出完整短句。"""
    user_msg = req.text.strip()
    if not user_msg:
        raise HTTPException(status_code=400, detail="empty text")

    with _docs_lock:
        has_active_docs = any(d.get("enabled", True) and d.get("content") for d in _docs_store)
    need_web = should_trigger_web_search(user_msg, req.web_search, has_active_docs)

    llm_url = (req.llm_url or DEFAULT_LLM_URL).strip()
    llm_model = (req.llm_model or DEFAULT_LLM_MODEL).strip()
    if llm_url == "http://127.0.0.1:8080/v1/chat/completions" and DEFAULT_LLM_URL != llm_url:
        llm_url = DEFAULT_LLM_URL
        if llm_model in ("", "local-model"):
            llm_model = DEFAULT_LLM_MODEL
    is_local = "127.0.0.1" in llm_url or "localhost" in llm_url
    headers = {"Content-Type": "application/json"}
    if req.api_key.strip():
        headers["Authorization"] = f"Bearer {req.api_key.strip()}"

    async def event_gen() -> AsyncIterator[str]:
        t0 = time.time()
        web_results: list[dict[str, str]] = []
        if need_web:
            yield f"data: {json.dumps({'type': 'searching', 'query': user_msg}, ensure_ascii=False)}\n\n"
            web_results = await asyncio.to_thread(web_search_realtime, user_msg, 4)

        messages, used_docs = _prepare_messages(req, web_results=web_results)
        stream_max_tokens = 1536 if is_local else 2048
        yield f"data: {json.dumps({'type': 'meta', 'used_docs': used_docs, 'web_results': web_results}, ensure_ascii=False)}\n\n"
        full_raw = ""
        visible_raw = ""
        sent_buf = ""
        in_think = False
        seg_idx = 0

        def pop_ready_sentences(buf: str, force: bool = False) -> tuple[list[str], str]:
            """
            尽量按完整句子切分播报；若单句字数偏短则继续向后累积延续，
            与后续完整句合并后再推送给 TTS，避免半截断句或零碎停顿。
            """
            ready: list[str] = []
            while True:
                min_chars = 24 if (seg_idx + len(ready)) == 0 else 38
                cut_pos = -1
                # 仅在完整句末标点处寻找达到最小长度的切分点；不够长就继续往后累积
                for m in re.finditer(r"[。！？!?；;\n]+", buf):
                    cand_clean = sanitize_for_tts(buf[: m.end()])
                    if len(cand_clean) >= min_chars:
                        cut_pos = m.end()
                        break
                if cut_pos > 0:
                    piece = sanitize_for_tts(buf[:cut_pos])
                    buf = buf[cut_pos:]
                    if piece:
                        ready.append(piece)
                    continue
                # 兜底：若模型连续输出超长不带句号的文本（>= 96 字），在最后一个逗号处平滑切分，避免长时间卡顿
                if len(sanitize_for_tts(buf)) >= 96:
                    clause_matches = list(re.finditer(r"[，,：:、]", buf))
                    fallback_pos = -1
                    for cm in reversed(clause_matches):
                        if len(sanitize_for_tts(buf[: cm.end()])) >= 42:
                            fallback_pos = cm.end()
                            break
                    if fallback_pos > 0:
                        piece = sanitize_for_tts(buf[:fallback_pos])
                        buf = buf[fallback_pos:]
                        if piece:
                            ready.append(piece)
                        continue
                break
            if force and buf.strip():
                piece = sanitize_for_tts(buf)
                buf = ""
                if piece:
                    ready.append(piece)
            return ready, buf

        try:
            async with httpx.AsyncClient(timeout=120.0, trust_env=not is_local) as client:
                cur_messages = list(messages)
                max_continuations = 2
                for cont_round in range(max_continuations + 1):
                    model_name = llm_model or DEFAULT_LLM_MODEL
                    payload: dict[str, Any] = {
                        "model": model_name,
                        "messages": cur_messages,
                        "temperature": 0.65,
                        "max_tokens": stream_max_tokens if cont_round == 0 else 640,
                        "stream": True,
                    }
                    # Ollama / Gemma-4 / Qwen3 等思考模型关闭冗长隐藏推理，实现亚秒级实时语音流式开口
                    if is_local and (":11434" in llm_url or "gemma-4" in model_name.lower() or "gemma4" in model_name.lower()):
                        payload["reasoning_effort"] = "none"
                    round_finish_reason = None
                    round_visible = ""
                    async with client.stream("POST", llm_url, json=payload, headers=headers) as resp:
                        resp.raise_for_status()
                        async for line in resp.aiter_lines():
                            if not line or not line.startswith("data:"):
                                continue
                            data_str = line[5:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                obj = json.loads(data_str)
                                choice0 = obj["choices"][0]
                                if choice0.get("finish_reason"):
                                    round_finish_reason = choice0.get("finish_reason")
                                delta = choice0.get("delta", {}).get("content") or ""
                            except Exception:
                                continue
                            if not delta:
                                continue
                            full_raw += delta
                            # 跳过 <think>...</think>
                            if "<think>" in delta:
                                in_think = True
                            if in_think:
                                if "</think>" in delta:
                                    in_think = False
                                    delta = delta.split("</think>", 1)[1]
                                else:
                                    continue
                            if not delta:
                                continue
                            visible_raw += delta
                            round_visible += delta
                            sent_buf += delta
                            yield f"data: {json.dumps({'type': 'delta', 'text': delta}, ensure_ascii=False)}\n\n"
                            ready, sent_buf = pop_ready_sentences(sent_buf, force=False)
                            for s in ready:
                                emo, gest = detect_emotion_and_gesture(s, user_msg, seg_idx)
                                yield f"data: {json.dumps({'type': 'sentence', 'index': seg_idx, 'text': s, 'emotion': emo, 'gesture': gest}, ensure_ascii=False)}\n\n"
                                seg_idx += 1

                    # 判断是否被长度截断或停在半句话：若是则自动无缝续写补全完整句子
                    tail_clean = visible_raw.strip()
                    ended_mid_sentence = bool(tail_clean) and len(tail_clean) >= 6 and not re.search(r"[。！？!?…]$", tail_clean)
                    need_continue = (round_finish_reason == "length") or (ended_mid_sentence and bool(round_visible.strip()))
                    if not need_continue or cont_round >= max_continuations:
                        break
                    cur_messages = list(messages) + [
                        {"role": "assistant", "content": tail_clean},
                        {"role": "user", "content": "请紧接着上面未说完的句子自然继续说完，把意思讲完整，不要重复前文。"},
                    ]

            ready, sent_buf = pop_ready_sentences(sent_buf, force=True)
            for s in ready:
                emo, gest = detect_emotion_and_gesture(s, user_msg, seg_idx)
                yield f"data: {json.dumps({'type': 'sentence', 'index': seg_idx, 'text': s, 'emotion': emo, 'gesture': gest}, ensure_ascii=False)}\n\n"
                seg_idx += 1
            final_text = clean_spoken_text(visible_raw or full_raw) or "好的，我在听，请继续说。"
            yield f"data: {json.dumps({'type': 'done', 'reply': final_text, 'latency_ms': int((time.time() - t0) * 1000)}, ensure_ascii=False)}\n\n"
        except Exception as exc:
            fallback = random.choice(NORMAL_FALLBACK_LINES)
            yield f"data: {json.dumps({'type': 'sentence', 'index': 0, 'text': fallback}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'reply': fallback, 'error': str(exc), 'latency_ms': int((time.time() - t0) * 1000)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/chat")
async def api_chat(req: ChatRequest) -> dict[str, Any]:
    user_msg = req.text.strip()
    if not user_msg:
        return {"reply": "我在听，请问想先讨论哪部分内容？", "llm_mode": "empty", "used_docs": [], "segments": []}
    messages, used_docs = _prepare_messages(req)
    llm_url = (req.llm_url or DEFAULT_LLM_URL).strip()
    llm_model = (req.llm_model or DEFAULT_LLM_MODEL).strip()
    if llm_url == "http://127.0.0.1:8080/v1/chat/completions" and DEFAULT_LLM_URL != llm_url:
        llm_url = DEFAULT_LLM_URL
        if llm_model in ("", "local-model"):
            llm_model = DEFAULT_LLM_MODEL
    is_local = "127.0.0.1" in llm_url or "localhost" in llm_url
    headers = {"Content-Type": "application/json"}
    if req.api_key.strip():
        headers["Authorization"] = f"Bearer {req.api_key.strip()}"
    model_name = req.llm_model or DEFAULT_LLM_MODEL
    payload: dict[str, Any] = {
        "model": model_name,
        "messages": messages,
        "temperature": 0.65,
        "max_tokens": 1024 if is_local else 1536,
        "stream": False,
    }
    if is_local and (":11434" in llm_url or "gemma-4" in model_name.lower() or "gemma4" in model_name.lower()):
        payload["reasoning_effort"] = "none"
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=45.0, trust_env=not is_local) as client:
            r = await client.post(llm_url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
            reply = clean_spoken_text(data["choices"][0]["message"]["content"]) or "好的，我在听。"
            return {
                "reply": reply,
                "segments": split_speech_segments(reply),
                "used_docs": used_docs,
                "llm_mode": "local" if is_local else "remote",
                "latency_ms": int((time.time() - t0) * 1000),
            }
    except Exception as exc:
        fallback = random.choice(NORMAL_FALLBACK_LINES)
        return {
            "reply": fallback,
            "segments": split_speech_segments(fallback),
            "used_docs": used_docs,
            "llm_mode": "fallback",
            "error": str(exc),
            "latency_ms": int((time.time() - t0) * 1000),
        }


@app.post("/api/tts")
async def api_tts(req: TTSRequest) -> Response:
    try:
        audio_bytes, media_type, eng = await synthesize_tts_audio(
            req.text,
            voice=req.voice,
            engine=req.engine,
            custom_tts_url=req.custom_tts_url,
            custom_api_key=req.custom_api_key,
        )
        return Response(content=audio_bytes, media_type=media_type, headers={"X-TTS-Engine": eng})
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TTS 合成失败: {exc}") from exc


@app.post("/api/tts_speech")
async def api_tts_speech(req: TTSRequest) -> dict[str, Any]:
    """面向 3D 虚拟人像的一体化语音合成接口：返回音频 + 毫秒级拼音口型时间轴 + 词级微表情/手势关键帧。"""
    from speech_choreography import build_speech_timeline

    try:
        clean_txt = sanitize_for_tts(req.text)
        audio_bytes, media_type, eng, marks = await synthesize_tts_with_marks(
            clean_txt,
            voice=req.voice,
            engine=req.engine,
            custom_tts_url=req.custom_tts_url,
            custom_api_key=req.custom_api_key,
        )
        if "wav" in media_type.lower() or audio_bytes[:4] == b"RIFF":
            with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
                est_dur = wf.getnframes() / float(wf.getframerate())
        else:
            est_dur = max(1.0, len(audio_bytes) / 6000.0)

        timeline = build_speech_timeline(clean_txt, marks, est_dur)
        return {
            "ok": True,
            "engine": eng,
            "media_type": media_type,
            "audio_b64": base64.b64encode(audio_bytes).decode("ascii"),
            "timeline": timeline,
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TTS 与动作编排合成失败: {exc}") from exc


@app.post("/api/lipsync")
async def api_lipsync(req: LipSyncRequest) -> dict[str, Any]:
    global _lipsync_busy
    text = clean_spoken_text(req.text)
    if not text:
        raise HTTPException(status_code=400, detail="文本不能为空")

    try:
        audio_bytes, media_type, eng = await synthesize_tts_audio(
            text,
            voice=req.voice,
            engine=req.engine,
            custom_tts_url=req.custom_tts_url,
            custom_api_key=req.custom_api_key,
        )
        wav_bytes, duration_s = await asyncio.to_thread(_ensure_wav_and_duration, audio_bytes, media_type)
        is_wav = wav_bytes[:4] == b"RIFF"
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"语音合成失败: {exc}") from exc

    async with _lipsync_lock:
        _lipsync_busy = True
        try:
            clip_path, elapsed_s = await asyncio.to_thread(
                _run_comfy_lipsync_sync,
                wav_bytes,
                is_wav,
                duration_s,
                req.quality,
                req.emotion,
            )
            return {
                "ok": True,
                "video_url": f"/assets/clips/{clip_path.name}",
                "duration": round(duration_s, 2),
                "elapsed_s": elapsed_s,
                "tts_engine": eng,
            }
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"音画同步生成失败: {exc}") from exc
        finally:
            _lipsync_busy = False


@app.post("/api/asr")
async def api_asr(audio: UploadFile = File(...)) -> dict[str, Any]:
    raw = await audio.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty audio")
    if raw[:4] != b"RIFF":
        raise HTTPException(status_code=400, detail="请发送 16kHz PCM WAV 音频")

    with wave.open(io.BytesIO(raw), "rb") as wf:
        channels = wf.getnchannels()
        sr = wf.getframerate()
        sampwidth = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())

    if sampwidth == 2:
        arr = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    elif sampwidth == 4:
        arr = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        arr = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    if channels > 1:
        arr = arr.reshape(-1, channels).mean(axis=1)
    if sr != 16000 and len(arr) > 0:
        target_len = max(1, int(round(len(arr) * 16000.0 / sr)))
        arr = np.interp(
            np.linspace(0.0, 1.0, num=target_len, endpoint=False),
            np.linspace(0.0, 1.0, num=len(arr), endpoint=False),
            arr,
        ).astype(np.float32)

    text, engine, ms = await asyncio.to_thread(transcribe_float32_16k, arr)
    return {"text": text, "engine": engine, "ms": ms}


@app.get("/api/vrm/list")
async def api_vrm_list() -> dict[str, Any]:
    files = sorted(VRM_DIR.glob("*.vrm"), key=lambda p: (0 if p.name == "AvatarSample_B.vrm" else 1, p.name))
    return {
        "models": [
            {
                "name": p.name,
                "url": f"/assets/vrm/{urllib.parse.quote(p.name)}",
                "size_mb": round(p.stat().st_size / 1048576, 2),
            }
            for p in files
        ]
    }


@app.post("/api/vrm/upload")
async def api_vrm_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空")
    name = Path(file.filename or "custom.vrm").name
    if not name.lower().endswith(".vrm"):
        raise HTTPException(status_code=400, detail="请上传 .vrm 格式的 3D 虚拟人像文件")
    safe_name = re.sub(r"[^\w\u4e00-\u9fff.\-]", "_", name)
    dest = VRM_DIR / safe_name
    dest.write_bytes(data)
    return {
        "ok": True,
        "name": safe_name,
        "url": f"/assets/vrm/{urllib.parse.quote(safe_name)}",
        "size_mb": round(len(data) / 1048576, 2),
    }


@app.post("/api/upload-anchor")
async def api_upload_anchor(file: UploadFile = File(...)) -> dict[str, Any]:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    dest = ASSETS_DIR / "anchor.png"
    dest.write_bytes(data)
    return {"ok": True, "path": str(dest), "size": len(data)}


@app.post("/api/regenerate")
async def api_regenerate(req: RegenRequest) -> dict[str, Any]:
    if regen_state["running"]:
        raise HTTPException(status_code=409, detail="已有生成任务正在运行中")

    quality = "hd" if req.quality == "hd" else "debug"
    valid_states = [s for s in req.states if s in ("idle", "listening", "talking")]
    if not valid_states:
        valid_states = ["idle", "listening", "talking"]

    cmd = [
        sys.executable,
        str(BASE_DIR / "generate_assets.py"),
        "--quality",
        quality,
        "--only",
        ",".join(valid_states),
        "--free-vram",
    ]
    if req.skip_anchor:
        cmd.append("--skip-anchor")
    if req.anchor_prompt.strip():
        cmd.extend(["--anchor-prompt", req.anchor_prompt.strip()])

    def _worker() -> None:
        regen_state["running"] = True
        regen_state["quality"] = quality
        regen_state["started_at"] = time.strftime("%H:%M:%S")
        regen_state["finished_at"] = None
        regen_state["logs"] = []
        regen_state["message"] = f"正在使用本地 ComfyUI MiniMax H3 ({quality}) 生成基础状态视频..."
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(BASE_DIR),
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if line:
                    regen_state["logs"].append(line)
                    regen_state["logs"] = regen_state["logs"][-40:]
                    regen_state["message"] = line
            code = proc.wait()
            regen_state["message"] = "生成完成！请刷新视频。" if code == 0 else f"生成失败 (exit={code})"
        except Exception as exc:
            regen_state["message"] = f"启动生成失败: {exc}"
        finally:
            regen_state["running"] = False
            regen_state["finished_at"] = time.strftime("%H:%M:%S")

    threading.Thread(target=_worker, daemon=True).start()
    return {"ok": True, "started": True, "cmd": cmd}


app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _get_lan_ipv4_list() -> list[str]:
    import socket

    ips: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("223.5.5.5", 80))
            primary = s.getsockname()[0]
            if primary and not primary.startswith(("127.", "169.254.")):
                ips.append(primary)
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith(("127.", "169.254.", "198.18.")) and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def _ensure_self_signed_cert(lan_ips: list[str]) -> tuple[Path | None, Path | None]:
    """生成并复用包含本机与全部内网 IP SAN 的自签名 TLS 证书，用于内网 HTTPS 解锁浏览器麦克风权限。"""
    cert_dir = ASSETS_DIR / "certs"
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / "server.crt"
    key_path = cert_dir / "server.key"
    meta_path = cert_dir / "san_ips.json"

    target_ips = sorted({ip for ip in (["127.0.0.1"] + lan_ips) if ip})
    if cert_path.exists() and key_path.exists() and meta_path.exists():
        try:
            saved = json.loads(meta_path.read_text(encoding="utf-8"))
            if saved.get("ips") == target_ips:
                return cert_path, key_path
        except Exception:
            pass

    try:
        import datetime
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name(
            [
                x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Xiaoxi Live Avatar"),
                x509.NameAttribute(NameOID.COMMON_NAME, "xiaoxi.local"),
            ]
        )
        san_entries: list[x509.GeneralName] = [
            x509.DNSName("localhost"),
        ]
        for extra_dom in os.environ.get("EXTRA_SAN_DOMAINS", "").split(","):
            extra_dom = extra_dom.strip()
            if extra_dom:
                san_entries.append(x509.DNSName(extra_dom))
        for ip in target_ips:
            try:
                san_entries.append(x509.IPAddress(ipaddress.ip_address(ip)))
            except ValueError:
                pass

        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )
        key_path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        meta_path.write_text(json.dumps({"ips": target_ips}, ensure_ascii=False, indent=2), encoding="utf-8")
        return cert_path, key_path
    except Exception as exc:
        print(f"[HTTPS] 自签名证书生成失败，仅启动 HTTP: {exc}", flush=True)
        return None, None


async def _serve_dual(
    host: str,
    http_port: int,
    https_port: int,
    cert_path: Path | None,
    key_path: Path | None,
) -> None:
    cfg_http = uvicorn.Config(app, host=host, port=http_port, log_level="info")
    srv_http = uvicorn.Server(cfg_http)
    tasks = [srv_http.serve()]
    if cert_path and key_path and cert_path.exists() and key_path.exists():
        cfg_https = uvicorn.Config(
            app,
            host=host,
            port=https_port,
            ssl_certfile=str(cert_path),
            ssl_keyfile=str(key_path),
            log_level="info",
        )
        srv_https = uvicorn.Server(cfg_https)
        tasks.append(srv_https.serve())
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8765"))
    https_port = int(os.environ.get("HTTPS_PORT", "8766"))
    lan_ips = _get_lan_ipv4_list()
    cert_path, key_path = _ensure_self_signed_cert(lan_ips)
    https_ok = bool(cert_path and key_path and cert_path.exists() and key_path.exists())
    _https_runtime["enabled"] = https_ok
    _https_runtime["port"] = https_port

    print(f"小汐 · 资料沟通与音画同步视频对话（小智语音栈）已启动 -> http://127.0.0.1:{port}", flush=True)
    if host in ("0.0.0.0", "::"):
        for ip in lan_ips:
            print(f"内网 HTTP 访问 -> http://{ip}:{port}", flush=True)
            if https_ok:
                print(f"内网 HTTPS 访问（支持麦克风）-> https://{ip}:{https_port}", flush=True)
    asyncio.run(_serve_dual(host, port, https_port, cert_path, key_path))
