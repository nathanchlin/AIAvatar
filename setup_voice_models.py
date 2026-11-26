"""Download and verify Xiaozhi-style local voice stack: Silero VAD + SenseVoiceSmall + Kokoro v1.1 ZH TTS."""
from __future__ import annotations

import asyncio
import tarfile
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
MODELS_DIR = BASE_DIR / "models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)
PROXY = "http://127.0.0.1:7890"

ARTIFACTS = [
    (
        "silero_vad.onnx",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
        False,
    ),
    (
        "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09.tar.bz2",
        True,
    ),
    (
        "kokoro-int8-multi-lang-v1_1",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/kokoro-int8-multi-lang-v1_1.tar.bz2",
        True,
    ),
]


def download_file(url: str, dest: Path) -> None:
    import httpx

    print(f"[Download] {url} -> {dest.name}", flush=True)
    t0 = time.time()
    with httpx.Client(proxy=PROXY, follow_redirects=True, timeout=180.0) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", "0") or 0)
            done = 0
            last_pct = -10
            tmp = dest.with_suffix(dest.suffix + ".part")
            with tmp.open("wb") as f:
                for chunk in resp.iter_bytes(262144):
                    f.write(chunk)
                    done += len(chunk)
                    if total > 0:
                        pct = int(done * 100 / total)
                        if pct - last_pct >= 20:
                            print(f"  {dest.name}: {pct}% ({done / 1048576:.1f}/{total / 1048576:.1f} MB)", flush=True)
                            last_pct = pct
            tmp.replace(dest)
    print(f"[Done] {dest.name} in {time.time() - t0:.1f}s", flush=True)


def ensure_models() -> None:
    for target_name, url, is_tar in ARTIFACTS:
        target_path = MODELS_DIR / target_name
        if target_path.exists():
            print(f"[Skip] Already present: {target_name}", flush=True)
            continue
        if not is_tar:
            download_file(url, target_path)
        else:
            archive = MODELS_DIR / Path(url).name
            if not archive.exists():
                download_file(url, archive)
            print(f"[Extract] {archive.name}...", flush=True)
            with tarfile.open(archive, "r:bz2") as tf:
                tf.extractall(MODELS_DIR)
            archive.unlink(missing_ok=True)
            print(f"[Extracted] {target_name}", flush=True)


def verify_all() -> None:
    import numpy as np
    import sherpa_onnx

    print("\n--- VERIFYING SILERO VAD ---", flush=True)
    vad_cfg = sherpa_onnx.VadModelConfig()
    vad_cfg.silero_vad.model = str(MODELS_DIR / "silero_vad.onnx")
    vad_cfg.silero_vad.threshold = 0.5
    vad_cfg.silero_vad.min_silence_duration = 0.55
    vad_cfg.silero_vad.min_speech_duration = 0.25
    vad_cfg.silero_vad.window_size = 512
    vad_cfg.sample_rate = 16000
    vad = sherpa_onnx.VoiceActivityDetector(vad_cfg, buffer_size_in_seconds=30)
    vad.accept_waveform(np.zeros(512, dtype=np.float32))
    print("SILERO_VAD_OK:", vad.is_speech_detected(), flush=True)

    print("\n--- VERIFYING SENSEVOICE ASR ---", flush=True)
    sv_dir = MODELS_DIR / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
    sv_model = sv_dir / "model.int8.onnx"
    if not sv_model.exists():
        sv_model = next(sv_dir.glob("*.onnx"))
    rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(sv_model),
        tokens=str(sv_dir / "tokens.txt"),
        num_threads=4,
        use_itn=True,
        language="auto",
    )
    print("SENSEVOICE_ASR_OK:", sv_model.name, flush=True)

    print("\n--- VERIFYING KOKORO V1.1 ZH TTS ---", flush=True)
    kk_dir = MODELS_DIR / "kokoro-int8-multi-lang-v1_1"
    kk_files = sorted([p.name for p in kk_dir.iterdir()])
    print("KOKORO_FILES:", kk_files, flush=True)
    lexicons = ",".join(str(p) for p in sorted(kk_dir.glob("lexicon-*.txt")))
    kk_cfg = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=str(kk_dir / "model.int8.onnx"),
                voices=str(kk_dir / "voices.bin"),
                tokens=str(kk_dir / "tokens.txt"),
                data_dir=str(kk_dir / "espeak-ng-data"),
                dict_dir=str(kk_dir / "dict"),
                lexicon=lexicons,
            ),
            num_threads=4,
            provider="cpu",
        ),
        max_num_sentences=2,
    )
    tts = sherpa_onnx.OfflineTts(kk_cfg)
    t0 = time.time()
    audio = tts.generate("你好，我是小汐，现在已经升级为小智同款本地神经语音引擎，听起来是不是自然多了？", sid=50, speed=1.05)
    elapsed = time.time() - t0
    dur = len(audio.samples) / float(audio.sample_rate)
    print(f"KOKORO_TTS_OK: num_speakers={tts.num_speakers}, sr={audio.sample_rate}, dur={dur:.2f}s, gen={elapsed:.2f}s (RTF={elapsed / max(dur, 0.01):.2f})", flush=True)

    # Roundtrip test: feed Kokoro Chinese audio directly into SenseVoice ASR!
    wav_np = np.array(audio.samples, dtype=np.float32)
    target_len = int(round(len(wav_np) * 16000.0 / audio.sample_rate))
    wav_16k = np.interp(
        np.linspace(0, 1, target_len, endpoint=False),
        np.linspace(0, 1, len(wav_np), endpoint=False),
        wav_np,
    ).astype(np.float32)
    t1 = time.time()
    stream = rec.create_stream()
    stream.accept_waveform(16000, wav_16k)
    rec.decode_stream(stream)
    print(f"ROUNDTRIP_ASR ({(time.time() - t1) * 1000:.0f}ms):", stream.result.text, flush=True)

    print("\n--- CHECKING EDGE TTS 7.3.1 ---", flush=True)
    import edge_tts
    async def test_edge():
        try:
            c = edge_tts.Communicate("你好测试", voice="zh-CN-XiaoyiNeural", proxy=PROXY)
            data = b"".join([ch["data"] async for ch in c.stream() if ch["type"] == "audio"])
            print("EDGE_TTS_731_OK bytes=", len(data), flush=True)
        except Exception as e:
            print("EDGE_TTS_731_ERR:", type(e).__name__, str(e)[:120], flush=True)
    asyncio.run(test_edge())


if __name__ == "__main__":
    ensure_models()
    verify_all()
