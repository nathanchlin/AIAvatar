/**
 * Three.js + @pixiv/three-vrm 高自然度 60fps 语音驱动 3D 虚拟人像引擎 (v5)
 *
 * 核心升级（专为贴合台词内容的自然对话设计）：
 * 1. 底层细分面部微表情驱动（VRoid Fcl_BRW_* 眉毛 / Fcl_EYE_* 笑眼微睁 / Fcl_MTH_Up 嘴角微笑 独立解耦控制，告别闭眼假笑！）
 * 2. 毫秒级中文拼音口型时间轴（含 b/p/m 真实双唇闭合、复韵母平滑过渡 + 实时音频能量门控）
 * 3. 词级动作编排时间轴（预备拍 -> 语义重音拍 -> 自然回落）：打招呼、指自己(“我”)、向用户摊手询问(“你/吗”)、列举要点(“第一/第二”)、权衡对比(“但是”)、重点拍击、思考托腮、共情关切
 * 4. 二阶临界阻尼弹簧骨骼动力学 + 近端到远端关节级联延迟 + 五指渐进自然弯曲弧度
 */
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { VRMLoaderPlugin, VRMUtils } from "@pixiv/three-vrm";

/** 二阶临界阻尼弹簧（消除机械线性插值折角，赋予真实惯性与柔和缓冲） */
class SpringEuler {
  constructor(omega = 10) {
    this.omega = omega;
    this.x = 0; this.y = 0; this.z = 0;
    this.vx = 0; this.vy = 0; this.vz = 0;
  }
  step(tx, ty, tz, dt, omegaOverride = null) {
    const w = omegaOverride || this.omega;
    const k = w * w;
    const d = 2 * w * 0.92; // 微欠阻尼带来极轻微生动的肌肉弹性
    const ax = -k * (this.x - tx) - d * this.vx;
    const ay = -k * (this.y - ty) - d * this.vy;
    const az = -k * (this.z - tz) - d * this.vz;
    this.vx += ax * dt; this.vy += ay * dt; this.vz += az * dt;
    this.x += this.vx * dt; this.y += this.vy * dt; this.z += this.vz * dt;
  }
}

const FINGER_GROUPS = [
  { prefix: "Thumb",  ratio: 0.45 },
  { prefix: "Index",  ratio: 0.65 }, // 食指弯曲最轻，自然灵动
  { prefix: "Middle", ratio: 0.88 },
  { prefix: "Ring",   ratio: 1.08 },
  { prefix: "Little", ratio: 1.25 }, // 小指弯曲最深，贴合真实人手放松弧度
];

export class Avatar3DEngine {
  constructor(containerEl, onStatus = () => {}) {
    this.container = containerEl;
    this.onStatus = onStatus;
    this.vrm = null;
    this.faceMeshes = [];
    this.hasFineMorphs = false;

    this.state = "idle"; // "idle" | "listening" | "thinking" | "speaking"
    this.clock = new THREE.Clock();
    this.elapsed = 0;

    // 当前正在播放的语音对象与精确时间轴
    this.activeAudio = null;
    this.activeTimeline = null;

    // 细分微表情平滑状态
    this.faceState = {
      smile: 0.22,
      eyeSmile: 0.16,
      eyeWide: 0.0,
      browUp: 0.08,
      browFurrow: 0.0,
      browSad: 0.0,
      joyPulse: 0.0,
    };
    this.manualFaceTarget = null;
    this.manualFaceTimer = 0;

    // 手势状态与手动预览
    this.manualGesture = null;
    this.manualGestureTimer = 0;
    this.activeActionStart = 0;

    // 眨眼状态
    this.blinkTimer = 2.2;
    this.blinkPhase = 0;
    this.doubleBlinkQueued = false;

    // 视线追踪 + 认知视线偏移
    this.pointer = { x: 0, y: 0 };
    this.smoothPointer = { x: 0, y: 0 };
    this.saccadeTarget = { x: 0, y: 0 };
    this.saccadeCurrent = { x: 0, y: 0 };
    this.saccadeTimer = 1.5;
    this.lookAtObj = new THREE.Object3D();

    // 音频实时分析器
    this.audioCtx = null;
    this.analyser = null;
    this.timeData = null;
    this.freqData = null;
    this.connectedSources = new WeakMap();
    this.visemes = { aa: 0, ih: 0, ou: 0, ee: 0, oh: 0 };
    this.voiceEnergy = 0;
    this.prevVoiceEnergy = 0;
    this.pitchAccent = 0;

    // 关节二阶弹簧实例（近端慢稳 -> 远端灵巧，形成自然级联延迟）
    this.springs = {
      hips: new SpringEuler(5.5),
      spine: new SpringEuler(6.5),
      chest: new SpringEuler(7.5),
      neck: new SpringEuler(9.5),
      head: new SpringEuler(11.0),
      leftShoulder: new SpringEuler(8.5),
      rightShoulder: new SpringEuler(8.5),
      leftUpperArm: new SpringEuler(9.0),
      rightUpperArm: new SpringEuler(9.0),
      leftLowerArm: new SpringEuler(11.5),
      rightLowerArm: new SpringEuler(11.5),
      leftHand: new SpringEuler(14.0),
      rightHand: new SpringEuler(14.0),
    };
    this.fingerCurlL = 0.28;
    this.fingerCurlR = 0.28;
    this.indexExtendR = 0.0; // 列举要点时食指自然舒展度

    this.defaultCamPos = new THREE.Vector3(0, 1.38, 1.60);
    this.defaultTarget = new THREE.Vector3(0, 1.34, 0);

    this._initScene();
    this._bindEvents();
    this._animate();
  }

