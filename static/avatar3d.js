/**
 * Three.js + @pixiv/three-vrm 高自然度 60fps 语音驱动 3D 虚拟人像引擎 (v6 · 单人 + 多角色 3D 群聊舞台)
 *
 * 核心能力：
 * 1. 底层细分面部微表情驱动（VRoid Fcl_BRW_* 眉毛 / Fcl_EYE_* 笑眼微睁 / Fcl_MTH_Up 嘴角微笑 独立解耦控制）
 * 2. 毫秒级中文拼音口型时间轴（b/p/m 真实双唇闭合、复韵母平滑过渡 + 实时音频能量门控）
 * 3. 词级动作编排时间轴 + 二阶临界阻尼弹簧骨骼动力学 + 五指渐进自然弯曲
 * 4. 多角色 3D 同台群聊舞台（AvatarActor 独立实例）：
 *    - 同屏 2~4 位 3D VRM 角色半圆弧自然站位，独立呼吸相位与眨眼节奏
 *    - 全员同步倾听用户，各自触发不同表情与手势反应
 *    - 某位角色发言时，其余角色自动转头注视发言者、点头或做微表情，发言者也会在对话对象与用户镜头间自然切换目光
 *    - 实时 3D→2D 屏幕坐标投影，驱动角色头顶/胸前悬浮名牌与内心反应气泡
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
    const d = 2 * w * 0.92;
    const ax = -k * (this.x - tx) - d * this.vx;
    const ay = -k * (this.y - ty) - d * this.vy;
    const az = -k * (this.z - tz) - d * this.vz;
    this.vx += ax * dt; this.vy += ay * dt; this.vz += az * dt;
    this.x += this.vx * dt; this.y += this.vy * dt; this.z += this.vz * dt;
  }
}

const FINGER_GROUPS = [
  { prefix: "Thumb",  ratio: 0.45 },
  { prefix: "Index",  ratio: 0.65 },
  { prefix: "Middle", ratio: 0.88 },
  { prefix: "Ring",   ratio: 1.08 },
  { prefix: "Little", ratio: 1.25 },
];

const EMOTION_MAP = {
  neutral:   { smile: 0.20, eyeSmile: 0.14, browUp: 0.08, browFurrow: 0, browSad: 0, eyeWide: 0, joyPulse: 0 },
  happy:     { smile: 0.72, eyeSmile: 0.62, browUp: 0.30, browFurrow: 0, browSad: 0, eyeWide: 0, joyPulse: 0.38 },
  relaxed:   { smile: 0.45, eyeSmile: 0.36, browUp: 0.18, browFurrow: 0, browSad: 0, eyeWide: 0, joyPulse: 0 },
  surprised: { smile: 0.16, eyeSmile: 0.0,  browUp: 0.82, browFurrow: 0, browSad: 0, eyeWide: 0.65, joyPulse: 0 },
  sad:       { smile: 0.0,  eyeSmile: 0.0,  browUp: 0.0,  browFurrow: 0, browSad: 0.62, eyeWide: 0, joyPulse: 0 },
  focused:   { smile: 0.10, eyeSmile: 0.05, browUp: 0.0,  browFurrow: 0.38, browSad: 0, eyeWide: 0.08, joyPulse: 0 },
};

/** 单个 3D 虚拟人像角色控制器（独立骨骼弹簧、微表情、口型、视线、呼吸相位） */
class AvatarActor {
  constructor(id, { name = "角色", role = "", color = "#60a5fa", sourceUrl = "" } = {}) {
    this.id = id;
    this.name = name;
    this.role = role;
    this.color = color;
    this.sourceUrl = sourceUrl;

    this.vrm = null;
    this.faceMeshes = [];
    this.hasFineMorphs = false;
    this.state = "idle"; // "idle" | "listening" | "thinking" | "speaking"

    // 错开每个角色的呼吸与微动相位，杜绝多人同频机械感
    this.phaseOffset = Math.random() * 25.0;
    this.basePos = new THREE.Vector3(0, 0, 0);
    this.baseRotY = 0;

    // 脚下柔和聚光环（群聊模式下高亮当前发言者）
    this.ringMesh = null;
    this.ringGlow = 0;

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

    // 手势状态
    this.manualGesture = null;
    this.manualGestureTimer = 0;
    this.activeActionStart = 0;

    // 独立眨眼节律
    this.blinkTimer = 1.4 + Math.random() * 2.8;
    this.blinkPhase = 0;
    this.doubleBlinkQueued = false;

    // 独立视线与扫视
    this.saccadeTarget = { x: 0, y: 0 };
    this.saccadeCurrent = { x: 0, y: 0 };
    this.saccadeTimer = 1.2 + Math.random() * 1.4;
    this.lookAtObj = new THREE.Object3D();
    this.eyeWorldPos = new THREE.Vector3(0, 1.50, 0);
    this.headWorldPos = new THREE.Vector3(0, 1.48, 0);
    this.socialYaw = 0;
    this.socialPitch = 0;

    // 独立口型与语音重音
    this.visemes = { aa: 0, ih: 0, ou: 0, ee: 0, oh: 0 };
    this.voiceEnergy = 0;
    this.prevVoiceEnergy = 0;
    this.pitchAccent = 0;

    // 关节二阶弹簧实例
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
    this.indexExtendR = 0.0;
  }

  attachVRM(vrm, scene, showRing = false) {
    this.dispose(scene);
    this.vrm = vrm;
    this.vrm.scene.userData.actorId = this.id;
    this.vrm.scene.traverse((obj) => {
      obj.frustumCulled = false;
      obj.userData.actorId = this.id;
    });
    this._discoverFineFaceMorphs(vrm.scene);

    // 初始垂臂姿态
    this.springs.leftUpperArm.z = 1.22;
    this.springs.rightUpperArm.z = -1.22;
    this.springs.leftLowerArm.y = -0.22;
    this.springs.rightLowerArm.y = 0.22;

    this.vrm.scene.position.copy(this.basePos);
    this.vrm.scene.rotation.y = this.baseRotY;
    scene.add(vrm.scene);
    scene.add(this.lookAtObj);
    if (vrm.lookAt) vrm.lookAt.target = this.lookAtObj;

    this.setRingVisible(showRing, scene);
  }

