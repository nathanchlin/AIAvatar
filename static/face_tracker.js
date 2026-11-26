/**
 * 手机/桌面端摄像头人脸注视追踪引擎 (FaceGazeTracker)
 *
 * 架构特点（专为手机竖屏 + 弱网/局域网离线环境优化）：
 * 1. 纯前端本地推理，视频帧绝不上传服务器，零网络延迟、零带宽占用；
 * 2. 三层自动降级与无缝热升级：
 *    - Tier 1（首选高精度）：本地托管 @mediapipe/tasks-vision FaceDetector (BlazeFace Short Range, 224KB, CPU WASM 避开 Three.js WebGL 冲突)
 *    - Tier 2（硬件加速快路径）：浏览器原生 window.FaceDetector API
 *    - Tier 3（零依赖秒启兜底）：YCbCr 肤色中心 + 空间先验实时质心追踪（WASM 加载期间也可立刻响应，加载完自动热切换为 MediaPipe 眼部精准锁定）
 * 3. 自适应低通滤波（速度感知 EMA）：静止时稳如磐石零抖动，快速转头/移动手机时丝滑跟手；
 * 4. 自动兼容 iOS Safari / Android Chrome / 微信内嵌浏览器（playsinline + muted + facingMode 切换 + 页面切后台自动省电休眠）。
 */

const MEDIAPIPE_MODULE_URL = "/static/vendor/node_modules/@mediapipe/tasks-vision/vision_bundle.mjs";
const MEDIAPIPE_WASM_ROOT = "/static/vendor/node_modules/@mediapipe/tasks-vision/wasm";
const BLAZEFACE_MODEL_URL = "/static/vendor/mediapipe/blaze_face_short_range.tflite";

function clamp(v, lo, hi) {
  return Math.max(lo, Math.min(hi, v));
}

export class FaceGazeTracker {
  constructor(options = {}) {
    this.videoEl = options.videoEl || null;
    this.overlayCanvas = options.overlayCanvas || null;
    this.onGaze = typeof options.onGaze === "function" ? options.onGaze : () => {};
    this.onStatus = typeof options.onStatus === "function" ? options.onStatus : () => {};

    this.facingMode = options.facingMode || "user"; // "user" | "environment"
    this.mirrorX = options.mirrorX !== undefined ? !!options.mirrorX : true;
    this.sensitivity = Number(options.sensitivity || 1.0); // 0.65 ~ 1.5
    this.targetFps = Number(options.targetFps || 18);
    this.renderOverlay = options.renderOverlay !== undefined ? !!options.renderOverlay : false;
    this.centerU = clamp(options.centerU !== undefined ? Number(options.centerU) : 0.5, 0.12, 0.88);
    this.centerV = clamp(options.centerV !== undefined ? Number(options.centerV) : 0.5, 0.12, 0.88);

    this.active = false;
    this.pausedByVisibility = false;
    this.stream = null;
    this.rafId = 0;
    this.lastDetectTime = 0;
    this.lastVideoTime = -1;
    this.lastMpTimestamp = 0;

    // 检测引擎状态
    this.engineName = "none"; // "mediapipe" | "native" | "skin"
    this.mpDetector = null;
    this.mpLoading = null;
    this.mpFailed = false;
    this.nativeDetector = null;
    this.nativeBusy = false;

    // 兜底肤色检测离屏画布
    this.workCanvas = document.createElement("canvas");
    this.workCanvas.width = 96;
    this.workCanvas.height = 72;
    this.workCtx = this.workCanvas.getContext("2d", { willReadFrequently: true });

    // 平滑状态（速度感知自适应滤波）
    this.smooth = { x: 0, y: 0, proximity: 0.35, roll: 0 };
    this.lastDetectedAt = 0;
    this.hasFace = false;
    this.lastBox = null; // 归一化 [0..1] 供预览框绘制
    this.lastEyes = null;
    this.lastRawUV = null;

    this._onVisibilityChange = () => {
      if (!this.active) return;
      if (document.hidden) {
        this.pausedByVisibility = true;
        if (this.rafId) {
          cancelAnimationFrame(this.rafId);
          this.rafId = 0;
        }
      } else if (this.pausedByVisibility) {
        this.pausedByVisibility = false;
        this._scheduleLoop();
      }
    };
    document.addEventListener("visibilitychange", this._onVisibilityChange);
  }