  _initScene() {
    const w = this.container.clientWidth || 800;
    const h = this.container.clientHeight || 450;

    this.scene = new THREE.Scene();
    this.scene.add(this.lookAtObj);

    const bgCanvas = document.createElement("canvas");
    bgCanvas.width = 512;
    bgCanvas.height = 512;
    const ctx = bgCanvas.getContext("2d");
    const grad = ctx.createRadialGradient(256, 185, 30, 256, 256, 360);
    grad.addColorStop(0, "#243656");
    grad.addColorStop(0.5, "#141d30");
    grad.addColorStop(1, "#090d16");
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, 512, 512);
    const bgTex = new THREE.CanvasTexture(bgCanvas);
    bgTex.colorSpace = THREE.SRGBColorSpace;
    this.scene.background = bgTex;

    this.camera = new THREE.PerspectiveCamera(27, w / h, 0.1, 20);
    this.camera.position.copy(this.defaultCamPos);

    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, powerPreference: "high-performance" });
    this.renderer.setSize(w, h);
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.07;
    this.container.innerHTML = "";
    this.container.appendChild(this.renderer.domElement);

    const hemi = new THREE.HemisphereLight(0xeef4ff, 0x1e293b, 1.08);
    hemi.position.set(0, 3, 0);
    this.scene.add(hemi);

    const keyLight = new THREE.DirectionalLight(0xfff5ea, 1.48);
    keyLight.position.set(1.2, 2.1, 2.2);
    this.scene.add(keyLight);

    const rimLight = new THREE.DirectionalLight(0x93c5fd, 0.88);
    rimLight.position.set(-1.8, 1.9, -1.6);
    this.scene.add(rimLight);

    const fillLight = new THREE.DirectionalLight(0xfde68a, 0.36);
    fillLight.position.set(-1.3, 1.2, 1.5);
    this.scene.add(fillLight);

    const starGeo = new THREE.BufferGeometry();
    const count = 45;
    const pos = new Float32Array(count * 3);
    for (let i = 0; i < count; i++) {
      pos[i * 3] = (Math.random() - 0.5) * 4.5;
      pos[i * 3 + 1] = 0.5 + Math.random() * 2.2;
      pos[i * 3 + 2] = -1.2 - Math.random() * 2.5;
    }
    starGeo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    const starMat = new THREE.PointsMaterial({ color: 0x93c5fd, size: 0.028, transparent: true, opacity: 0.45 });
    this.bokehPoints = new THREE.Points(starGeo, starMat);
    this.scene.add(this.bokehPoints);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.07;
    this.controls.enablePan = true;
    this.controls.minDistance = 0.7;
    this.controls.maxDistance = 3.5;
    this.controls.minPolarAngle = Math.PI * 0.25;
    this.controls.maxPolarAngle = Math.PI * 0.65;
    this.controls.target.copy(this.defaultTarget);
    this.controls.update();

    this.loader = new GLTFLoader();
    this.loader.register((parser) => new VRMLoaderPlugin(parser));
  }

  _bindEvents() {
    this._onResize = () => {
      if (!this.container) return;
      const w = this.container.clientWidth;
      const h = this.container.clientHeight;
      if (w > 0 && h > 0) {
        const prevAspect = this.camera.aspect || 1;
        const nextAspect = w / h;
        this.camera.aspect = nextAspect;
        this.camera.updateProjectionMatrix();
        this.renderer.setSize(w, h);
        // 竖屏/横屏切换或高度大幅变化时自动重算最佳半身构图
        if (this.vrm && ((prevAspect < 1) !== (nextAspect < 1) || Math.abs(prevAspect - nextAspect) > 0.25)) {
          this._autoFrameCamera();
        }
      }
    };
    window.addEventListener("resize", this._onResize);
    if (window.ResizeObserver) {
      this._ro = new ResizeObserver(() => this._onResize());
      this._ro.observe(this.container);
    }

    this.container.addEventListener("pointermove", (e) => {
      const rect = this.container.getBoundingClientRect();
      this.pointer.x = ((e.clientX - rect.left) / Math.max(1, rect.width)) * 2 - 1;
      this.pointer.y = -(((e.clientY - rect.top) / Math.max(1, rect.height)) * 2 - 1);
    });
    this.container.addEventListener("pointerleave", () => {
      this.pointer.x = 0;
      this.pointer.y = 0;
    });
  }

  _discoverFineFaceMorphs(root) {
    this.faceMeshes = [];
    this.hasFineMorphs = false;
    root.traverse((obj) => {
      if (obj.isMesh && obj.morphTargetDictionary && obj.morphTargetInfluences) {
        const keys = Object.keys(obj.morphTargetDictionary);
        const map = {};
        const findKey = (suffix) => keys.find((k) => k.toLowerCase().endsWith(suffix.toLowerCase()));
        const candidates = {
          brwFun: findKey("Fcl_BRW_Fun"),
          brwJoy: findKey("Fcl_BRW_Joy"),
          brwSurprised: findKey("Fcl_BRW_Surprised"),
          brwSorrow: findKey("Fcl_BRW_Sorrow"),
          brwAngry: findKey("Fcl_BRW_Angry"),
          eyeFun: findKey("Fcl_Eye_Fun"),
          eyeJoy: findKey("Fcl_EYE_Joy"),
          eyeSurprised: findKey("Fcl_EYE_Surprised"),
          eyeSpread: findKey("Fcl_EYE_Spread"),
          eyeSorrow: findKey("Fcl_EYE_Sorrow"),
          mthUp: findKey("Fcl_MTH_Up"),
          mthDown: findKey("Fcl_MTH_Down"),
          mthFun: findKey("Fcl_MTH_Fun"),
        };
        const resolved = {};
        for (const [alias, fullKey] of Object.entries(candidates)) {
          if (fullKey !== undefined) {
            resolved[alias] = obj.morphTargetDictionary[fullKey];
            this.hasFineMorphs = true;
          }
        }
        if (Object.keys(resolved).length > 0) {
          this.faceMeshes.push({ mesh: obj, morphs: resolved });
        }
      }
    });
  }

  _setFineMorph(alias, value) {
    const v = Math.max(0, Math.min(1, value));
    for (const item of this.faceMeshes) {
      const idx = item.morphs[alias];
      if (idx !== undefined) {
        item.mesh.morphTargetInfluences[idx] = v;
      }
    }
  }

  async loadVRM(url) {
    this.onStatus("正在加载 3D 虚拟人像...");
    return new Promise((resolve, reject) => {
      this.loader.load(
        url,
        (gltf) => {
          const vrm = gltf.userData.vrm;
          if (!vrm) {
            reject(new Error("该文件不是有效的 VRM 模型"));
            return;
          }
          if (this.vrm) {
            this.scene.remove(this.vrm.scene);
            VRMUtils.deepDispose(this.vrm.scene);
            this.vrm = null;
          }

          VRMUtils.removeUnnecessaryVertices(gltf.scene);
          VRMUtils.combineSkeletons(gltf.scene);
          VRMUtils.rotateVRM0(vrm);
          vrm.scene.traverse((obj) => { obj.frustumCulled = false; });

          this.scene.add(vrm.scene);
          this.vrm = vrm;
          this._discoverFineFaceMorphs(vrm.scene);

          // 初始化双臂自然垂落初值，避免首帧从 T-pose 甩下来
          this.springs.leftUpperArm.z = 1.22;
          this.springs.rightUpperArm.z = -1.22;
          this.springs.leftLowerArm.y = -0.22;
          this.springs.rightLowerArm.y = 0.22;

          this._autoFrameCamera();
          if (vrm.lookAt) vrm.lookAt.target = this.lookAtObj;

          this.triggerGesture("wave", 2.2);
          this.setEmotion("happy", 2.6);

          const title = vrm.meta?.name || vrm.meta?.title || "3D 虚拟形象";
          this.onStatus(`3D 形象就绪 (${title})`);
          resolve(vrm);
        },
        (progress) => {
          if (progress.total) {
            const pct = Math.min(99, Math.round((progress.loaded / progress.total) * 100));
            this.onStatus(`加载 3D 模型中 ${pct}%...`);
          }
        },
        (err) => {
          this.onStatus("3D 模型加载失败");
          reject(err);
        }
      );
    });
  }

  _getBone(name) {
    return this.vrm?.humanoid?.getNormalizedBoneNode(name) || null;
  }

  _autoFrameCamera() {
    if (!this.vrm) return;
    this.vrm.scene.updateMatrixWorld(true);
    const head = this._getBone("head");
    const chest = this._getBone("upperChest") || this._getBone("chest") || this._getBone("spine");
    const headPos = new THREE.Vector3(0, 1.45, 0);
    const chestPos = new THREE.Vector3(0, 1.18, 0);
    if (head) head.getWorldPosition(headPos);
    if (chest) chest.getWorldPosition(chestPos);

    const aspect = this.camera.aspect || 1.77;
    if (aspect < 1.0) {
      // 手机竖屏最大化构图：头部位于黄金上三分线，完整容纳双肩与自然讲解手势，同时把人物画幅撑满竖屏
      const focusY = headPos.y * 0.46 + chestPos.y * 0.54;
      const dist = Math.min(2.05, Math.max(1.62, 1.24 / Math.max(0.55, aspect)));
      this.defaultTarget.set(0, focusY, 0);
      this.defaultCamPos.set(0, focusY + 0.04, dist);
    } else {
      const focusY = headPos.y * 0.62 + chestPos.y * 0.38;
      this.defaultTarget.set(0, focusY, 0);
      this.defaultCamPos.set(0, focusY + 0.05, 1.60);
    }
    this.resetCamera();
  }

  resetCamera() {
    this.camera.position.copy(this.defaultCamPos);
    this.controls.target.copy(this.defaultTarget);
    this.controls.update();
  }

  setState(nextState) {
    const prev = this.state;
    this.state = nextState || "idle";
    if (this.state !== "speaking") {
      this.activeAudio = null;
      this.activeTimeline = null;
    }
    if (this.state === "thinking" && prev !== "thinking") {
      this.triggerGesture("think_chin", 4.0);
      this.setEmotion("focused", 4.0);
    } else if (this.state === "listening" && prev !== "listening") {
      this.triggerGesture("nod_agree", 1.8);
      this.setEmotion("relaxed", 3.0);
    }
  }

  setEmotion(name, holdSec = 3.2) {
    const map = {
      neutral:   { smile: 0.20, eyeSmile: 0.14, browUp: 0.08, browFurrow: 0, browSad: 0, eyeWide: 0, joyPulse: 0 },
      happy:     { smile: 0.72, eyeSmile: 0.62, browUp: 0.30, browFurrow: 0, browSad: 0, eyeWide: 0, joyPulse: 0.38 },
      relaxed:   { smile: 0.45, eyeSmile: 0.36, browUp: 0.18, browFurrow: 0, browSad: 0, eyeWide: 0, joyPulse: 0 },
      surprised: { smile: 0.16, eyeSmile: 0.0,  browUp: 0.82, browFurrow: 0, browSad: 0, eyeWide: 0.65, joyPulse: 0 },
      sad:       { smile: 0.0,  eyeSmile: 0.0,  browUp: 0.0,  browFurrow: 0, browSad: 0.62, eyeWide: 0, joyPulse: 0 },
      focused:   { smile: 0.10, eyeSmile: 0.05, browUp: 0.0,  browFurrow: 0.38, browSad: 0, eyeWide: 0.08, joyPulse: 0 },
    };
    if (map[name]) {
      this.manualFaceTarget = map[name];
      this.manualFaceTimer = holdSec;
    }
  }

  triggerGesture(name, durationSec = 2.6) {
    const alias = { think: "think_chin", surprise: "surprise_soft", explain_one: "explain_open" }[name] || name;
    this.manualGesture = alias;
    this.manualGestureTimer = durationSec;
    this.activeActionStart = this.elapsed;
  }

  ensureAudioAnalyser() {
    if (!this.audioCtx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      this.audioCtx = new AC();
      this.analyser = this.audioCtx.createAnalyser();
      this.analyser.fftSize = 1024;
      this.analyser.smoothingTimeConstant = 0.35;
      this.analyser.connect(this.audioCtx.destination);
      this.timeData = new Float32Array(this.analyser.fftSize);
      this.freqData = new Uint8Array(this.analyser.frequencyBinCount);
    }
    if (this.audioCtx.state === "suspended") {
      this.audioCtx.resume().catch(() => {});
    }
    // 移动端（iOS Safari / Android Chrome / 微信）同步播放 1 帧静音以彻底解锁 Web Audio 输出
    if (!this._audioUnlocked && this.audioCtx) {
      try {
        const silent = this.audioCtx.createBuffer(1, 1, 22050);
        const src = this.audioCtx.createBufferSource();
        src.buffer = silent;
        src.connect(this.audioCtx.destination);
        src.start(0);
        this._audioUnlocked = true;
      } catch (_) {}
    }
    return this.audioCtx;
  }

  stopActiveAudio() {
    if (this._activeBufferSource) {
      try {
        this._activeBufferSource.onended = null;
        this._activeBufferSource.stop(0);
        this._activeBufferSource.disconnect();
      } catch (_) {}
      this._activeBufferSource = null;
    }
    this.activeAudio = null;
    this.activeTimeline = null;
  }

  /** 使用已解锁的 Web Audio API 解码并播放 TTS 音频（彻底绕过手机端 new Audio().play() 自动播放限制） */
  async playAudioBufferBlob(blob, timeline = null, { onStart = null, isCancelled = null } = {}) {
    const ctx = this.ensureAudioAnalyser();
    if (!ctx) throw new Error("AudioContext 不可用");
    if (ctx.state === "suspended") {
      try { await ctx.resume(); } catch (_) {}
    }
    const rawBuf = await blob.arrayBuffer();
    if (isCancelled && isCancelled()) return;

    const decoded = await new Promise((resolve, reject) => {
      const p = ctx.decodeAudioData(rawBuf.slice(0), resolve, reject);
      if (p && typeof p.then === "function") p.then(resolve, reject);
    });
    if (isCancelled && isCancelled()) return;

    this.stopActiveAudio();
    await new Promise((resolve) => {
      const src = ctx.createBufferSource();
      src.buffer = decoded;
      src.connect(this.analyser);
      const startAt = ctx.currentTime;
      this._activeBufferSource = src;
      this.activeTimeline = timeline;
      this.activeAudio = {
        get currentTime() {
          return Math.max(0, ctx.currentTime - startAt);
        },
      };
      let finished = false;
      const done = () => {
        if (finished) return;
        finished = true;
        if (this._activeBufferSource === src) {
          this._activeBufferSource = null;
          this.activeAudio = null;
          this.activeTimeline = null;
        }
        try { src.disconnect(); } catch (_) {}
        resolve();
      };
      src.onended = done;
      try {
        src.start(0);
        if (onStart) onStart();
      } catch (_) {
        done();
      }
    });
  }

  /** 绑定正在播放的语音与它专属的毫秒级拼音口型 & 语义动作时间轴 */
  attachAudioElement(audioEl, timeline = null) {
    if (!audioEl) return;
    this.activeAudio = audioEl;
    this.activeTimeline = timeline;
    try {
      this.ensureAudioAnalyser();
      if (!this.connectedSources.has(audioEl)) {
        const src = this.audioCtx.createMediaElementSource(audioEl);
        src.connect(this.analyser);
        this.connectedSources.set(audioEl, src);
      }
    } catch (e) {
      console.warn("3D audio attach skipped:", e);
    }
  }

  _setExpr(name, value) {
    const em = this.vrm?.expressionManager;
    if (!em) return;
    em.setValue(name, Math.max(0, Math.min(1, value)));
  }

  /** 当前音频播放到的精确动作关键帧查询 */
  _sampleActiveAction() {
    if (this.manualGesture && this.manualGestureTimer > 0) {
      const localT = this.elapsed - this.activeActionStart;
      return {
        gesture: this.manualGesture,
        localT,
        duration: Math.max(0.5, this.manualGestureTimer + localT),
        face: this.manualFaceTarget || null,
      };
    }

    if (this.state === "speaking" && this.activeAudio && this.activeTimeline?.actions?.length) {
      const curT = this.activeAudio.currentTime || 0;
      let chosen = null;
      for (const act of this.activeTimeline.actions) {
        if (curT >= act.t && curT <= act.t + act.d) {
          chosen = act;
        }
      }
      if (chosen) {
        return {
          gesture: chosen.gesture,
          localT: curT - chosen.t,
          duration: chosen.d,
          face: chosen.face || null,
        };
      }
    }
    return null;
  }

  /** 钟形起承转合包络：预备起手(22%) -> 语义高点保持(48%) -> 自然回落(30%)，告别生硬定格！ */
  _gestureEnvelope(localT, duration) {
    if (duration <= 0.05) return 0;
    const p = Math.max(0, Math.min(1, localT / duration));
    if (p < 0.24) {
      const u = p / 0.24;
      return u * u * (3 - 2 * u); // smoothstep 起手
    }
    if (p < 0.72) {
      return 1.0;
    }
    const u = (1.0 - p) / 0.28;
    return u * u * (3 - 2 * u); // smoothstep 柔和收回
  }

  _updateLipSync(dt) {
    let target = { aa: 0, ih: 0, ou: 0, ee: 0, oh: 0 };
    let energy = 0;

    if (this.state === "speaking" && this.analyser && this.timeData) {
      this.analyser.getFloatTimeDomainData(this.timeData);
      let sumSq = 0;
      for (let i = 0; i < this.timeData.length; i++) sumSq += this.timeData[i] * this.timeData[i];
      const rms = Math.sqrt(sumSq / this.timeData.length);
      energy = Math.min(1, Math.max(0, (rms - 0.009) * 7.5));
    }

    // 捕捉重音突增（驱动微点头与眉毛重音轻抬！）
    const deltaE = Math.max(0, energy - this.prevVoiceEnergy);
    this.prevVoiceEnergy = energy;
    this.pitchAccent = Math.min(1, this.pitchAccent * Math.max(0, 1 - dt * 9) + deltaE * 2.6);
    this.voiceEnergy += (energy - this.voiceEnergy) * Math.min(1, dt * 16);

    let usedTimeline = false;
    if (this.state === "speaking" && this.activeAudio && this.activeTimeline?.visemes?.length) {
      const curT = this.activeAudio.currentTime || 0;
      const list = this.activeTimeline.visemes;
      // 二分/线性查找当前音素窗口
      for (let i = 0; i < list.length; i++) {
        const seg = list[i];
        if (curT >= seg.t && curT < seg.t + seg.d) {
          usedTimeline = true;
          if (seg.bmp) {
            // b / p / m 双唇闭合瞬间：快速合拢嘴唇！
            target = { aa: 0, ih: 0, ou: 0.05, ee: 0, oh: 0 };
          } else {
            // 音节内呼吸弧度 + 实时音量微调，确保停顿处绝不空张嘴
            const localP = (curT - seg.t) / Math.max(0.02, seg.d);
            const syllableContour = 0.72 + 0.28 * Math.sin(localP * Math.PI);
            const gate = energy > 0.02 ? (0.58 + 0.48 * energy) : 0.15;
            const gain = Math.min(1.05, syllableContour * gate);
            for (const k of Object.keys(target)) {
              target[k] = (seg.v[k] || 0) * gain;
            }
          }
          break;
        }
      }
    } else if (this.state === "speaking" && energy > 0.03 && this.freqData) {
      // 无时间轴时的实时频谱兜底
      this.analyser.getByteFrequencyData(this.freqData);
      const sr = this.audioCtx?.sampleRate || 48000;
      const binHz = sr / this.analyser.fftSize;
      const bandAvg = (f0, f1) => {
        const i0 = Math.max(0, Math.floor(f0 / binHz));
        const i1 = Math.min(this.freqData.length - 1, Math.ceil(f1 / binHz));
        let s = 0;
        for (let i = i0; i <= i1; i++) s += this.freqData[i];
        return s / (Math.max(1, i1 - i0 + 1) * 255);
      };
      const bL = bandAvg(220, 620), bM1 = bandAvg(620, 1350), bM2 = bandAvg(1350, 2600), bH = bandAvg(2600, 4800);
      const tot = bL + bM1 + bM2 + bH + 1e-4;
      target.aa = Math.min(0.85, energy * (0.45 + (bM1 / tot) * 1.4));
      target.oh = Math.min(0.60, energy * Math.max(0, (bL / tot) * 1.1 - (bM2 / tot) * 0.4));
      target.ou = Math.min(0.55, energy * Math.max(0, (bL / tot) * 0.9 - (bM1 / tot) * 0.5));
      target.ee = Math.min(0.58, energy * ((bM2 / tot) * 1.3));
      target.ih = Math.min(0.50, energy * ((bH / tot) * 1.4));
    }

    for (const k of Object.keys(this.visemes)) {
      // 双唇闭合要求极快收口(rate=36)，普通元音张口快(28)、收口柔(18)
      const isClosingToZero = usedTimeline && target[k] < 0.02;
      const rate = isClosingToZero ? 36 : (target[k] > this.visemes[k] ? 28 : 18);
      this.visemes[k] += (target[k] - this.visemes[k]) * Math.min(1, dt * rate);
      this._setExpr(k, this.visemes[k]);
    }
  }

  _updateMicroExpressionsAndBlink(dt, activeAct) {
    if (this.manualFaceTimer > 0) {
      this.manualFaceTimer -= dt;
      if (this.manualFaceTimer <= 0) this.manualFaceTarget = null;
    }

    // 默认自然亲切神态（睁眼微笑，不死板也不闭眼！）
    let tgt = {
      smile: this.state === "listening" ? 0.34 : 0.22,
      eyeSmile: this.state === "listening" ? 0.26 : 0.15,
      eyeWide: 0.0,
      browUp: this.state === "listening" ? 0.22 : 0.08,
      browFurrow: this.state === "thinking" ? 0.32 : 0.0,
      browSad: 0.0,
      joyPulse: 0.0,
    };

    const cue = this.manualFaceTarget || activeAct?.face;
    if (cue) {
      tgt.smile = cue.smile ?? tgt.smile;
      tgt.eyeSmile = cue.eyeSmile ?? tgt.eyeSmile;
      tgt.eyeWide = cue.eyeWide ?? 0;
      tgt.browUp = cue.browUp ?? tgt.browUp;
      tgt.browFurrow = cue.browFurrow ?? 0;
      tgt.browSad = cue.browSad ?? 0;
      // joyPulse 只在动作前 1.0 秒轻闪一下笑眼，绝不全程闭眼说话！
      const pulseDecay = activeAct ? Math.max(0, 1 - (activeAct.localT || 0) / 0.95) : 0.6;
      tgt.joyPulse = (cue.joyPulse || 0) * pulseDecay;
    }

    // 说话重音时眉毛自然微抬（真实人类讲话核心微表情！）
    if (this.state === "speaking") {
      tgt.browUp = Math.min(0.85, tgt.browUp + this.pitchAccent * 0.28);
    }

    for (const k of Object.keys(this.faceState)) {
      this.faceState[k] += (tgt[k] - this.faceState[k]) * Math.min(1, dt * 6.5);
    }

    // 张大嘴发 aa/oh 音时自动减弱嘴角上扬变形，防止模型嘴部网格撕裂
    const mouthOpen = Math.min(1, this.visemes.aa + this.visemes.oh + this.visemes.ou);
    const safeMouthSmile = this.faceState.smile * Math.max(0.18, 1 - mouthOpen * 0.78);

    // 自然眨眼
    this.blinkTimer -= dt;
    if (this.blinkTimer <= 0 && this.blinkPhase === 0) this.blinkPhase = 0.001;
    let blinkVal = 0;
    if (this.blinkPhase > 0) {
      this.blinkPhase += dt;
      const dur = 0.14;
      if (this.blinkPhase < dur) {
        blinkVal = Math.sin((this.blinkPhase / dur) * Math.PI);
      } else {
        this.blinkPhase = 0;
        if (this.doubleBlinkQueued) {
          this.doubleBlinkQueued = false;
          this.blinkTimer = 0.13;
        } else {
          this.doubleBlinkQueued = Math.random() < 0.22;
          this.blinkTimer = 2.5 + Math.random() * 3.2;
        }
      }
    }

    if (this.hasFineMorphs) {
      // 清空粗粒度整脸预设，改用细腻独立的眉/眼/嘴角通道！
      this._setExpr("happy", this.faceState.joyPulse * 0.65);
      this._setExpr("relaxed", 0);
      this._setExpr("angry", 0);
      this._setExpr("sad", 0);
      this._setExpr("surprised", 0);

      this._setFineMorph("brwFun", this.faceState.smile * 0.55);
      this._setFineMorph("brwSurprised", this.faceState.browUp);
      this._setFineMorph("brwAngry", this.faceState.browFurrow);
      this._setFineMorph("brwSorrow", this.faceState.browSad);

      this._setFineMorph("eyeFun", this.faceState.eyeSmile * (1 - blinkVal));
      this._setFineMorph("eyeSurprised", this.faceState.eyeWide * 0.65 * (1 - blinkVal));
      this._setFineMorph("eyeSpread", this.faceState.eyeWide * 0.45);
      this._setFineMorph("eyeSorrow", this.faceState.browSad * 0.65);

      this._setFineMorph("mthUp", safeMouthSmile * 0.72);
      this._setFineMorph("mthFun", safeMouthSmile * (1 - mouthOpen) * 0.38);
      this._setFineMorph("mthDown", this.faceState.browSad * 0.45 * (1 - mouthOpen));
    } else {
      // 非 VRoid 自定义 VRM 兜底走标准预设（使用 relaxed 保持睁眼微笑）
      this._setExpr("relaxed", this.faceState.smile * 0.65);
      this._setExpr("happy", this.faceState.joyPulse * 0.75);
      this._setExpr("surprised", this.faceState.eyeWide * 0.7);
      this._setExpr("sad", this.faceState.browSad * 0.7);
      this._setExpr("angry", this.faceState.browFurrow * 0.5);
    }

    this._setExpr("blink", blinkVal * Math.max(0.2, 1 - this.faceState.joyPulse));
  }

  _updateGaze(dt, activeAct) {
    this.smoothPointer.x += (this.pointer.x - this.smoothPointer.x) * Math.min(1, dt * 5);
    this.smoothPointer.y += (this.pointer.y - this.smoothPointer.y) * Math.min(1, dt * 5);

    const gazeAway = activeAct?.face?.gazeAway || (this.state === "thinking" ? 1 : 0);
    this.saccadeTimer -= dt;
    if (this.saccadeTimer <= 0) {
      this.saccadeTimer = gazeAway ? (0.9 + Math.random() * 1.1) : (1.5 + Math.random() * 2.4);
      if (gazeAway) {
        // 思考/组织语言时自然向斜上方短暂移开视线（真实人类认知眼神！）
        this.saccadeTarget.x = 0.24 + (Math.random() - 0.5) * 0.12;
        this.saccadeTarget.y = 0.16 + (Math.random() - 0.5) * 0.08;
      } else {
        // 说话强调与倾听时回到柔和眼神交流 + 极细微生理扫视
        this.saccadeTarget.x = (Math.random() - 0.5) * 0.14;
        this.saccadeTarget.y = (Math.random() - 0.5) * 0.08;
      }
    }
    this.saccadeCurrent.x += (this.saccadeTarget.x - this.saccadeCurrent.x) * Math.min(1, dt * 8);
    this.saccadeCurrent.y += (this.saccadeTarget.y - this.saccadeCurrent.y) * Math.min(1, dt * 8);

    this.lookAtObj.position.set(
      this.camera.position.x + this.smoothPointer.x * 0.50 + this.saccadeCurrent.x,
      this.camera.position.y + this.smoothPointer.y * 0.32 + this.saccadeCurrent.y,
      this.camera.position.z
    );
  }

  _updateBiomechanics(dt, activeAct) {
    if (!this.vrm) return;
    if (this.manualGestureTimer > 0) {
      this.manualGestureTimer -= dt;
      if (this.manualGestureTimer <= 0) this.manualGesture = null;
    }

    const t = this.elapsed;
    // 黄金比例无周期有机呼吸与重心微摆（永远不会看出死循环！）
    const breath = Math.sin(t * 1.48) * 0.7 + Math.sin(t * 2.39) * 0.3;
    const organicX = Math.sin(t * 0.61) * 0.6 + Math.sin(t * 0.99) * 0.4;
    const organicZ = Math.sin(t * 0.47) * 0.65 + Math.sin(t * 1.13) * 0.35;

    let hips = [0, 0.02 * organicX, 0.012 * organicZ];
    let spine = [0.016 * breath, 0.022 * organicX, 0.016 * organicZ];
    let chest = [0.022 * breath, 0.025 * Math.sin(t * 0.41), -0.01 * organicZ];

    // 头部基础微动 + 语音重音自然轻点（pitchAccent 越大，在重读字上越自然轻点下巴）
    const speechNod = this.state === "speaking" ? (-this.pitchAccent * 0.095 - this.voiceEnergy * 0.035 * Math.sin(t * 5.4)) : 0;
    let headPitch = 0.018 * Math.sin(t * 1.05) + this.smoothPointer.y * 0.08 + speechNod;
    let headYaw = 0.045 * organicX + this.smoothPointer.x * 0.16;
    let headRoll = 0.03 * organicZ;

    // 默认自然放松双臂
    const basePose = {
      lSho: [0, 0, 0.04],
      rSho: [0, 0, -0.04],
      lUp: [0.06, 0.04, 1.23 - 0.018 * breath],
      rUp: [0.06, -0.04, -1.23 + 0.018 * breath],
      lLow: [0.06, -0.22, 0.08],
      rLow: [0.06, 0.22, -0.08],
      lHand: [0, 0, 0.05],
      rHand: [0, 0, -0.05],
      curlL: 0.28,
      curlR: 0.28,
      idxExtR: 0.0,
    };

    const gPose = { ...basePose };
    let env = 0;

    if (activeAct) {
      const g = activeAct.gesture;
      const gt = activeAct.localT || 0;
      env = this._gestureEnvelope(gt, activeAct.duration || 2.0);
      const tiltCue = activeAct.face?.tilt || 0;
      const nodCue = activeAct.face?.nod || 0;

      headRoll += tiltCue * env;
      // 在关键语义词起手处做一个温和肯定的点头拍（而非一直机械猛点）
      headPitch += -nodCue * 0.14 * Math.sin(Math.min(1, gt / 0.55) * Math.PI) * env;

      const beat = Math.sin(gt * 4.8) * (0.05 + this.voiceEnergy * 0.10);

      if (g === "wave") {
        const w = Math.sin(gt * 8.2);
        gPose.rSho = [0, 0, 0.10];
        gPose.rUp = [1.28, -0.18, -0.45];
        gPose.rLow = [0.0, 1.55 + w * 0.26, -0.12];
        gPose.rHand = [0, 0, w * 0.18];
        gPose.curlR = 0.05;
        spine[2] -= 0.025 * env;
      } else if (g === "self_chest") {
        // 说“我 / 我帮你 / 我觉得”时：右手轻抬贴向胸口锁骨下方，温暖真诚
        gPose.rSho = [0, 0, 0.05];
        gPose.rUp = [0.62, -0.32, -1.02];
        gPose.rLow = [0.22, 1.78, -0.22];
        gPose.rHand = [0.18, -0.35, -0.15];
        gPose.curlR = 0.22;
        spine[0] -= 0.025 * env;
      } else if (g === "ask_user") {
        // 说“你觉得呢 / 你想看哪部分”时：身体微前倾、偏头好奇注视、右手掌心轻摊向镜头邀请对方
        gPose.rSho = [0, 0, 0.03];
        gPose.rUp = [0.56 + beat * 0.5, -0.08, -0.98];
        gPose.rLow = [0.06, 1.18 + beat * 0.4, -0.22];
        gPose.rHand = [0.22, -0.38, -0.20];
        gPose.curlR = 0.10;
        spine[0] -= 0.04 * env;
      } else if (g === "point_list") {
        // 说“第一 / 首先”时：右手抬至胸前略竖食指点明条理
        gPose.rSho = [0, 0, 0.06];
        gPose.rUp = [0.68 + beat * 0.6, -0.16, -0.90];
        gPose.rLow = [0.12, 1.52 + beat * 0.5, -0.15];
        gPose.rHand = [0.10, -0.12, -0.10];
        gPose.curlR = 0.48;
        gPose.idxExtR = 0.42; // 单独舒展食指！
      } else if (g === "explain_open") {
        // 单手自然讲解展开
        gPose.rSho = [0, 0, 0.04];
        gPose.rUp = [0.54 + beat, -0.15, -0.98];
        gPose.rLow = [0.08, 1.32 + beat * 0.6, -0.16];
        gPose.rHand = [0.14, -0.26, -0.12 + beat * 0.8];
        gPose.curlR = 0.15;
        gPose.lUp = [0.18, 0.06, 1.18];
        gPose.lLow = [0.08, -0.45, 0.10];
      } else if (g === "explain_both") {
        // 双手对称展开梳理架构/总结核心
        gPose.lSho = [0, 0, -0.04];
        gPose.rSho = [0, 0, 0.04];
        gPose.lUp = [0.50 + beat * 0.8, 0.15, 0.99 - beat * 0.3];
        gPose.rUp = [0.50 + beat * 0.8, -0.15, -0.99 + beat * 0.3];
        gPose.lLow = [0.10, -1.26 - beat * 0.4, 0.18];
        gPose.rLow = [0.10, 1.26 + beat * 0.4, -0.18];
        gPose.lHand = [0.12, 0.26, 0.14];
        gPose.rHand = [0.12, -0.26, -0.14];
        gPose.curlL = 0.13;
        gPose.curlR = 0.13;
      } else if (g === "weigh_contrast") {
        // 说“但是 / 对比 / 一方面另一方面”时：双手高低交替权衡手势
        const alt = Math.sin(gt * 3.8) * 0.16;
        gPose.lUp = [0.48 + alt, 0.14, 1.02];
        gPose.rUp = [0.48 - alt, -0.14, -1.02];
        gPose.lLow = [0.10, -1.25 - alt * 0.8, 0.18];
        gPose.rLow = [0.10, 1.25 + alt * 0.8, -0.18];
        gPose.lHand = [0.15, 0.30, 0.12];
        gPose.rHand = [0.15, -0.30, -0.12];
        gPose.curlL = 0.14;
        gPose.curlR = 0.14;
      } else if (g === "beat_emphasis") {
        // 遇到关键数字/核心结论：干脆利落的前臂重音下顿拍
        const stroke = Math.sin(Math.min(1, gt / 0.45) * Math.PI);
        gPose.rUp = [0.48 + stroke * 0.16, -0.14, -1.02];
        gPose.rLow = [0.08, 1.28 + stroke * 0.25, -0.14];
        gPose.rHand = [0.12, -0.20, -0.22 * stroke];
        gPose.curlR = 0.18;
      } else if (g === "think_chin") {
        gPose.rSho = [0, 0, 0.07];
        gPose.rUp = [0.76, -0.28, -0.88];
        gPose.rLow = [0.18, 1.92, -0.18];
        gPose.rHand = [0.22, -0.15, -0.12];
        gPose.curlR = 0.40;
        gPose.lUp = [0.32, 0.18, 1.10];
        gPose.lLow = [0.08, -1.05, 0.12];
        headPitch += 0.06 * env;
        headYaw += 0.08 * env;
      } else if (g === "empathy_soft") {
        // 关切/抱歉：左手与右手轻柔合拢在胸前下方，微低头柔和注视
        gPose.lUp = [0.44, 0.24, 1.06];
        gPose.rUp = [0.44, -0.24, -1.06];
        gPose.lLow = [0.14, -1.45, 0.16];
        gPose.rLow = [0.14, 1.45, -0.16];
        gPose.curlL = 0.22;
        gPose.curlR = 0.22;
        headPitch -= 0.05 * env;
      } else if (g === "surprise_soft") {
        gPose.lSho = [0, 0, -0.09];
        gPose.rSho = [0, 0, 0.09];
        gPose.lUp = [0.52, 0.10, 0.90];
        gPose.rUp = [0.52, -0.10, -0.90];
        gPose.lLow = [0.14, -1.48, 0.22];
        gPose.rLow = [0.14, 1.48, -0.22];
        gPose.curlL = 0.07;
        gPose.curlR = 0.07;
        spine[0] += 0.035 * env;
      } else if (g === "nod_agree") {
        spine[0] -= 0.035 * env;
        gPose.lUp = [0.18, 0.06, 1.18];
        gPose.rUp = [0.18, -0.06, -1.18];
        gPose.lLow = [0.08, -0.45, 0.10];
        gPose.rLow = [0.08, 0.45, -0.10];
      }
    }

    const mix = (a, b) => [
      a[0] + (b[0] - a[0]) * env,
      a[1] + (b[1] - a[1]) * env,
      a[2] + (b[2] - a[2]) * env,
    ];

    const applySpring = (boneName, targetArr) => {
      const sp = this.springs[boneName];
      const bone = this._getBone(boneName);
      if (!sp || !bone) return;
      sp.step(targetArr[0], targetArr[1], targetArr[2], dt);
      bone.rotation.set(sp.x, sp.y, sp.z);
    };

    applySpring("hips", hips);
    applySpring("spine", spine);
    applySpring("chest", chest);
    applySpring("neck", [headPitch * 0.42, headYaw * 0.42, headRoll * 0.35]);
    applySpring("head", [headPitch * 0.58, headYaw * 0.58, headRoll * 0.65]);

    applySpring("leftShoulder", mix(basePose.lSho, gPose.lSho));
    applySpring("rightShoulder", mix(basePose.rSho, gPose.rSho));
    applySpring("leftUpperArm", mix(basePose.lUp, gPose.lUp));
    applySpring("rightUpperArm", mix(basePose.rUp, gPose.rUp));
    applySpring("leftLowerArm", mix(basePose.lLow, gPose.lLow));
    applySpring("rightLowerArm", mix(basePose.rLow, gPose.rLow));
    applySpring("leftHand", mix(basePose.lHand, gPose.lHand));
    applySpring("rightHand", mix(basePose.rHand, gPose.rHand));

    // 五指渐进自然扇形弯曲（食指轻、小指深，支持要点伸食指！）
    const tgtCurlL = basePose.curlL + (gPose.curlL - basePose.curlL) * env;
    const tgtCurlR = basePose.curlR + (gPose.curlR - basePose.curlR) * env;
    const tgtIdxExt = gPose.idxExtR * env;
    this.fingerCurlL += (tgtCurlL - this.fingerCurlL) * Math.min(1, dt * 10);
    this.fingerCurlR += (tgtCurlR - this.fingerCurlR) * Math.min(1, dt * 10);
    this.indexExtendR += (tgtIdxExt - this.indexExtendR) * Math.min(1, dt * 12);

    for (const grp of FINGER_GROUPS) {
      for (const joint of ["Proximal", "Intermediate", "Distal"]) {
        const bl = this._getBone(`left${grp.prefix}${joint}`);
        const br = this._getBone(`right${grp.prefix}${joint}`);
        if (grp.prefix === "Thumb") {
          if (bl) bl.rotation.y = -this.fingerCurlL * 0.6;
          if (br) br.rotation.y = this.fingerCurlR * 0.6;
        } else {
          if (bl) bl.rotation.z = this.fingerCurlL * grp.ratio;
          let cr = this.fingerCurlR * grp.ratio;
          if (grp.prefix === "Index") cr = Math.max(0.03, cr - this.indexExtendR);
          if (br) br.rotation.z = -cr;
        }
      }
    }
  }

  _animate() {
    requestAnimationFrame(() => this._animate());
    const dt = Math.min(0.05, this.clock.getDelta());
    this.elapsed += dt;

    if (this.vrm) {
      const activeAct = this._sampleActiveAction();
      this._updateLipSync(dt);
      this._updateMicroExpressionsAndBlink(dt, activeAct);
      this._updateGaze(dt, activeAct);
      this._updateBiomechanics(dt, activeAct);
      this.vrm.update(dt);
    }
    if (this.bokehPoints) {
      this.bokehPoints.rotation.y = this.elapsed * 0.025;
    }
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }
}