  setRingVisible(visible, scene) {
    if (visible && !this.ringMesh) {
      const geo = new THREE.RingGeometry(0.24, 0.34, 40);
      const mat = new THREE.MeshBasicMaterial({
        color: new THREE.Color(this.color || "#60a5fa"),
        transparent: true,
        opacity: 0.16,
        side: THREE.DoubleSide,
        depthWrite: false,
      });
      this.ringMesh = new THREE.Mesh(geo, mat);
      this.ringMesh.rotation.x = -Math.PI / 2;
      this.ringMesh.position.set(this.basePos.x, 0.015, this.basePos.z);
      scene.add(this.ringMesh);
    } else if (!visible && this.ringMesh) {
      scene.remove(this.ringMesh);
      this.ringMesh.geometry.dispose();
      this.ringMesh.material.dispose();
      this.ringMesh = null;
    } else if (visible && this.ringMesh) {
      this.ringMesh.material.color.set(this.color || "#60a5fa");
      this.ringMesh.position.set(this.basePos.x, 0.015, this.basePos.z);
    }
  }

  setPlacement(x, y, z, rotY) {
    this.basePos.set(x, y, z);
    this.baseRotY = rotY;
    if (this.vrm) {
      this.vrm.scene.position.copy(this.basePos);
      this.vrm.scene.rotation.y = this.baseRotY;
    }
    if (this.ringMesh) {
      this.ringMesh.position.set(x, 0.015, z);
    }
  }

  dispose(scene) {
    if (this.vrm) {
      scene.remove(this.vrm.scene);
      VRMUtils.deepDispose(this.vrm.scene);
      this.vrm = null;
    }
    if (this.lookAtObj.parent) {
      this.lookAtObj.parent.remove(this.lookAtObj);
    }
    if (this.ringMesh) {
      scene.remove(this.ringMesh);
      this.ringMesh.geometry.dispose();
      this.ringMesh.material.dispose();
      this.ringMesh = null;
    }
    this.faceMeshes = [];
    this.hasFineMorphs = false;
  }

