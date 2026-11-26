"""
本地 ComfyUI MiniMax H3 角色资产生成脚本（方案 A）
功能：
1. 调用本地 ComfyUI (127.0.0.1:8188) 的 MiniMax-H3 图像模型生成角色基准锚点图 (anchor.png)
   或使用用户指定的自定义图片 (--anchor)
2. 调用本地 ComfyUI 的 MiniMax-H3 FL2VA 视频模型 + 4步/8步 Turbo LoRA
   以首尾帧同图 (first_frame = last_frame = anchor) 生成 4 段无缝循环/衔接视频：
   - idle.mp4      (待机呼吸)
   - listening.mp4 (专注倾听)
   - talking.mp4   (自然说话)
   - startled.mp4  (突然受惊 + 原生惊呼音)
3. 预生成一份极低延迟的受惊惊呼音效 (startle_sfx.mp3)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import random
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")

# 质量档位配置：debug 档追求极致生成速度，hd 档用于正式效果
QUALITY_PRESETS = {
    "debug": {
        "img_width": 896,
        "img_height": 512,
        "img_steps": 6,
        "vid_width": 512,
        "vid_height": 288,
        "length": 56,  # 17*3 + 5 = 56 帧 @ 24fps ≈ 2.33 秒
        "steps": 4,
        "lora": "minimax_h3_turbo_4步加速ema_comfyui.safetensors",
        "lora_strength": 1.0,
    },
    "hd": {
        "img_width": 1344,
        "img_height": 768,
        "img_steps": 8,
        "vid_width": 960,
        "vid_height": 544,
        "length": 73,  # 17*4 + 5 = 73 帧 @ 24fps ≈ 3.04 秒
        "steps": 8,
        "lora": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
        "lora_strength": 1.0,
    },
}

DEFAULT_ANCHOR_PROMPT = (
    "A photorealistic medium close-up portrait of a charming 22-year-old East Asian woman "
    "sitting at a cozy wooden desk in a warm modern study room, soft warm desk lamp and "
    "blurred bookshelves in the background, wearing a soft cream knit sweater, natural "
    "shoulder-length dark brown hair with soft bangs, looking directly into the camera "
    "with a gentle relaxed smile, natural skin texture, cinematic shallow depth of field, "
    "16:9 widescreen composition, centered framing, high realism, no text, no watermark."
)

STATE_PROMPTS = {
    "idle": (
        "Static tripod camera, exact same room and lighting. The young woman sits calmly "
        "facing the camera with a gentle relaxed smile, breathing naturally, blinking softly "
        "once or twice, very subtle natural micro-movements of her shoulders and hair, "
        "returning smoothly to her exact initial resting pose at the end. "
        "Audio: quiet cozy room tone, very faint soft breathing, no speech, no music."
    ),
    "listening": (
        "Static tripod camera, exact same room and lighting. The young woman listens attentively "
        "to the viewer, leaning slightly forward with bright curious eyes, giving a gentle "
        "encouraging nod and slight warm smile, then smoothly returning to her initial pose. "
        "Audio: quiet cozy room tone, no speech, no music."
    ),
    "talking": (
        "Static tripod camera, exact same room and lighting. The young woman speaks warmly and "
        "expressively to the camera in a friendly conversation, natural realistic mouth and lip "
        "movements, expressive eyebrows, slight lively head tilts and gentle blinking, "
        "smoothly settling back to her initial pose at the end. "
        "Audio: very soft quiet room ambience, no background music."
    ),
    "startled": (
        "Static tripod camera, exact same room and lighting. Timeline: "
        "[0s-0.3s] She sits calmly looking at the camera. "
        "[0.3s-1.2s] Suddenly startled by an unexpected loud shout off-camera, she jolts sharply "
        "backward, shoulders jump up, eyes snap wide open in shock, eyebrows shoot up, mouth "
        "opens in a quick surprised gasp, and both hands instinctively rise near her chest. "
        "[1.2s-2.3s] She exhales in flustered relief, lowers her hands, blinks rapidly with a "
        "shy startled expression, and smoothly returns to her exact starting pose. "
        "Audio: at 0.3s a crisp short surprised young female Mandarin gasp '呀！' followed by "
        "a quick breathy exhale, cozy quiet room tone, no music."
    ),
}


def make_client(timeout: float = 120.0) -> httpx.Client:
    return httpx.Client(timeout=timeout, trust_env=False)


def check_comfy(client: httpx.Client) -> dict[str, Any]:
    r = client.get(f"{COMFY_URL}/system_stats")
    r.raise_for_status()
    return r.json()


def upload_image(client: httpx.Client, image_path: Path) -> str:
    mime = mimetypes.guess_type(str(image_path))[0] or "image/png"
    with image_path.open("rb") as f:
        files = {"image": (image_path.name, f, mime)}
        data = {"overwrite": "true"}
        r = client.post(f"{COMFY_URL}/upload/image", files=files, data=data)
    r.raise_for_status()
    resp = r.json()
    return resp["name"]


def queue_and_wait(client: httpx.Client, prompt_graph: dict[str, Any], label: str, timeout_s: int = 900) -> dict[str, Any]:
    payload = {"prompt": prompt_graph}
    r = client.post(f"{COMFY_URL}/prompt", json=payload)
    if r.status_code != 200:
        raise RuntimeError(f"[{label}] ComfyUI /prompt 报错 ({r.status_code}): {r.text}")
    prompt_id = r.json()["prompt_id"]
    print(f"[{label}] 已提交 ComfyUI 任务: {prompt_id}", flush=True)

    t0 = time.time()
    last_log = 0.0
    while True:
        elapsed = time.time() - t0
        if elapsed > timeout_s:
            raise TimeoutError(f"[{label}] 生成超时 ({timeout_s}s)")
        hr = client.get(f"{COMFY_URL}/history/{prompt_id}")
        hr.raise_for_status()
        hist = hr.json()
        if prompt_id in hist:
            entry = hist[prompt_id]
            status = entry.get("status", {})
            status_str = status.get("status_str", "")
            if status_str == "error":
                raise RuntimeError(f"[{label}] ComfyUI 执行失败: {json.dumps(status, ensure_ascii=False)}")
            if status.get("completed"):
                print(f"[{label}] 完成，耗时 {elapsed:.1f}s", flush=True)
                return entry
        if elapsed - last_log >= 5.0:
            print(f"[{label}] 生成中... ({elapsed:.0f}s)", flush=True)
            last_log = elapsed
        time.sleep(1.5)


def extract_first_output_file(entry: dict[str, Any]) -> dict[str, Any]:
    outputs = entry.get("outputs", {})
    for _node_id, node_out in outputs.items():
        if not isinstance(node_out, dict):
            continue
        for _key, items in node_out.items():
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and "filename" in item:
                        return item
    raise RuntimeError(f"未在 ComfyUI 输出中找到文件: {json.dumps(outputs, ensure_ascii=False)}")


def download_comfy_file(client: httpx.Client, file_info: dict[str, Any], dest: Path) -> Path:
    params = urllib.parse.urlencode(
        {
            "filename": file_info["filename"],
            "subfolder": file_info.get("subfolder", ""),
            "type": file_info.get("type", "output"),
        }
    )
    url = f"{COMFY_URL}/view?{params}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with client.stream("GET", url) as resp:
        resp.raise_for_status()
        with dest.open("wb") as f:
            for chunk in resp.iter_bytes(65536):
                f.write(chunk)
    return dest


def build_anchor_image_graph(prompt_text: str, width: int, height: int, steps: int, seed: int) -> dict[str, Any]:
    return {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {
                "unet_name": "minimax_h3_image_pruned_w4a8.safetensors",
                "weight_dtype": "default",
            },
        },
        "2": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                "type": "minimax",
                "device": "default",
            },
        },
        "3": {
            "class_type": "VAELoader",
            "inputs": {
                "vae_name": "minimax_h3_image_vae_fp16.safetensors",
            },
        },
        "4": {
            "class_type": "MiniMaxH3ImageConditioning",
            "inputs": {
                "clip": ["2", 0],
                "vae": ["3", 0],
                "prompt": prompt_text,
                "width": width,
                "height": height,
            },
        },
        "5": {
            "class_type": "ConditioningZeroOut",
            "inputs": {
                "conditioning": ["4", 0],
            },
        },
        "6": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["4", 0],
                "negative": ["5", 0],
                "latent_image": ["4", 1],
                "seed": seed,
                "steps": steps,
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
            },
        },
        "7": {
            "class_type": "VAEDecode",
            "inputs": {
                "samples": ["6", 0],
                "vae": ["3", 0],
            },
        },
        "8": {
            "class_type": "SaveImage",
            "inputs": {
                "images": ["7", 0],
                "filename_prefix": "minimax_avatar_anchor",
            },
        },
    }


def build_video_state_graph(
    uploaded_image_name: str,
    prompt_text: str,
    state_name: str,
    preset: dict[str, Any],
    seed: int,
) -> dict[str, Any]:
    return {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {
                "unet_name": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
                "weight_dtype": "default",
            },
        },
        "2": {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": ["1", 0],
                "lora_name": preset["lora"],
                "strength_model": preset["lora_strength"],
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
        "4": {
            "class_type": "VAELoader",
            "inputs": {
                "vae_name": "minimax_h3_video_vae_fp16.safetensors",
            },
        },
        "5": {
            "class_type": "VAELoader",
            "inputs": {
                "vae_name": "minimax_h3_audio_vae_fp32.safetensors",
            },
        },
        "6": {
            "class_type": "LoadImage",
            "inputs": {
                "image": uploaded_image_name,
            },
        },
        "7": {
            "class_type": "MiniMaxH3ImageToVideo",
            "inputs": {
                "clip": ["3", 0],
                "vae": ["4", 0],
                "first_frame": ["6", 0],
                "last_frame": ["6", 0],
                "prompt": prompt_text,
                "width": preset["vid_width"],
                "height": preset["vid_height"],
                "length": preset["length"],
            },
        },
        "8": {
            "class_type": "RandomNoise",
            "inputs": {
                "noise_seed": seed,
            },
        },
        "9": {
            "class_type": "KSamplerSelect",
            "inputs": {
                "sampler_name": "res_multistep",
            },
        },
        "10": {
            "class_type": "BasicScheduler",
            "inputs": {
                "model": ["2", 0],
                "scheduler": "simple",
                "steps": preset["steps"],
                "denoise": 1.0,
            },
        },
        "11": {
            "class_type": "BasicGuider",
            "inputs": {
                "model": ["2", 0],
                "conditioning": ["7", 0],
            },
        },
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
            "class_type": "VAEDecode",
            "inputs": {
                "samples": ["12", 0],
                "vae": ["4", 0],
            },
        },
        "14": {
            "class_type": "VAEDecodeAudio",
            "inputs": {
                "samples": ["12", 0],
                "vae": ["5", 0],
            },
        },
        "15": {
            "class_type": "CreateVideo",
            "inputs": {
                "images": ["13", 0],
                "audio": ["14", 0],
                "fps": 24.0,
                "bit_depth": 8,
            },
        },
        "16": {
            "class_type": "SaveVideo",
            "inputs": {
                "video": ["15", 0],
                "filename_prefix": f"video/minimax_avatar_{state_name}",
                "format": "mp4",
                "codec": "h264",
            },
        },
    }


def free_comfy_vram(client: httpx.Client) -> None:
    try:
        r = client.post(f"{COMFY_URL}/free", json={"unload_models": True, "free_memory": True})
        if r.status_code == 200:
            print("[ComfyUI] 已请求释放显存缓存", flush=True)
    except Exception as e:
        print(f"[ComfyUI] 释放显存跳过: {e}", flush=True)


async def generate_startle_sfx(dest: Path, voice: str = "zh-CN-XiaoyiNeural") -> bool:
    try:
        import edge_tts

        communicate = edge_tts.Communicate(
            text="呀！吓我一跳！",
            voice=voice,
            rate="+22%",
            pitch="+14Hz",
        )
        await communicate.save(str(dest))
        print(f"[SFX] 已生成预缓存受惊音效: {dest}", flush=True)
        return True
    except Exception as e:
        print(f"[SFX] 生成受惊音效失败（不影响主流程，将使用视频原生音/浏览器回退）: {e}", flush=True)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="使用本地 ComfyUI MiniMax H3 生成数字人状态视频")
    parser.add_argument("--quality", choices=["debug", "hd"], default="debug", help="debug=极速低清调试, hd=高清")
    parser.add_argument("--anchor", type=str, default="", help="指定自定义角色锚点图片路径（跳过文生图）")
    parser.add_argument("--anchor-prompt", type=str, default=DEFAULT_ANCHOR_PROMPT, help="生成锚点图的提示词")
    parser.add_argument(
        "--only",
        type=str,
        default="idle,listening,talking,startled",
        help="要生成的视频状态，逗号分隔 (idle,listening,talking,startled)",
    )
    parser.add_argument("--skip-anchor", action="store_true", help="如果 assets/anchor.png 已存在则直接复用")
    parser.add_argument("--seed", type=int, default=20261125, help="随机种子基准")
    parser.add_argument("--free-vram", action="store_true", help="全部生成完成后自动释放 ComfyUI 显存")
    args = parser.parse_args()

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    preset = QUALITY_PRESETS[args.quality]
    states = [s.strip() for s in args.only.split(",") if s.strip() in STATE_PROMPTS]

    print(f"=== MiniMax H3 资产生成 | 档位: {args.quality} ({preset['vid_width']}x{preset['vid_height']}, {preset['steps']}步, {preset['length']}帧) ===", flush=True)

    with make_client() as client:
        check_comfy(client)
        anchor_dest = ASSETS_DIR / "anchor.png"

        if args.anchor:
            src = Path(args.anchor)
            if not src.exists():
                raise FileNotFoundError(f"找不到指定锚点图: {src}")
            anchor_dest.write_bytes(src.read_bytes())
            print(f"[Anchor] 已使用自定义图片: {src} -> {anchor_dest}", flush=True)
        elif args.skip_anchor and anchor_dest.exists():
            print(f"[Anchor] 复用已有锚点图: {anchor_dest}", flush=True)
        else:
            print("[Anchor] 正在用本地 MiniMax H3 Image 生成角色基准图...", flush=True)
            img_graph = build_anchor_image_graph(
                prompt_text=args.anchor_prompt,
                width=preset["img_width"],
                height=preset["img_height"],
                steps=preset["img_steps"],
                seed=args.seed,
            )
            entry = queue_and_wait(client, img_graph, "AnchorImage")
            file_info = extract_first_output_file(entry)
            download_comfy_file(client, file_info, anchor_dest)
            print(f"[Anchor] 已保存: {anchor_dest} ({anchor_dest.stat().st_size // 1024} KB)", flush=True)
            # 切模型前释放图像模型显存，防止 12GB 显存切换视频模型时抖动
            free_comfy_vram(client)

        uploaded_name = upload_image(client, anchor_dest)
        print(f"[Upload] 锚点图已上传至 ComfyUI input: {uploaded_name}", flush=True)

        for idx, state in enumerate(states):
            print(f"\n--- 生成状态视频 [{idx + 1}/{len(states)}]: {state} ---", flush=True)
            vid_graph = build_video_state_graph(
                uploaded_image_name=uploaded_name,
                prompt_text=STATE_PROMPTS[state],
                state_name=state,
                preset=preset,
                seed=args.seed + 100 + idx * 17,
            )
            entry = queue_and_wait(client, vid_graph, f"Video:{state}")
            file_info = extract_first_output_file(entry)
            out_path = ASSETS_DIR / f"{state}.mp4"
            download_comfy_file(client, file_info, out_path)
            print(f"[Video:{state}] 已保存: {out_path} ({out_path.stat().st_size // 1024} KB)", flush=True)

        if args.free_vram:
            free_comfy_vram(client)

    sfx_path = ASSETS_DIR / "startle_sfx.mp3"
    if not sfx_path.exists():
        asyncio.run(generate_startle_sfx(sfx_path))

    # 写入资产元数据供前端读取
    meta = {
        "quality": args.quality,
        "preset": preset,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "states": {s: (ASSETS_DIR / f"{s}.mp4").exists() for s in STATE_PROMPTS},
        "has_anchor": (ASSETS_DIR / "anchor.png").exists(),
        "has_startle_sfx": sfx_path.exists(),
    }
    (ASSETS_DIR / "manifest.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== 全部资产准备完成 ===", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
