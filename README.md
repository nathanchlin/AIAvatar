# AIAvatar · 3D 实时语音虚拟人像 & 写实对口型对话助手（小汐）

跨平台（Linux / Windows）实时语音虚拟人像与资料沟通系统：基于 **Three.js + `@pixiv/three-vrm`** 的 60fps 实时语音驱动 3D 虚拟形象，集成参考 `xiaozhi-esp32-server` 打造的低延迟本地语音栈、实时网络信息检索、沟通资料库，并支持在 RTX 4090 / ComfyUI 上启用 **MiniMax H3 4 步流式写实对口型视频生成**。

## 核心特性

1. **Three.js 60fps 3D 实时虚拟人像（`static/avatar3d.js`）**
   - 毫秒级中文拼音口型时间轴（`speech_choreography.py`）：结合 Edge TTS `WordBoundary` + `pypinyin`，支持 `b/p/m` 真实双唇闭合与复韵母平滑过渡；
   - 底层 57 维细分微表情控制：独立驱动 `Fcl_BRW_*` 眉毛、`Fcl_Eye_Fun` 睁眼笑意与 `Fcl_MTH_Up` 嘴角微笑，重音自动轻抬眉与轻点头；
   - 逐词语义手势时间轴 + 二阶弹簧骨骼动力学：打招呼、自我指代、列要点、展开总结、权衡对比、摊手询问、托腮思考、共情关切；
   - 支持直接拖拽任意标准 `.vrm` 模型文件热切换 3D 形象；
   - **浏览器 IndexedDB 离线缓存**：支持一键下载缓存 3D 模型与实时进度条，公网/弱网二次访问 0 流量秒开。

2. **低延迟语音栈与智能防回声（`server.py`）**
   - **VAD**：`Silero VAD (ONNX)` WebSocket 32ms 帧级检测，支持可调停顿时长（防长句截断）与移动端自动灵敏度调优；
   - **ASR**：阿里 FunASR `SenseVoiceSmall (int8 ONNX)` 本地毫秒级中英文识别 + 拼音级扬声器自回声过滤与打断口令放行；
   - **LLM 流式对话**：大 `max_tokens`（1536/2048）完整句累积延续 + 截断自动续写 + Ollama/Gemma-4 思考模型亚秒级加速（`reasoning_effort="none"`）；
   - **TTS**：`Edge TTS 7.3.1` 神经音色 + 本地离线 `Kokoro v1.1-zh` 神经音色兜底 + 强符号/表情朗读过滤。

3. **实时联网检索 & 沟通资料库**
   - **实时联网检索**：支持 `auto / always / off` 三种策略，国内免代理直连 `cn.bing.com` 并自动兼容代理环境 `ddgs` 兜底，回复附可点击来源卡片；
   - **资料库**：支持上传/拖拽 PDF、Word (`.docx`)、Markdown、TXT、CSV、JSON、代码文件或粘贴文本，自动分块相关性召回注入上下文。

4. **RTX 4090 / ComfyUI MiniMax H3 流式写实对口型**
   - 对接本地/云端 ComfyUI + `MiniMaxH3AudioConditioningT8`（`lock_source` 精准锁音）+ 4 步 Turbo 加速 LoRA；
   - 零等待流式预渲染队列：收到首句即按短段提交 GPU 渲染，播放当前片段的同时后台并行预渲染下一段，支持 `448×256`（极速实时流）/ `512×288`（均衡清晰）/ `768×448`（高清写实）三档画质。

5. **全端体验：内网双协议 HTTPS + 手机竖屏最大化适配**
   - 启动时自动生成多 SAN 自签名证书，**同时监听 HTTP `8765` 与 HTTPS `8766`**，局域网设备访问 `https://<LAN-IP>:8766/` 即可原生解锁浏览器麦克风；
   - 手机竖屏专属适配：默认 66%～74% 屏高巨幅 3D 舞台 + 竖屏 3D 半身镜头自适应 + 一键 **`⛶ 全屏`** 沉浸模式 + 移动端 Web Audio 同步解锁防静音。

## 快速部署

### 1. 安装 Python 依赖
```bash
pip install -r requirements.txt
```

### 2. 安装前端本地依赖（Three.js + `@pixiv/three-vrm`）
```bash
cd static/vendor
npm install
cd ../..
```

### 3. 下载本地语音模型（Silero VAD + SenseVoiceSmall + Kokoro v1.1-zh）
```bash
python setup_voice_models.py
```

### 4. 启动服务（Linux / Windows 通用）
```bash
# 可通过环境变量指定 LLM（如 Ollama / llama.cpp / OpenAI 兼容接口）、ComfyUI 与端口
export LLM_URL="http://127.0.0.1:11434/v1/chat/completions"
export LLM_MODEL="huihui_ai/gemma-4-abliterated:26b-qat"
export COMFY_URL="http://127.0.0.1:8188"
export PORT=8765
export HTTPS_PORT=8766

python server.py
```

- **本机访问**：`http://127.0.0.1:8765/`
- **局域网支持麦克风访问**：`https://<你的内网IP>:8766/`（Windows 局域网放行端口可右键管理员运行 `放行内网8765端口(右键以管理员身份运行).cmd`）