  _discoverFineFaceMorphs(root) {
    this.faceMeshes = [];
    this.hasFineMorphs = false;
    root.traverse((obj) => {
      if (obj.isMesh && obj.morphTargetDictionary && obj.morphTargetInfluences) {
        const keys = Object.keys(obj.morphTargetDictionary);
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

  _getBone(name) {
    return this.vrm?.humanoid?.getNormalizedBoneNode(name) || null;
  }

  _setExpr(name, value) {
    const em = this.vrm?.expressionManager;
    if (!em) return;
    em.setValue(name, Math.max(0, Math.min(1, value)));
  }

  setState(nextState, elapsed) {
    const prev = this.state;
    this.state = nextState || "idle";
    if (this.state === "thinking" && prev !== "thinking") {
      this.triggerGesture("think_chin", 3.6, elapsed);
      this.setEmotion("focused", 3.6);
    } else if (this.state === "listening" && prev !== "listening" && prev !== "speaking") {
      this.triggerGesture("nod_agree", 1.5, elapsed);
      this.setEmotion("relaxed", 2.6);
    }
  }

  setEmotion(name, holdSec = 3.2) {
    if (EMOTION_MAP[name]) {
      this.manualFaceTarget = EMOTION_MAP[name];
      this.manualFaceTimer = holdSec;
    }
  }

  triggerGesture(name, durationSec = 2.6, elapsed = 0) {
    const alias = { think: "think_chin", surprise: "surprise_soft", explain_one: "explain_open" }[name] || name;
    this.manualGesture = alias;
    this.manualGestureTimer = durationSec;
    this.activeActionStart = elapsed;
  }

  _sampleActiveAction(elapsed, isActiveSpeaker, activeAudio, activeTimeline) {
    if (this.manualGesture && this.manualGestureTimer > 0) {
      const localT = elapsed - this.activeActionStart;
      return {
        gesture: this.manualGesture,
        localT,
        duration: Math.max(0.5, this.manualGestureTimer + localT),
        face: this.manualFaceTarget || null,
      };
    }

    if (isActiveSpeaker && this.state === "speaking" && activeAudio && activeTimeline?.actions?.length) {
      const curT = activeAudio.currentTime || 0;
      let chosen = null;
      for (const act of activeTimeline.actions) {
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

  _gestureEnvelope(localT, duration) {
    if (duration <= 0.05) return 0;
    const p = Math.max(0, Math.min(1, localT / duration));
    if (p < 0.24) {
      const u = p / 0.24;
      return u * u * (3 - 2 * u);
    }
    if (p < 0.72) return 1.0;
    const u = (1.0 - p) / 0.28;
    return u * u * (3 - 2 * u);
  }

  _updateLipSync(dt, isActiveSpeaker, engine) {
    let target = { aa: 0, ih: 0, ou: 0, ee: 0, oh: 0 };
    let energy = 0;

    if (isActiveSpeaker && this.state === "speaking" && engine.analyser && engine.timeData) {
      engine.analyser.getFloatTimeDomainData(engine.timeData);
      let sumSq = 0;
      for (let i = 0; i < engine.timeData.length; i++) sumSq += engine.timeData[i] * engine.timeData[i];
      const rms = Math.sqrt(sumSq / engine.timeData.length);
      energy = Math.min(1, Math.max(0, (rms - 0.009) * 7.5));
    }

    const deltaE = Math.max(0, energy - this.prevVoiceEnergy);
    this.prevVoiceEnergy = energy;
    this.pitchAccent = Math.min(1, this.pitchAccent * Math.max(0, 1 - dt * 9) + deltaE * 2.6);
    this.voiceEnergy += (energy - this.voiceEnergy) * Math.min(1, dt * 16);

    let usedTimeline = false;
    if (isActiveSpeaker && this.state === "speaking" && engine.activeAudio && engine.activeTimeline?.visemes?.length) {
      const curT = engine.activeAudio.currentTime || 0;
      const list = engine.activeTimeline.visemes;
      for (let i = 0; i < list.length; i++) {
        const seg = list[i];
        if (curT >= seg.t && curT < seg.t + seg.d) {
          usedTimeline = true;
          if (seg.bmp) {
            target = { aa: 0, ih: 0, ou: 0.05, ee: 0, oh: 0 };
          } else {
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
    } else if (isActiveSpeaker && this.state === "speaking" && energy > 0.03 && engine.freqData) {
      engine.analyser.getByteFrequencyData(engine.freqData);
      const sr = engine.audioCtx?.sampleRate || 48000;
      const binHz = sr / engine.analyser.fftSize;
      const bandAvg = (f0, f1) => {
        const i0 = Math.max(0, Math.floor(f0 / binHz));
        const i1 = Math.min(engine.freqData.length - 1, Math.ceil(f1 / binHz));
        let s = 0;
        for (let i = i0; i <= i1; i++) s += engine.freqData[i];
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
      const isClosingToZero = usedTimeline && target[k] < 0.02;
      const rate = isClosingToZero ? 36 : (target[k] > this.visemes[k] ? 28 : 18);
      this.visemes[k] += (target[k] - this.visemes[k]) * Math.min(1, dt * rate);
      this._setExpr(k, this.visemes[k]);
    }
  }

  _updateMicroExpressionsAndBlink(dt, activeAct, isActiveSpeaker) {
    if (this.manualFaceTimer > 0) {
      this.manualFaceTimer -= dt;
      if (this.manualFaceTimer <= 0) this.manualFaceTarget = null;
    }

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
      const pulseDecay = activeAct ? Math.max(0, 1 - (activeAct.localT || 0) / 0.95) : 0.6;
      tgt.joyPulse = (cue.joyPulse || 0) * pulseDecay;
    }

    if (isActiveSpeaker && this.state === "speaking") {
      tgt.browUp = Math.min(0.85, tgt.browUp + this.pitchAccent * 0.28);
    }

    for (const k of Object.keys(this.faceState)) {
      this.faceState[k] += (tgt[k] - this.faceState[k]) * Math.min(1, dt * 6.5);
    }

    const mouthOpen = Math.min(1, this.visemes.aa + this.visemes.oh + this.visemes.ou);
    const safeMouthSmile = this.faceState.smile * Math.max(0.18, 1 - mouthOpen * 0.78);

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
          this.blinkTimer = 2.4 + Math.random() * 3.4;
        }
      }
    }

    if (this.hasFineMorphs) {
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
      this._setExpr("relaxed", this.faceState.smile * 0.65);
      this._setExpr("happy", this.faceState.joyPulse * 0.75);
      this._setExpr("surprised", this.faceState.eyeWide * 0.7);
      this._setExpr("sad", this.faceState.browSad * 0.7);
      this._setExpr("angry", this.faceState.browFurrow * 0.5);
    }

    this._setExpr("blink", blinkVal * Math.max(0.2, 1 - this.faceState.joyPulse));
  }

  _updateGaze(dt, activeAct, engine, lookTargetActor) {
    // 更新眼睛与头部世界坐标
    if (this.vrm?.lookAt) {
      this.vrm.lookAt.getLookAtWorldPosition(this.eyeWorldPos);
    }
    const head = this._getBone("head");
    if (head) {
      head.getWorldPosition(this.headWorldPos);
      if (!this.vrm?.lookAt) {
        this.eyeWorldPos.copy(this.headWorldPos);
        this.eyeWorldPos.y += 0.06;
      }
    }

    const w = engine.cameraGaze.weight;
    const gazeAway = (activeAct?.face?.gazeAway || (this.state === "thinking" ? 1 : 0)) * (1 - 0.55 * w);
    this.saccadeTimer -= dt;
    if (this.saccadeTimer <= 0) {
      this.saccadeTimer = gazeAway > 0.35 ? (0.9 + Math.random() * 1.1) : (1.5 + Math.random() * 2.4);
      if (gazeAway > 0.35) {
        this.saccadeTarget.x = (0.24 + (Math.random() - 0.5) * 0.12) * gazeAway;
        this.saccadeTarget.y = (0.16 + (Math.random() - 0.5) * 0.08) * gazeAway;
      } else {
        this.saccadeTarget.x = (Math.random() - 0.5) * 0.14;
        this.saccadeTarget.y = (Math.random() - 0.5) * 0.08;
      }
    }
    this.saccadeCurrent.x += (this.saccadeTarget.x - this.saccadeCurrent.x) * Math.min(1, dt * 8);
    this.saccadeCurrent.y += (this.saccadeTarget.y - this.saccadeCurrent.y) * Math.min(1, dt * 8);

    // 群聊社交视线：当需要看向同台另一位角色（如听众看发言者，或发言者回应某群友）
    let targetSocialYaw = 0;
    let targetSocialPitch = 0;
    if (lookTargetActor && lookTargetActor !== this && lookTargetActor.vrm) {
      const dx = lookTargetActor.eyeWorldPos.x - this.eyeWorldPos.x;
      const dz = lookTargetActor.eyeWorldPos.z - this.eyeWorldPos.z;
      const dy = lookTargetActor.eyeWorldPos.y - this.eyeWorldPos.y;
      const worldAngle = Math.atan2(dx, dz);
      let relYaw = worldAngle - this.baseRotY;
      while (relYaw > Math.PI) relYaw -= Math.PI * 2;
      while (relYaw < -Math.PI) relYaw += Math.PI * 2;
      // 柔和限制转头幅度，既一眼看出在看对方，又不至于背对镜头
      targetSocialYaw = Math.max(-0.52, Math.min(0.52, relYaw * 0.62));
      targetSocialPitch = Math.max(-0.15, Math.min(0.15, dy * 0.35));

      this.lookAtObj.position.lerp(
        new THREE.Vector3(
          lookTargetActor.eyeWorldPos.x + this.saccadeCurrent.x * 0.08,
          lookTargetActor.eyeWorldPos.y + this.saccadeCurrent.y * 0.06,
          lookTargetActor.eyeWorldPos.z
        ),
        Math.min(1, dt * 8)
      );
    } else {
      const gx = engine.smoothPointer.x * (1 - w) + engine.cameraGaze.smoothX * w;
      const gy = engine.smoothPointer.y * (1 - w) + engine.cameraGaze.smoothY * w;
      const calX = engine.gazeCalibration.offsetX;
      const calY = engine.gazeCalibration.offsetY;
      const spanX = 0.55 + 0.85 * w;
      const spanY = 0.48 + 0.72 * w;
      const calSpanX = 1.65;
      const calSpanY = 2.15;
      const sacScale = 1 - 0.72 * w;
      const depthPull = w * (engine.cameraGaze.smoothProx - 0.35) * 0.25;

      const manualCamLift = engine.camera.position.y - engine.defaultCamPos.y;
      const baseAnchorY = this.eyeWorldPos.y + 0.05 + manualCamLift * 0.65;

      this.lookAtObj.position
        .set(engine.camera.position.x, baseAnchorY, engine.camera.position.z)
        .addScaledVector(engine._camRight, gx * spanX + calX * calSpanX + this.saccadeCurrent.x * sacScale)
        .addScaledVector(engine._camUp, gy * spanY + calY * calSpanY + this.saccadeCurrent.y * sacScale)
        .addScaledVector(engine._camForward, -depthPull);
    }

    this.socialYaw += (targetSocialYaw - this.socialYaw) * Math.min(1, dt * 5.0);
    this.socialPitch += (targetSocialPitch - this.socialPitch) * Math.min(1, dt * 5.0);
  }

  _updateBiomechanics(dt, activeAct, isActiveSpeaker, engine) {
    if (!this.vrm) return;
    if (this.manualGestureTimer > 0) {
      this.manualGestureTimer -= dt;
      if (this.manualGestureTimer <= 0) this.manualGesture = null;
    }

    const t = engine.elapsed + this.phaseOffset;
    const breath = Math.sin(t * 1.48) * 0.7 + Math.sin(t * 2.39) * 0.3;
    const organicX = Math.sin(t * 0.61) * 0.6 + Math.sin(t * 0.99) * 0.4;
    const organicZ = Math.sin(t * 0.47) * 0.65 + Math.sin(t * 1.13) * 0.35;

    const w = engine.cameraGaze.weight;
    const vrmAxisSign = this.vrm?.meta?.metaVersion === "1" ? -1 : 1;
    const camX = engine.cameraGaze.smoothX;
    const camY = engine.cameraGaze.smoothY;
    const camRoll = engine.cameraGaze.smoothRoll;
    const idleDriftScale = 1 - 0.65 * w;

    const socialBlend = Math.min(1, Math.abs(this.socialYaw) * 2.2);
    const camTrackWeight = 1 - socialBlend * 0.85;

    let hips = [0, 0.02 * organicX * idleDriftScale, 0.012 * organicZ];
    let spine = [
      0.016 * breath,
      0.022 * organicX * idleDriftScale + camX * 0.055 * w * camTrackWeight + this.socialYaw * 0.14,
      0.016 * organicZ,
    ];
    let chest = [
      0.022 * breath,
      0.025 * Math.sin(t * 0.41) * idleDriftScale + camX * 0.105 * w * camTrackWeight + this.socialYaw * 0.24,
      -0.01 * organicZ,
    ];

    const calX = engine.gazeCalibration.offsetX;
    const calY = engine.gazeCalibration.offsetY;
    const speechNod = (isActiveSpeaker && this.state === "speaking")
      ? (-this.pitchAccent * 0.095 - this.voiceEnergy * 0.035 * Math.sin(t * 5.4))
      : 0;
    const trackYaw = (engine.smoothPointer.x * 0.16 * (1 - w) + camX * 0.42 * w + calX * 0.20) * camTrackWeight;
    const trackPitch = (engine.smoothPointer.y * 0.08 * (1 - w) + camY * 0.27 * w + calY * 0.16) * camTrackWeight;
    const trackRoll = camRoll * 0.12 * w * camTrackWeight;

    let headPitch = 0.018 * Math.sin(t * 1.05) + trackPitch + speechNod + this.socialPitch;
    let headYaw = 0.045 * organicX * idleDriftScale + trackYaw + this.socialYaw * 0.72;
    let headRoll = 0.03 * organicZ * idleDriftScale + trackRoll;

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
      headPitch += -nodCue * 0.14 * Math.sin(Math.min(1, gt / 0.55) * Math.PI) * env;

      const beat = Math.sin(gt * 4.8) * (0.05 + this.voiceEnergy * 0.10);

      if (g === "wave") {
        const wv = Math.sin(gt * 8.2);
        gPose.rSho = [0, 0, 0.10];
        gPose.rUp = [1.28, -0.18, -0.45];
        gPose.rLow = [0.0, 1.55 + wv * 0.26, -0.12];
        gPose.rHand = [0, 0, wv * 0.18];
        gPose.curlR = 0.05;
        spine[2] -= 0.025 * env;
      } else if (g === "self_chest") {
        gPose.rSho = [0, 0, 0.05];
        gPose.rUp = [0.62, -0.32, -1.02];
        gPose.rLow = [0.22, 1.78, -0.22];
        gPose.rHand = [0.18, -0.35, -0.15];
        gPose.curlR = 0.22;
        spine[0] -= 0.025 * env;
      } else if (g === "ask_user") {
        gPose.rSho = [0, 0, 0.03];
        gPose.rUp = [0.56 + beat * 0.5, -0.08, -0.98];
        gPose.rLow = [0.06, 1.18 + beat * 0.4, -0.22];
        gPose.rHand = [0.22, -0.38, -0.20];
        gPose.curlR = 0.10;
        spine[0] -= 0.04 * env;
      } else if (g === "point_list") {
        gPose.rSho = [0, 0, 0.06];
        gPose.rUp = [0.68 + beat * 0.6, -0.16, -0.90];
        gPose.rLow = [0.12, 1.52 + beat * 0.5, -0.15];
        gPose.rHand = [0.10, -0.12, -0.10];
        gPose.curlR = 0.48;
        gPose.idxExtR = 0.42;
      } else if (g === "explain_open") {
        gPose.rSho = [0, 0, 0.04];
        gPose.rUp = [0.54 + beat, -0.15, -0.98];
        gPose.rLow = [0.08, 1.32 + beat * 0.6, -0.16];
        gPose.rHand = [0.14, -0.26, -0.12 + beat * 0.8];
        gPose.curlR = 0.15;
        gPose.lUp = [0.18, 0.06, 1.18];
        gPose.lLow = [0.08, -0.45, 0.10];
      } else if (g === "explain_both") {
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
      bone.rotation.set(sp.x * vrmAxisSign, sp.y, sp.z * vrmAxisSign);
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
          if (bl) bl.rotation.z = this.fingerCurlL * grp.ratio * vrmAxisSign;
          let cr = this.fingerCurlR * grp.ratio;
          if (grp.prefix === "Index") cr = Math.max(0.03, cr - this.indexExtendR);
          if (br) br.rotation.z = -cr * vrmAxisSign;
        }
      }
    }

    // 脚下聚光环动态脉冲
    if (this.ringMesh) {
      const targetGlow = (isActiveSpeaker && this.state === "speaking") ? 1.0 : 0.0;
      this.ringGlow += (targetGlow - this.ringGlow) * Math.min(1, dt * 7);
      const pulse = 1.0 + Math.sin(engine.elapsed * 5.0) * 0.08 * this.ringGlow + this.voiceEnergy * 0.14 * this.ringGlow;
      this.ringMesh.scale.set(pulse, pulse, 1);
      this.ringMesh.material.opacity = 0.14 + 0.56 * this.ringGlow;
    }
  }

  update(dt, engine, isActiveSpeaker, lookTargetActor) {
    if (!this.vrm) return;
    const activeAct = this._sampleActiveAction(engine.elapsed, isActiveSpeaker, engine.activeAudio, engine.activeTimeline);
    this._updateLipSync(dt, isActiveSpeaker, engine);
    this._updateMicroExpressionsAndBlink(dt, activeAct, isActiveSpeaker);
    this._updateGaze(dt, activeAct, engine, lookTargetActor);
    this._updateBiomechanics(dt, activeAct, isActiveSpeaker, engine);
    this.vrm.update(dt);
  }
}

export class Avatar3DEngine {
  constructor(containerEl, onStatus = () => {}) {
    this.container = containerEl;
    this.onStatus = onStatus;

    this.groupMode = false;
    this.actors = new Map(); // id -> AvatarActor
    this.singleActorId = "solo";
    this.activeSpeakerId = "solo";
    this.speakTargetId = "user";

    this.clock = new THREE.Clock();
    this.elapsed = 0;

    // 当前正在播放的语音对象与精确时间轴
    this.activeAudio = null;
    this.activeTimeline = null;

    // 视线追踪 + 摄像头实时人脸注视追踪
    this.pointer = { x: 0, y: 0 };
    this.smoothPointer = { x: 0, y: 0 };
    this.cameraGaze = {
      active: false,
      detected: false,
      x: 0,
      y: 0,
      proximity: 0.35,
      roll: 0,
      weight: 0,
      smoothX: 0,
      smoothY: 0,
      smoothProx: 0.35,
      smoothRoll: 0,
    };
    this.gazeCalibration = { offsetX: 0, offsetY: 0 };
    this._camRight = new THREE.Vector3();
    this._camUp = new THREE.Vector3();
    this._camForward = new THREE.Vector3();
    this._projVec = new THREE.Vector3();
    this._raycaster = new THREE.Raycaster();
    this._clickNdc = new THREE.Vector2();
    this._pointerDownPos = null;

    this.onNameplatesUpdate = null;
    this.onActorClick = null;

    // 音频实时分析器
    this.audioCtx = null;
    this.analyser = null;
    this.timeData = null;
    this.freqData = null;
    this.connectedSources = new WeakMap();

    this.defaultCamPos = new THREE.Vector3(0, 1.38, 1.60);
    this.defaultTarget = new THREE.Vector3(0, 1.34, 0);
    this._desiredCamTargetX = 0;
    this._camFocusActive = false;

    this._initScene();
    this._bindEvents();
    this._animate();
  }

  get vrm() {
    return this.actors.get(this.singleActorId)?.vrm || null;
  }

  _initScene() {
    const w = this.container.clientWidth || 800;
    const h = this.container.clientHeight || 450;

    this.scene = new THREE.Scene();

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

    this.camera = new THREE.PerspectiveCamera(27, w / h, 0.1, 25);
    this.camera.position.copy(this.defaultCamPos);

    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, powerPreference: "high-performance" });
    this.renderer.setSize(w, h);
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.07;
    this.container.innerHTML = "";
    this.container.appendChild(this.renderer.domElement);

    const hemi = new THREE.HemisphereLight(0xeef4ff, 0x1e293b, 1.12);
    hemi.position.set(0, 3, 0);
    this.scene.add(hemi);

    const keyLight = new THREE.DirectionalLight(0xfff5ea, 1.48);
    keyLight.position.set(1.2, 2.3, 2.5);
    this.scene.add(keyLight);

    const rimLight = new THREE.DirectionalLight(0x93c5fd, 0.90);
    rimLight.position.set(-1.8, 1.9, -1.8);
    this.scene.add(rimLight);

    const fillLight = new THREE.DirectionalLight(0xfde68a, 0.38);
    fillLight.position.set(-1.5, 1.2, 1.6);
    this.scene.add(fillLight);

    // 群聊舞台地面柔和圆台（仅群聊模式显示）
    const floorGeo = new THREE.CircleGeometry(2.3, 48);
    const floorMat = new THREE.MeshBasicMaterial({
      color: 0x1e293b,
      transparent: true,
      opacity: 0.28,
      depthWrite: false,
    });
    this.groupStageFloor = new THREE.Mesh(floorGeo, floorMat);
    this.groupStageFloor.rotation.x = -Math.PI / 2;
    this.groupStageFloor.position.set(0, 0.005, 0);
    this.groupStageFloor.visible = false;
    this.scene.add(this.groupStageFloor);

    const starGeo = new THREE.BufferGeometry();
    const count = 55;
    const pos = new Float32Array(count * 3);
    for (let i = 0; i < count; i++) {
      pos[i * 3] = (Math.random() - 0.5) * 6.0;
      pos[i * 3 + 1] = 0.5 + Math.random() * 2.4;
      pos[i * 3 + 2] = -1.2 - Math.random() * 3.0;
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
    this.controls.maxDistance = 6.5;
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
        if (this.actors.size > 0 && ((prevAspect < 1) !== (nextAspect < 1) || Math.abs(prevAspect - nextAspect) > 0.25)) {
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
    this.container.addEventListener("pointerdown", (e) => {
      this._pointerDownPos = { x: e.clientX, y: e.clientY };
    });
    this.container.addEventListener("pointerup", (e) => {
      if (!this.groupMode || !this.onActorClick || !this._pointerDownPos) return;
      const dist = Math.hypot(e.clientX - this._pointerDownPos.x, e.clientY - this._pointerDownPos.y);
      this._pointerDownPos = null;
      if (dist > 6) return;
      const rect = this.container.getBoundingClientRect();
      this._clickNdc.x = ((e.clientX - rect.left) / Math.max(1, rect.width)) * 2 - 1;
      this._clickNdc.y = -(((e.clientY - rect.top) / Math.max(1, rect.height)) * 2 - 1);
      this._raycaster.setFromCamera(this._clickNdc, this.camera);
      const roots = [];
      for (const act of this.actors.values()) {
        if (act.vrm) roots.push(act.vrm.scene);
      }
      const hits = this._raycaster.intersectObjects(roots, true);
      if (hits.length > 0) {
        const hitActorId = hits[0].object?.userData?.actorId;
        if (hitActorId && this.actors.has(hitActorId)) {
          this.onActorClick(hitActorId);
        }
      }
    });
  }

  _loadVrmRaw(url, onProgress = null) {
    return new Promise((resolve, reject) => {
      this.loader.load(
        url,
        (gltf) => {
          const vrm = gltf.userData.vrm;
          if (!vrm) {
            reject(new Error("该文件不是有效的 VRM 模型"));
            return;
          }
          VRMUtils.removeUnnecessaryVertices(gltf.scene);
          VRMUtils.combineSkeletons(gltf.scene);
          VRMUtils.rotateVRM0(vrm);
          resolve(vrm);
        },
        (progress) => {
          if (onProgress && progress.total) {
            onProgress(Math.min(99, Math.round((progress.loaded / progress.total) * 100)));
          }
        },
        (err) => reject(err)
      );
    });
  }

  /** 单人模式加载 VRM（完全兼容原有 API） */
  async loadVRM(url) {
    this.groupMode = false;
    if (this.groupStageFloor) this.groupStageFloor.visible = false;
    // 清理多余的群聊角色，只保留 solo
    for (const [id, act] of Array.from(this.actors.entries())) {
      if (id !== this.singleActorId) {
        act.dispose(this.scene);
        this.actors.delete(id);
      }
    }

    this.onStatus("正在加载 3D 虚拟人像...");
    try {
      const vrm = await this._loadVrmRaw(url, (pct) => this.onStatus(`加载 3D 模型中 ${pct}%...`));
      let actor = this.actors.get(this.singleActorId);
      if (!actor) {
        actor = new AvatarActor(this.singleActorId, { name: "小汐", color: "#60a5fa", sourceUrl: url });
        this.actors.set(this.singleActorId, actor);
      } else {
        actor.sourceUrl = url;
      }
      actor.setPlacement(0, 0, 0, 0);
      actor.attachVRM(vrm, this.scene, false);

      this.activeSpeakerId = this.singleActorId;
      this.speakTargetId = "user";
      this._autoFrameCamera();
      actor.triggerGesture("wave", 2.2, this.elapsed);
      actor.setEmotion("happy", 2.6);

      const title = vrm.meta?.name || vrm.meta?.title || "3D 虚拟形象";
      this.onStatus(`3D 形象就绪 (${title})`);
      return vrm;
    } catch (err) {
      this.onStatus("3D 模型加载失败");
      throw err;
    }
  }

  /**
   * 群聊模式批量加载并摆放 2~4 位 3D 角色到同台半圆弧舞台
   * entries: [{ id, name, role, color, blobUrl, sourceUrl }]
   */
  async loadGroupAvatars(entries, onProgress = () => {}) {
    this.groupMode = true;
    if (this.groupStageFloor) this.groupStageFloor.visible = true;

    const wantedIds = new Set(entries.map((e) => e.id));
    // 移除不在本群中的旧角色（包括 solo）
    for (const [id, act] of Array.from(this.actors.entries())) {
      if (!wantedIds.has(id)) {
        act.dispose(this.scene);
        this.actors.delete(id);
      }
    }

    for (let i = 0; i < entries.length; i++) {
      const ent = entries[i];
      let actor = this.actors.get(ent.id);
      const needsLoad = !actor || !actor.vrm || actor.sourceUrl !== ent.sourceUrl;
      if (!actor) {
        actor = new AvatarActor(ent.id, {
          name: ent.name,
          role: ent.role,
          color: ent.color,
          sourceUrl: ent.sourceUrl,
        });
        this.actors.set(ent.id, actor);
      } else {
        actor.name = ent.name;
        actor.role = ent.role;
        actor.color = ent.color;
      }

      if (needsLoad) {
        onProgress(i + 1, entries.length, ent.name);
        this.onStatus(`群舞台加载中 (${i + 1}/${entries.length}): ${ent.name}...`);
        const vrm = await this._loadVrmRaw(ent.blobUrl);
        actor.sourceUrl = ent.sourceUrl;
        actor.attachVRM(vrm, this.scene, true);
      } else {
        actor.setRingVisible(true, this.scene);
      }
    }

    // 保持按 entries 顺序排列
    const ordered = new Map();
    for (const ent of entries) {
      const a = this.actors.get(ent.id);
      if (a) ordered.set(ent.id, a);
    }
    this.actors = ordered;

    this._layoutGroupActors();
    this._autoFrameCamera();
    this.activeSpeakerId = null;
    this.speakTargetId = "user";

    // 入场打招呼错开 0.25s 更有群聊活人感
    let idx = 0;
    for (const act of this.actors.values()) {
      const delay = idx * 180;
      const gest = idx === 0 ? "wave" : (idx === 1 ? "nod_agree" : "explain_open");
      setTimeout(() => {
        act.triggerGesture(gest, 2.0, this.elapsed);
        act.setEmotion(idx === 1 ? "relaxed" : "happy", 2.5);
      }, delay);
      idx++;
    }

    this.onStatus(`3D 群聊舞台就绪 (${this.actors.size} 人同台)`);
  }

  _layoutGroupActors() {
    const list = Array.from(this.actors.values());
    const n = list.length;
    if (n === 0) return;
    if (n === 1) {
      list[0].setPlacement(0, 0, 0, 0);
      return;
    }

    // 半圆弧优雅站位：两侧角色略向前并微微朝向中心，方便彼此对视 & 面向镜头
    const layouts = {
      2: [
        { x: -0.56, z: 0.06, rotY: 0.20 },
        { x:  0.56, z: 0.06, rotY: -0.20 },
      ],
      3: [
        { x: -0.86, z: 0.14, rotY: 0.26 },
        { x:  0.00, z: -0.06, rotY: 0.00 },
        { x:  0.86, z: 0.14, rotY: -0.26 },
      ],
      4: [
        { x: -1.24, z: 0.24, rotY: 0.34 },
        { x: -0.42, z: -0.04, rotY: 0.11 },
        { x:  0.42, z: -0.04, rotY: -0.11 },
        { x:  1.24, z: 0.24, rotY: -0.34 },
      ],
    };
    const slots = layouts[n] || layouts[4];
    for (let i = 0; i < n; i++) {
      const s = slots[i] || { x: (i - (n - 1) / 2) * 0.8, z: 0, rotY: 0 };
      list[i].setPlacement(s.x, 0, s.z, s.rotY);
    }
  }

  _autoFrameCamera() {
    const list = Array.from(this.actors.values()).filter((a) => a.vrm);
    if (!list.length) return;

    let avgHeadY = 0;
    let avgChestY = 0;
    let minX = Infinity;
    let maxX = -Infinity;
    for (const act of list) {
      act.vrm.scene.updateMatrixWorld(true);
      const head = act._getBone("head");
      const chest = act._getBone("upperChest") || act._getBone("chest") || act._getBone("spine");
      const hp = new THREE.Vector3(act.basePos.x, 1.45, act.basePos.z);
      const cp = new THREE.Vector3(act.basePos.x, 1.18, act.basePos.z);
      if (head) head.getWorldPosition(hp);
      if (chest) chest.getWorldPosition(cp);
      avgHeadY += hp.y;
      avgChestY += cp.y;
      minX = Math.min(minX, act.basePos.x);
      maxX = Math.max(maxX, act.basePos.x);
    }
    avgHeadY /= list.length;
    avgChestY /= list.length;

    const aspect = this.camera.aspect || 1.77;
    if (!this.groupMode || list.length === 1) {
      if (aspect < 1.0) {
        const focusY = avgHeadY * 0.46 + avgChestY * 0.54;
        const dist = Math.min(2.05, Math.max(1.62, 1.24 / Math.max(0.55, aspect)));
        this.defaultTarget.set(0, focusY, 0);
        this.defaultCamPos.set(0, focusY + 0.04, dist);
      } else {
        const focusY = avgHeadY * 0.62 + avgChestY * 0.38;
        this.defaultTarget.set(0, focusY, 0);
        this.defaultCamPos.set(0, focusY + 0.05, 1.60);
      }
    } else {
      // 群聊多角色构图：根据同台跨度与屏幕宽高比自动算最佳全景机位，保证全员上半身+手势完整入框
      const span = Math.max(1.2, (maxX - minX) + 1.05);
      const vFovRad = THREE.MathUtils.degToRad(this.camera.fov);
      const hFovHalf = Math.atan(Math.tan(vFovRad / 2) * Math.max(0.55, aspect));
      const neededDist = (span * 0.56) / Math.max(0.22, Math.tan(hFovHalf));
      const dist = Math.max(2.35, Math.min(5.2, neededDist));
      const focusY = avgHeadY * 0.50 + avgChestY * 0.50;
      this.defaultTarget.set(0, focusY, 0.04);
      this.defaultCamPos.set(0, focusY + 0.08, dist);
    }

    this._desiredCamTargetX = this.defaultTarget.x;
    this._camFocusActive = false;
    this.resetCamera();
  }

  resetCamera() {
    this._desiredCamTargetX = this.defaultTarget.x;
    this._camFocusActive = false;
    this.camera.position.copy(this.defaultCamPos);
    this.controls.target.copy(this.defaultTarget);
    this.controls.update();
  }

  setNameplateCallback(cb) {
    this.onNameplatesUpdate = cb;
  }

  setOnActorClick(cb) {
    this.onActorClick = cb;
  }

  setCameraGaze(gaze) {
    if (!gaze || !gaze.active) {
      this.cameraGaze.active = false;
      this.cameraGaze.detected = false;
      return;
    }
    this.cameraGaze.active = true;
    this.cameraGaze.detected = !!gaze.detected;
    if (gaze.detected) {
      this.cameraGaze.x = Math.max(-1, Math.min(1, Number(gaze.x) || 0));
      this.cameraGaze.y = Math.max(-1, Math.min(1, Number(gaze.y) || 0));
      this.cameraGaze.proximity = Math.max(0, Math.min(1, Number(gaze.proximity) || 0.35));
      this.cameraGaze.roll = Math.max(-0.45, Math.min(0.45, Number(gaze.roll) || 0));
    }
  }

  setGazeCalibration(calib = {}) {
    if (calib.offsetX !== undefined && Number.isFinite(Number(calib.offsetX))) {
      this.gazeCalibration.offsetX = Math.max(-1, Math.min(1, Number(calib.offsetX)));
    }
    if (calib.offsetY !== undefined && Number.isFinite(Number(calib.offsetY))) {
      this.gazeCalibration.offsetY = Math.max(-1, Math.min(1, Number(calib.offsetY)));
    }
  }

  /** 兼容单人模式：设置全体或单人状态 */
  setState(nextState) {
    if (nextState !== "speaking") {
      this.activeAudio = null;
      this.activeTimeline = null;
      if (this.groupMode) {
        this.activeSpeakerId = null;
        this.speakTargetId = "user";
        this._desiredCamTargetX = this.defaultTarget.x;
        this._camFocusActive = true;
      }
    }
    if (!this.groupMode) {
      const solo = this.actors.get(this.singleActorId);
      if (solo) solo.setState(nextState, this.elapsed);
    } else {
      for (const act of this.actors.values()) {
        act.setState(nextState, this.elapsed);
      }
    }
  }

  /** 群聊专用：指定当前发言者与对话目标，其余角色自动进入倾听并转头看发言者 */
  setActiveSpeaker(actorId, targetId = "user") {
    this.activeSpeakerId = actorId || null;
    this.speakTargetId = targetId || "user";
    for (const [id, act] of this.actors.entries()) {
      if (id === actorId) {
        act.state = "speaking";
      } else {
        act.state = actorId ? "listening" : "idle";
      }
    }
    // 镜头轻微向发言者偏移一点点（保持全员仍在画面里，既有焦点感又不晕）
    if (this.groupMode) {
      const sp = actorId ? this.actors.get(actorId) : null;
      this._desiredCamTargetX = sp ? sp.basePos.x * 0.24 : this.defaultTarget.x;
      this._camFocusActive = true;
    }
  }

  /** 群聊专用：给任意角色独立触发表情和动作（不影响其他角色！） */
  setActorReaction(actorId, { emotion = null, gesture = null, holdSec = 3.2 } = {}) {
    const act = this.actors.get(actorId);
    if (!act) return;
    if (emotion) act.setEmotion(emotion, holdSec);
    if (gesture) act.triggerGesture(gesture, Math.min(holdSec, 2.8), this.elapsed);
  }

  setEmotion(name, holdSec = 3.2) {
    if (!this.groupMode) {
      const solo = this.actors.get(this.singleActorId);
      if (solo) solo.setEmotion(name, holdSec);
    } else {
      for (const act of this.actors.values()) act.setEmotion(name, holdSec);
    }
  }

  triggerGesture(name, durationSec = 2.6) {
    if (!this.groupMode) {
      const solo = this.actors.get(this.singleActorId);
      if (solo) solo.triggerGesture(name, durationSec, this.elapsed);
    } else {
      for (const act of this.actors.values()) act.triggerGesture(name, durationSec, this.elapsed);
    }
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
    if (this._activeAudioTimer) {
      clearTimeout(this._activeAudioTimer);
      this._activeAudioTimer = null;
    }
    if (this._activeBufferSource) {
      try {
        this._activeBufferSource.onended = null;
        this._activeBufferSource.stop(0);
        this._activeBufferSource.disconnect();
      } catch (_) {}
      this._activeBufferSource = null;
    }
    const resolvePrev = this._activeAudioDone;
    this._activeAudioDone = null;
    this.activeAudio = null;
    this.activeTimeline = null;
    if (typeof resolvePrev === "function") {
      try { resolvePrev(); } catch (_) {}
    }
  }

  async playAudioBufferBlob(blob, timeline = null, opts = {}) {
    return this.playAudioBufferBlobForActor(this.groupMode ? this.activeSpeakerId : this.singleActorId, blob, timeline, opts);
  }

  /** 播放指定角色的语音并将口型与动作时间轴精确驱动到该角色 */
  async playAudioBufferBlobForActor(actorId, blob, timeline = null, { onStart = null, isCancelled = null, targetId = "user" } = {}) {
    const ctx = this.ensureAudioAnalyser();
    if (!ctx) throw new Error("AudioContext 不可用");
    if (ctx.state === "suspended" || ctx.state === "interrupted") {
      try {
        await Promise.race([
          ctx.resume(),
          new Promise((r) => setTimeout(r, 650)),
        ]);
      } catch (_) {}
    }
    if (isCancelled && isCancelled()) return;
    if (ctx.state !== "running") {
      throw new Error(`AudioContext 未激活(${ctx.state})`);
    }
    const rawBuf = await blob.arrayBuffer();
    if (isCancelled && isCancelled()) return;

    const decoded = await new Promise((resolve, reject) => {
      const p = ctx.decodeAudioData(rawBuf.slice(0), resolve, reject);
      if (p && typeof p.then === "function") p.then(resolve, reject);
    });
    if (isCancelled && isCancelled()) return;

    this.stopActiveAudio();
    if (this.groupMode && actorId) {
      this.setActiveSpeaker(actorId, targetId);
    } else if (!this.groupMode) {
      this.activeSpeakerId = this.singleActorId;
    }

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
        if (this._activeAudioTimer) {
          clearTimeout(this._activeAudioTimer);
          this._activeAudioTimer = null;
        }
        if (this._activeAudioDone === done) {
          this._activeAudioDone = null;
        }
        if (this._activeBufferSource === src) {
          this._activeBufferSource = null;
          this.activeAudio = null;
          this.activeTimeline = null;
        }
        try { src.disconnect(); } catch (_) {}
        resolve();
      };
      this._activeAudioDone = done;
      src.onended = done;
      const maxWaitMs = Math.max(4500, Math.ceil((decoded.duration || 1) * 1000) + 3500);
      this._activeAudioTimer = setTimeout(done, maxWaitMs);
      try {
        src.start(0);
        if (onStart) onStart();
      } catch (_) {
        done();
      }
    });
  }

  attachAudioElement(audioEl, timeline = null, actorId = null) {
    if (!audioEl) return;
    if (actorId) this.activeSpeakerId = actorId;
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

  _updateSharedInputs(dt) {
    this.smoothPointer.x += (this.pointer.x - this.smoothPointer.x) * Math.min(1, dt * 5);
    this.smoothPointer.y += (this.pointer.y - this.smoothPointer.y) * Math.min(1, dt * 5);

    const targetWeight = (this.cameraGaze.active && this.cameraGaze.detected) ? 1.0 : 0.0;
    const weightRate = targetWeight > this.cameraGaze.weight ? 6.5 : 2.2;
    this.cameraGaze.weight += (targetWeight - this.cameraGaze.weight) * Math.min(1, dt * weightRate);

    const camTargetX = targetWeight > 0 ? this.cameraGaze.x : 0;
    const camTargetY = targetWeight > 0 ? this.cameraGaze.y : 0;
    const camTargetRoll = targetWeight > 0 ? this.cameraGaze.roll : 0;
    const camTargetProx = targetWeight > 0 ? this.cameraGaze.proximity : 0.35;
    const followRate = Math.min(1, dt * 11);
    this.cameraGaze.smoothX += (camTargetX - this.cameraGaze.smoothX) * followRate;
    this.cameraGaze.smoothY += (camTargetY - this.cameraGaze.smoothY) * followRate;
    this.cameraGaze.smoothRoll += (camTargetRoll - this.cameraGaze.smoothRoll) * Math.min(1, dt * 6);
    this.cameraGaze.smoothProx += (camTargetProx - this.cameraGaze.smoothProx) * Math.min(1, dt * 5);

    this.camera.updateMatrixWorld();
    this._camRight.setFromMatrixColumn(this.camera.matrixWorld, 0).normalize();
    this._camUp.setFromMatrixColumn(this.camera.matrixWorld, 1).normalize();
    this._camForward.setFromMatrixColumn(this.camera.matrixWorld, 2).normalize();
  }

  _resolveLookTarget(actor, activeSpeaker) {
    if (!this.groupMode || this.actors.size <= 1) return null;
    if (activeSpeaker && activeSpeaker.vrm) {
      if (actor.id !== activeSpeaker.id) {
        // 听众主要看向正在说话的群友，偶尔扫一眼用户保持参与感
        const glanceUser = Math.sin(this.elapsed * 0.45 + actor.phaseOffset) > 0.84;
        return glanceUser ? null : activeSpeaker;
      } else {
        // 发言者如果正在回应某位群友，在群友和用户镜头之间自然切换眼神
        const targetActor = (this.speakTargetId && this.speakTargetId !== "user")
          ? this.actors.get(this.speakTargetId)
          : null;
        if (targetActor && targetActor.id !== actor.id) {
          const lookAtPeer = Math.sin(this.elapsed * 0.72) > -0.25;
          return lookAtPeer ? targetActor : null;
        }
      }
    }
    return null;
  }

  _emitNameplateProjections() {
    if (!this.groupMode || typeof this.onNameplatesUpdate !== "function") return;
    const w = this.container.clientWidth || 1;
    const h = this.container.clientHeight || 1;
    const list = [];
    for (const act of this.actors.values()) {
      if (!act.vrm) continue;
      this._projVec.copy(act.headWorldPos);
      this._projVec.y += 0.24;
      this._projVec.project(this.camera);
      const visible = this._projVec.z >= -1 && this._projVec.z <= 1;
      const sx = (this._projVec.x * 0.5 + 0.5) * w;
      const sy = (-this._projVec.y * 0.5 + 0.5) * h;
      list.push({
        id: act.id,
        x: sx,
        y: sy,
        visible,
        speaking: act.id === this.activeSpeakerId && act.state === "speaking",
      });
    }
    this.onNameplatesUpdate(list);
  }

  _animate() {
    requestAnimationFrame(() => this._animate());
    const dt = Math.min(0.05, this.clock.getDelta());
    this.elapsed += dt;

    this._updateSharedInputs(dt);

    // 群聊模式平滑焦点跟随
    if (this.groupMode && this._camFocusActive) {
      const dx = this._desiredCamTargetX - this.controls.target.x;
      if (Math.abs(dx) > 0.002) {
        this.controls.target.x += dx * Math.min(1, dt * 2.8);
      } else {
        this._camFocusActive = false;
      }
    }

    const activeSpeaker = this.activeSpeakerId ? this.actors.get(this.activeSpeakerId) : null;
    for (const act of this.actors.values()) {
      const isSp = this.groupMode ? (act.id === this.activeSpeakerId) : (act.id === this.singleActorId);
      const lookTgt = this._resolveLookTarget(act, activeSpeaker);
      act.update(dt, this, isSp, lookTgt);
    }

    if (this.bokehPoints) {
      this.bokehPoints.rotation.y = this.elapsed * 0.025;
    }
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
    this._emitNameplateProjections();
  }
}