  setSensitivity(val) {
    this.sensitivity = clamp(Number(val) || 1.0, 0.5, 1.8);
  }

  setMirrorX(enabled) {
    this.mirrorX = !!enabled;
  }

  setRenderOverlay(enabled) {
    this.renderOverlay = !!enabled;
    if (!this.renderOverlay) {
      this._clearOverlay();
    }
  }

  setNeutralCenter(u = 0.5, v = 0.5) {
    this.centerU = clamp(Number.isFinite(Number(u)) ? Number(u) : 0.5, 0.12, 0.88);
    this.centerV = clamp(Number.isFinite(Number(v)) ? Number(v) : 0.5, 0.12, 0.88);
  }

  calibrateCenter() {
    if (!this.active || !this.hasFace || !this.lastRawUV) {
      return { ok: false, centerU: this.centerU, centerV: this.centerV };
    }
    this.centerU = clamp(this.lastRawUV.u, 0.12, 0.88);
    this.centerV = clamp(this.lastRawUV.v, 0.12, 0.88);
    this.smooth.x = 0;
    this.smooth.y = 0;
    this.onGaze({
      active: true,
      detected: true,
      x: 0,
      y: 0,
      proximity: this.smooth.proximity,
      roll: this.smooth.roll,
      engine: this.engineName,
    });
    return { ok: true, centerU: this.centerU, centerV: this.centerV };
  }

  resetCalibration() {
    this.centerU = 0.5;
    this.centerV = 0.5;
    return { centerU: this.centerU, centerV: this.centerV };
  }

  async _ensureMediaPipeDetector() {
    if (this.mpDetector) return this.mpDetector;
    if (this.mpFailed) return null;
    if (this.mpLoading) return this.mpLoading;

    this.mpLoading = (async () => {
      try {
        this.onStatus({ phase: "loading_model", engine: this.engineName, message: "正在加载高精度人脸检测模型..." });
        const mp = await import(MEDIAPIPE_MODULE_URL);
        const FilesetResolver = mp.FilesetResolver;
        const FaceDetector = mp.FaceDetector;
        if (!FilesetResolver || !FaceDetector) {
          throw new Error("MediaPipe 模块导出不完整");
        }
        const vision = await FilesetResolver.forVisionTasks(MEDIAPIPE_WASM_ROOT);
        const detector = await FaceDetector.createFromOptions(vision, {
          baseOptions: {
            modelAssetPath: BLAZEFACE_MODEL_URL,
            delegate: "CPU", // CPU WASM 推理 224KB BlazeFace 极快，且绝不与 Three.js WebGL 抢占手机 GPU 上下文
          },
          runningMode: "VIDEO",
          minDetectionConfidence: 0.42,
          minSuppressionThreshold: 0.3,
        });
        this.mpDetector = detector;
        this.engineName = "mediapipe";
        if (this.active) {
          this.onStatus({ phase: "ready", engine: "mediapipe", message: "已启用 MediaPipe 精准眼神锁定" });
        }
        return detector;
      } catch (err) {
        console.warn("[FaceGazeTracker] MediaPipe 初始化失败，使用内置轻量检测兜底:", err);
        this.mpFailed = true;
        if (this.active) {
          this.onStatus({
            phase: "fallback",
            engine: this.engineName,
            message: this.engineName === "native" ? "已启用浏览器原生人脸检测" : "已启用轻量视觉追踪兜底",
          });
        }
        return null;
      } finally {
        this.mpLoading = null;
      }
    })();

    return this.mpLoading;
  }

  _initFastFallbacks() {
    if (!this.nativeDetector && typeof window !== "undefined" && "FaceDetector" in window) {
      try {
        this.nativeDetector = new window.FaceDetector({ fastMode: true, maxDetectedFaces: 2 });
        if (!this.mpDetector) this.engineName = "native";
      } catch (_) {
        this.nativeDetector = null;
      }
    }
    if (!this.mpDetector && !this.nativeDetector) {
      this.engineName = "skin";
    }
  }

  async start(facingMode = null) {
    if (facingMode) {
      this.facingMode = facingMode;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error("CAMERA_UNAVAILABLE");
    }

    // 若已在运行则先释放旧流
    this._stopStreamOnly();

    const constraints = {
      audio: false,
      video: {
        facingMode: { ideal: this.facingMode },
        width: { ideal: 320, max: 640 },
        height: { ideal: 240, max: 480 },
        frameRate: { ideal: 20, max: 30 },
      },
    };

    let stream = null;
    try {
      stream = await navigator.mediaDevices.getUserMedia(constraints);
    } catch (err) {
      // 某些手机/桌面摄像头不支持 ideal facingMode，降级为最简 video: true
      if (err && (err.name === "OverconstrainedError" || err.name === "ConstraintNotSatisfiedError")) {
        stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: true });
      } else {
        throw err;
      }
    }

    this.stream = stream;

    // 从真实视频轨道判断实际使用的前后摄像头方向
    const track = stream.getVideoTracks()[0];
    const settings = track && typeof track.getSettings === "function" ? track.getSettings() : {};
    if (settings.facingMode === "environment") {
      this.facingMode = "environment";
    } else if (settings.facingMode === "user") {
      this.facingMode = "user";
    }

    if (!this.videoEl) {
      this.videoEl = document.createElement("video");
    }
    this.videoEl.setAttribute("playsinline", "");
    this.videoEl.setAttribute("webkit-playsinline", "");
    this.videoEl.playsInline = true;
    this.videoEl.muted = true;
    this.videoEl.autoplay = true;
    this.videoEl.srcObject = stream;

    await new Promise((resolve, reject) => {
      const v = this.videoEl;
      let done = false;
      const finish = (ok, err) => {
        if (done) return;
        done = true;
        v.onloadedmetadata = null;
        v.onerror = null;
        if (ok) resolve();
        else reject(err || new Error("摄像头视频流加载失败"));
      };
      const timer = setTimeout(() => finish(true), 2500);
      v.onloadedmetadata = () => {
        clearTimeout(timer);
        finish(true);
      };
      v.onerror = (e) => {
        clearTimeout(timer);
        finish(false, e);
      };
      v.play().catch(() => {});
    });
    try {
      await this.videoEl.play();
    } catch (_) {}

    this.active = true;
    this.pausedByVisibility = false;
    this.lastVideoTime = -1;
    this.lastDetectedAt = 0;
    this.hasFace = false;
    this.lastBox = null;
    this.lastEyes = null;

    this._initFastFallbacks();
    this.onStatus({
      phase: "started",
      engine: this.mpDetector ? "mediapipe" : this.engineName,
      facingMode: this.facingMode,
      message: this.mpDetector ? "已启用 MediaPipe 精准眼神锁定" : "摄像头已开启，正在初始化人脸追踪...",
    });

    // 后台异步加载高精度 MediaPipe（不阻塞画面和立即可用的兜底追踪）
    if (!this.mpDetector && !this.mpFailed) {
      this._ensureMediaPipeDetector();
    }

    this._scheduleLoop();
    return { facingMode: this.facingMode };
  }

  async switchCamera() {
    const next = this.facingMode === "user" ? "environment" : "user";
    this.mirrorX = next === "user";
    if (!this.active) {
      this.facingMode = next;
      return { facingMode: next, mirrorX: this.mirrorX };
    }
    await this.start(next);
    return { facingMode: this.facingMode, mirrorX: this.mirrorX };
  }

  _stopStreamOnly() {
    if (this.rafId) {
      cancelAnimationFrame(this.rafId);
      this.rafId = 0;
    }
    if (this.stream) {
      try {
        this.stream.getTracks().forEach((t) => t.stop());
      } catch (_) {}
      this.stream = null;
    }
    if (this.videoEl) {
      try {
        this.videoEl.pause();
        this.videoEl.srcObject = null;
      } catch (_) {}
    }
  }

  stop() {
    this.active = false;
    this.pausedByVisibility = false;
    this.hasFace = false;
    this.lastBox = null;
    this.lastEyes = null;
    this.lastRawUV = null;
    this._stopStreamOnly();
    this._clearOverlay();
    this.onGaze({
      active: false,
      detected: false,
      x: 0,
      y: 0,
      proximity: 0.35,
      roll: 0,
      engine: this.engineName,
    });
    this.onStatus({ phase: "stopped", engine: this.engineName, message: "摄像头追踪已关闭" });
  }

  dispose() {
    this.stop();
    document.removeEventListener("visibilitychange", this._onVisibilityChange);
    if (this.mpDetector && typeof this.mpDetector.close === "function") {
      try { this.mpDetector.close(); } catch (_) {}
      this.mpDetector = null;
    }
  }

  _scheduleLoop() {
    if (!this.active || this.pausedByVisibility) return;
    const tick = (now) => {
      if (!this.active || this.pausedByVisibility) return;
      this.rafId = requestAnimationFrame(tick);

      const minInterval = 1000 / Math.max(6, this.targetFps);
      if (now - this.lastDetectTime < minInterval) return;

      const v = this.videoEl;
      if (!v || v.readyState < 2 || !v.videoWidth || !v.videoHeight) return;

      // 若视频帧未推进则跳过重复推理
      if (v.currentTime === this.lastVideoTime && now - this.lastDetectTime < 140) return;
      this.lastVideoTime = v.currentTime;
      this.lastDetectTime = now;

      this._processFrame(v, now);
    };
    this.rafId = requestAnimationFrame(tick);
  }

  _processFrame(video, now) {
    // Tier 1: MediaPipe BlazeFace (精准人脸框 + 双眼关键点)
    if (this.mpDetector) {
      try {
        const ts = Math.max(Math.round(now), this.lastMpTimestamp + 1);
        this.lastMpTimestamp = ts;
        const result = this.mpDetector.detectForVideo(video, ts);
        const dets = result && result.detections ? result.detections : [];
        const best = this._pickBestMediaPipeFace(dets, video.videoWidth, video.videoHeight);
        if (best) {
          this._emitDetection(best, now, "mediapipe");
        } else {
          this._emitNoFace(now, "mediapipe");
        }
        return;
      } catch (err) {
        console.warn("[FaceGazeTracker] MediaPipe 单帧推理异常:", err);
      }
    }

    // Tier 2: 浏览器原生 FaceDetector
    if (this.nativeDetector && !this.nativeBusy) {
      this.nativeBusy = true;
      this.nativeDetector
        .detect(video)
        .then((faces) => {
          this.nativeBusy = false;
          if (!this.active) return;
          const best = this._pickBestNativeFace(faces, video.videoWidth, video.videoHeight);
          if (best) this._emitDetection(best, performance.now(), "native");
          else this._emitNoFace(performance.now(), "native");
        })
        .catch(() => {
          this.nativeBusy = false;
          this.nativeDetector = null;
          this.engineName = "skin";
        });
      return;
    }

    // Tier 3: 零依赖 YCbCr 肤色中心兜底（WASM 加载中或旧浏览器仍可立即跟随）
    const skinFace = this._detectSkinFaceCentroid(video);
    if (skinFace) {
      this._emitDetection(skinFace, now, "skin");
    } else {
      this._emitNoFace(now, "skin");
    }
  }

  _pickBestMediaPipeFace(detections, vw, vh) {
    if (!detections || !detections.length || !vw || !vh) return null;
    let best = null;
    let bestScore = -1;

    for (const det of detections) {
      const box = det.boundingBox;
      if (!box || box.width <= 0 || box.height <= 0) continue;
      const areaRatio = (box.width * box.height) / (vw * vh);
      const conf = det.categories && det.categories[0] ? det.categories[0].score : 0.6;
      const cx = (box.originX + box.width * 0.5) / vw;
      const cy = (box.originY + box.height * 0.5) / vh;
      // 优先选择面积最大（离手机最近）且靠近画面中上部的面部
      const centerBonus = 1 - 0.25 * Math.hypot(cx - 0.5, cy - 0.45);
      const score = Math.sqrt(areaRatio) * conf * centerBonus;
      if (score > bestScore) {
        bestScore = score;
        best = det;
      }
    }
    if (!best) return null;

    const box = best.boundingBox;
    const normBox = {
      x: clamp(box.originX / vw, 0, 1),
      y: clamp(box.originY / vh, 0, 1),
      w: clamp(box.width / vw, 0.02, 1),
      h: clamp(box.height / vh, 0.02, 1),
    };

    // BlazeFace 6 关键点顺序：0=右眼, 1=左眼, 2=鼻尖, 3=嘴中心, 4=右耳, 5=左耳 (已归一化 0..1)
    const kps = best.keypoints || [];
    let u = normBox.x + normBox.w * 0.5;
    let v = normBox.y + normBox.h * 0.40; // 眼睛位于人脸框上约 40% 处
    let roll = 0;
    let eyes = null;

    if (kps.length >= 2) {
      const e0 = kps[0];
      const e1 = kps[1];
      const eyeMidX = (e0.x + e1.x) * 0.5;
      const eyeMidY = (e0.y + e1.y) * 0.5;
      // 融合眼部中心(70%)与人脸框中心(30%)，既精准盯眼又极其稳定
      u = eyeMidX * 0.72 + u * 0.28;
      v = eyeMidY * 0.72 + v * 0.28;
      // 计算歪头角度（roll）
      const leftEye = e0.x < e1.x ? e0 : e1;
      const rightEye = e0.x < e1.x ? e1 : e0;
      roll = clamp(Math.atan2(rightEye.y - leftEye.y, Math.max(0.01, rightEye.x - leftEye.x)), -0.45, 0.45);
      eyes = [
        { x: clamp(e0.x, 0, 1), y: clamp(e0.y, 0, 1) },
        { x: clamp(e1.x, 0, 1), y: clamp(e1.y, 0, 1) },
      ];
    }

    const proximity = clamp((normBox.h - 0.12) / 0.52, 0, 1);
    return { u, v, proximity, roll, box: normBox, eyes };
  }

  _pickBestNativeFace(faces, vw, vh) {
    if (!faces || !faces.length || !vw || !vh) return null;
    let best = null;
    let bestArea = 0;
    for (const f of faces) {
      const b = f.boundingBox;
      if (!b) continue;
      const area = b.width * b.height;
      if (area > bestArea) {
        bestArea = area;
        best = f;
      }
    }
    if (!best) return null;
    const b = best.boundingBox;
    const normBox = {
      x: clamp(b.x / vw, 0, 1),
      y: clamp(b.y / vh, 0, 1),
      w: clamp(b.width / vw, 0.02, 1),
      h: clamp(b.height / vh, 0.02, 1),
    };
    const u = normBox.x + normBox.w * 0.5;
    const v = normBox.y + normBox.h * 0.40;
    const proximity = clamp((normBox.h - 0.12) / 0.52, 0, 1);
    return { u, v, proximity, roll: 0, box: normBox, eyes: null };
  }

  _detectSkinFaceCentroid(video) {
    const W = this.workCanvas.width;
    const H = this.workCanvas.height;
    try {
      this.workCtx.drawImage(video, 0, 0, W, H);
      const data = this.workCtx.getImageData(0, 0, W, H).data;
      let sumX = 0;
      let sumY = 0;
      let sumW = 0;
      let minX = W, maxX = 0, minY = H, maxY = 0;

      for (let y = 4; y < H - 4; y++) {
        // 上中区域加权，避开手部与下方躯干
        const yWeight = y < H * 0.78 ? 1.0 : 0.35;
        for (let x = 4; x < W - 4; x++) {
          const i = (y * W + x) * 4;
          const r = data[i];
          const g = data[i + 1];
          const b = data[i + 2];
          // YCbCr 通用肤色检测（兼容黄/白/深肤色与室内暖冷光）
          const Y = 0.299 * r + 0.587 * g + 0.114 * b;
          if (Y < 45 || Y > 245) continue;
          const Cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b;
          const Cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b;
          if (Cb >= 77 && Cb <= 127 && Cr >= 133 && Cr <= 176) {
            sumX += x * yWeight;
            sumY += y * yWeight;
            sumW += yWeight;
            if (x < minX) minX = x;
            if (x > maxX) maxX = x;
            if (y < minY) minY = y;
            if (y > maxY) maxY = y;
          }
        }
      }

      if (sumW < W * H * 0.025) return null;
      const u = clamp(sumX / sumW / W, 0.05, 0.95);
      const v = clamp(sumY / sumW / H, 0.05, 0.95);
      const bw = clamp((maxX - minX) / W, 0.12, 0.75);
      const bh = clamp((maxY - minY) / H, 0.14, 0.75);
      return {
        u,
        v,
        proximity: clamp((bh - 0.14) / 0.5, 0, 1),
        roll: 0,
        box: { x: clamp(u - bw * 0.5, 0, 1), y: clamp(v - bh * 0.5, 0, 1), w: bw, h: bh },
        eyes: null,
      };
    } catch (_) {
      return null;
    }
  }

  _emitDetection(det, now, engine) {
    this.lastDetectedAt = now;
    this.hasFace = true;
    this.lastBox = det.box;
    this.lastEyes = det.eyes;
    this.lastRawUV = { u: det.u, v: det.v };

    // 坐标映射（支持自定义中性零点 centerU / centerV 校准）：
    // 前置摄像头 (mirrorX=true)：用户向右移动 -> 原始图像中脸偏左 (u < centerU) -> rawX > 0 -> 3D 角色向屏幕右侧 (+X) 注视
    // 后置摄像头 (mirrorX=false)：rawX = (u - centerU) * 2
    const rawX = this.mirrorX ? (this.centerU - det.u) * 2.0 : (det.u - this.centerU) * 2.0;
    // 图像上方 v < centerV -> rawY > 0 -> 3D 角色向上 (+Y) 注视（移除原有向下写死偏移，由眼平基准与微调选项精确控制）
    const rawY = (this.centerV - det.v) * 2.0;

    const targetX = clamp(rawX * this.sensitivity, -1.0, 1.0);
    const targetY = clamp(rawY * this.sensitivity, -1.0, 1.0);
    const targetRoll = this.mirrorX ? -det.roll : det.roll;

    // 速度感知自适应滤波（类似简化 One-Euro Filter）：微小抖动强抑制，明显转头瞬间跟上
    const dx = Math.abs(targetX - this.smooth.x);
    const dy = Math.abs(targetY - this.smooth.y);
    const speed = Math.hypot(dx, dy);
    const alpha = clamp(0.16 + speed * 0.95, 0.16, 0.62);

    this.smooth.x += (targetX - this.smooth.x) * alpha;
    this.smooth.y += (targetY - this.smooth.y) * alpha;
    this.smooth.proximity += (det.proximity - this.smooth.proximity) * 0.15;
    this.smooth.roll += (targetRoll - this.smooth.roll) * 0.22;

    if (this.renderOverlay) {
      this._drawOverlay(true);
    }
    this.onGaze({
      active: true,
      detected: true,
      x: this.smooth.x,
      y: this.smooth.y,
      proximity: this.smooth.proximity,
      roll: this.smooth.roll,
      engine,
    });
  }

  _emitNoFace(now, engine) {
    const lostMs = now - this.lastDetectedAt;
    // 超过 900ms 未检测到人脸，判定离框并平滑回正
    const detected = this.lastDetectedAt > 0 && lostMs < 900;
    this.hasFace = detected;
    if (!detected) {
      this.lastBox = null;
      this.lastEyes = null;
      this.lastRawUV = null;
      this.smooth.x *= 0.88;
      this.smooth.y *= 0.88;
      this.smooth.roll *= 0.85;
    }
    if (this.renderOverlay) {
      this._drawOverlay(detected);
    }
    this.onGaze({
      active: true,
      detected,
      x: this.smooth.x,
      y: this.smooth.y,
      proximity: this.smooth.proximity,
      roll: this.smooth.roll,
      engine,
    });
  }

  _clearOverlay() {
    if (!this.overlayCanvas) return;
    const ctx = this.overlayCanvas.getContext("2d");
    if (ctx) ctx.clearRect(0, 0, this.overlayCanvas.width, this.overlayCanvas.height);
  }

  _drawOverlay(detected) {
    if (!this.renderOverlay) return;
    const canvas = this.overlayCanvas;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const cw = Math.max(1, Math.round(rect.width * dpr));
    const ch = Math.max(1, Math.round(rect.height * dpr));
    if (canvas.width !== cw || canvas.height !== ch) {
      canvas.width = cw;
      canvas.height = ch;
    }

    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, cw, ch);
    if (!detected || !this.lastBox) return;

    // 精确匹配 video 的 object-fit: cover 裁切，保证竖屏/横屏手机上追踪框与眼睛光点像素级贴合
    const vw = this.videoEl?.videoWidth || cw;
    const vh = this.videoEl?.videoHeight || ch;
    const scale = Math.max(cw / vw, ch / vh);
    const drawW = vw * scale;
    const drawH = vh * scale;
    const offX = (cw - drawW) * 0.5;
    const offY = (ch - drawH) * 0.5;

    // 预览图对前置摄像头应用了 CSS scaleX(-1)，所以绘制坐标也相应镜像对齐！
    const mapX = (u) => offX + (this.mirrorX ? 1 - u : u) * drawW;
    const mapY = (v) => offY + v * drawH;

    const b = this.lastBox;
    const bx = offX + (this.mirrorX ? 1 - b.x - b.w : b.x) * drawW;
    const by = offY + b.y * drawH;
    const bw = b.w * drawW;
    const bh = b.h * drawH;

    // 柔和青绿科技感四角跟踪框
    ctx.strokeStyle = "rgba(52, 211, 153, 0.92)";
    ctx.lineWidth = Math.max(1.5, 1.8 * dpr);
    const corner = Math.max(6 * dpr, Math.min(bw, bh) * 0.22);

    ctx.beginPath();
    // 左上
    ctx.moveTo(bx, by + corner); ctx.lineTo(bx, by); ctx.lineTo(bx + corner, by);
    // 右上
    ctx.moveTo(bx + bw - corner, by); ctx.lineTo(bx + bw, by); ctx.lineTo(bx + bw, by + corner);
    // 左下
    ctx.moveTo(bx, by + bh - corner); ctx.lineTo(bx, by + bh); ctx.lineTo(bx + corner, by + bh);
    // 右下
    ctx.moveTo(bx + bw - corner, by + bh); ctx.lineTo(bx + bw, by + bh); ctx.lineTo(bx + bw, by + bh - corner);
    ctx.stroke();

    // 双眼精准锁定光点
    if (this.lastEyes && this.lastEyes.length >= 2) {
      ctx.fillStyle = "rgba(96, 165, 250, 0.95)";
      for (const eye of this.lastEyes) {
        ctx.beginPath();
        ctx.arc(mapX(eye.x), mapY(eye.y), 2.6 * dpr, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  }
}
