import { useEffect, useRef, useState } from "react";

interface BuyerAmbientFXProps {
  videoRef: React.RefObject<HTMLVideoElement | null>;
  posterRef?: React.RefObject<HTMLImageElement | null>;
  particleCanvasRef?: React.RefObject<HTMLCanvasElement | null>;
  posterUrl: string;
  videoFailed: boolean;
  reducedMotion: boolean;
  enableWebGL?: boolean;
  variant?: "hero" | "detail";
  onWebGLStateChange?: (active: boolean) => void;
}

const VERTEX_SHADER_SOURCE = `
attribute vec2 aPosition;
varying vec2 vScreenUV;
void main() {
  vScreenUV = vec2(aPosition.x * 0.5 + 0.5, 0.5 - aPosition.y * 0.5);
  gl_Position = vec4(aPosition, 0.0, 1.0);
}
`;

const FRAGMENT_SHADER_SOURCE = `
precision highp float;

uniform sampler2D uTexture;
uniform vec2 uResolution;
uniform vec2 uMediaSize;
uniform vec2 uObjectPos;
uniform vec2 uParallax;
uniform float uTime;
uniform float uMediaKind;

varying vec2 vScreenUV;

vec2 hash22(vec2 point) {
  point = vec2(
    dot(point, vec2(127.1, 311.7)),
    dot(point, vec2(269.5, 183.3))
  );
  return -1.0 + 2.0 * fract(sin(point) * 43758.5453123);
}

float perlinNoise(vec2 point) {
  vec2 cell = floor(point);
  vec2 local = fract(point);
  vec2 curve = local * local * local * (local * (local * 6.0 - 15.0) + 10.0);

  float cornerA = dot(hash22(cell + vec2(0.0, 0.0)), local - vec2(0.0, 0.0));
  float cornerB = dot(hash22(cell + vec2(1.0, 0.0)), local - vec2(1.0, 0.0));
  float cornerC = dot(hash22(cell + vec2(0.0, 1.0)), local - vec2(0.0, 1.0));
  float cornerD = dot(hash22(cell + vec2(1.0, 1.0)), local - vec2(1.0, 1.0));

  return mix(mix(cornerA, cornerB, curve.x), mix(cornerC, cornerD, curve.x), curve.y);
}

float rotatedEllipseDist(vec2 point, vec2 center, vec2 radii, float angle) {
  vec2 delta = point - center;
  float cs = cos(angle);
  float sn = sin(angle);
  vec2 rot = vec2(delta.x * cs - delta.y * sn, delta.x * sn + delta.y * cs);
  return length(rot / radii);
}

float softBlob(vec2 point, vec2 center, vec2 radii, float angle) {
  float dist = rotatedEllipseDist(point, center, radii, angle);
  return 1.0 - smoothstep(0.32, 1.0, dist);
}

float hardLockZone(vec2 point, vec2 center, vec2 radii, float angle, float feather) {
  float dist = rotatedEllipseDist(point, center, radii, angle);
  return 1.0 - smoothstep(1.0, 1.0 + feather, dist);
}

void main() {
  vec2 safeRes = max(uResolution, vec2(1.0));
  vec2 safeMedia = max(uMediaSize, vec2(1.0));

  vec2 centeredUV = (vScreenUV - 0.5) / 1.018 + 0.5;
  vec2 parallaxScreenUV = centeredUV - (uParallax / safeRes);

  float viewAspect = safeRes.x / safeRes.y;
  float mediaAspect = safeMedia.x / safeMedia.y;
  vec2 coverScale = vec2(1.0, 1.0);
  if (viewAspect > mediaAspect) {
    coverScale.y = mediaAspect / viewAspect;
  } else {
    coverScale.x = viewAspect / mediaAspect;
  }

  vec2 mediaUV = (parallaxScreenUV - uObjectPos) * coverScale + uObjectPos;

  vec2 portraitUV = mediaUV;
  if (uMediaKind > 0.5) {
    portraitUV = vec2(0.22 + mediaUV.x * 0.625, mediaUV.y);
  }

  float faceCoreLock = hardLockZone(portraitUV, vec2(0.540, 0.392), vec2(0.154, 0.194), -0.12, 0.34);
  float crownLock = hardLockZone(portraitUV, vec2(0.558, 0.158), vec2(0.140, 0.120), -0.06, 0.30);
  float neckChestLock = hardLockZone(portraitUV, vec2(0.538, 0.652), vec2(0.144, 0.158), 0.0, 0.30);
  float handLock = hardLockZone(portraitUV, vec2(0.655, 0.820), vec2(0.155, 0.145), 0.12, 0.26);
  float leftEarLock = hardLockZone(portraitUV, vec2(0.322, 0.295), vec2(0.062, 0.044), 0.10, 0.32);
  float rightEarLock = hardLockZone(portraitUV, vec2(0.736, 0.435), vec2(0.035, 0.032), 0.12, 0.30);

  float totalLock = max(
    max(faceCoreLock, crownLock),
    max(max(neckChestLock, handLock), max(leftEarLock, rightEarLock))
  );
  float safeFactor = clamp(1.0 - totalLock, 0.0, 1.0);

  float rightHairMid = softBlob(portraitUV, vec2(0.802, 0.455), vec2(0.158, 0.240), -0.18)
    * smoothstep(0.640, 0.845, portraitUV.x);
  float rightRibbonBow = softBlob(portraitUV, vec2(0.812, 0.235), vec2(0.142, 0.155), -0.22)
    * 0.95;
  float rightRibbonStreamer = softBlob(portraitUV, vec2(0.856, 0.585), vec2(0.145, 0.195), -0.08)
    * smoothstep(0.680, 0.860, portraitUV.x);
  float rightHairTail = softBlob(portraitUV, vec2(0.880, 0.798), vec2(0.165, 0.145), 0.14)
    * smoothstep(0.720, 0.885, portraitUV.x);
  float leftHairFall = softBlob(portraitUV, vec2(0.345, 0.515), vec2(0.098, 0.185), 0.14)
    * smoothstep(0.305, 0.545, portraitUV.y)
    * 0.94;
  float leftOuterLocks = softBlob(portraitUV, vec2(0.298, 0.468), vec2(0.068, 0.145), 0.10)
    * 0.86;
  float leftSideburnWisp = softBlob(portraitUV, vec2(0.382, 0.438), vec2(0.052, 0.105), 0.08)
    * smoothstep(0.340, 0.500, portraitUV.y)
    * 0.78;

  float hairMask = clamp(
    max(
      max(max(rightHairMid, rightRibbonBow), max(rightRibbonStreamer, rightHairTail)),
      max(leftHairFall, max(leftOuterLocks, leftSideburnWisp))
    ),
    0.0,
    1.0
  );

  float windWeight = hairMask * safeFactor;

  float phaseFlow = portraitUV.x * 4.8 - portraitUV.y * 2.6;
  float primarySway = sin(uTime * 1.40 + phaseFlow);
  float secondarySway = sin(uTime * 1.82 + phaseFlow * 1.35 + 1.2);
  float macroNoiseX = perlinNoise(vec2(portraitUV.x * 3.2 - uTime * 0.26, portraitUV.y * 2.8 + uTime * 0.19));
  float macroNoiseY = perlinNoise(vec2(portraitUV.x * 2.7 + uTime * 0.21 + 5.3, portraitUV.y * 3.1 - uTime * 0.18));

  vec2 macroSwayPx = vec2(
    primarySway * 2.08 + secondarySway * 0.78 + macroNoiseX * 1.29,
    sin(uTime * 1.25 + phaseFlow * 0.88 + 0.7) * 0.81 + macroNoiseY * 0.73
  );

  float strandPhase = portraitUV.x * 15.6 + portraitUV.y * 6.4;
  float fineWaveA = sin(uTime * 2.45 + strandPhase);
  float fineWaveB = cos(uTime * 3.10 - portraitUV.x * 21.0 + portraitUV.y * 9.2);
  float fineNoise = perlinNoise(vec2(portraitUV.x * 10.5 - uTime * 0.44, portraitUV.y * 8.8 + uTime * 0.34));
  vec2 fineStrandPx = vec2(
    fineWaveA * 0.62 + fineNoise * 0.54,
    fineWaveB * 0.34 + fineNoise * 0.28
  ) * (0.55 + 0.45 * smoothstep(0.22, 0.75, hairMask));

  vec2 swayPx = (macroSwayPx + fineStrandPx) * windWeight;

  float swayMag = length(swayPx);
  if (swayMag > 3.8) {
    swayPx = (swayPx / swayMag) * 3.8;
  }

  vec2 pxToMediaUV = coverScale / safeRes;
  vec2 displacedUV = clamp(mediaUV - swayPx * pxToMediaUV, vec2(0.001), vec2(0.999));

  vec4 baseColor = texture2D(uTexture, displacedUV);
  float crestWave = smoothstep(
    0.38,
    0.96,
    0.5 + 0.5 * sin(uTime * 1.65 + phaseFlow * 1.2 + fineWaveA * 0.65)
  );
  float luminance = dot(baseColor.rgb, vec3(0.299, 0.587, 0.114));
  float highlightMask = smoothstep(0.34, 0.88, luminance);
  float shimmerStrength = windWeight * crestWave * highlightMask * 0.082;
  vec3 paleLilacGlow = vec3(0.99, 0.84, 1.00);
  vec3 finalRgb = clamp(
    mix(baseColor.rgb, max(baseColor.rgb, paleLilacGlow), shimmerStrength)
      + paleLilacGlow * (shimmerStrength * 0.34),
    0.0,
    1.0
  );

  gl_FragColor = vec4(finalRgb, baseColor.a);
}
`;

type SakuraPaletteKey = "rose" | "blush" | "orchid" | "dawn";
type DepthLayer = "far" | "near";

interface PetalParticle {
  x: number;
  y: number;
  size: number;
  speedY: number;
  driftX: number;
  swayAmp: number;
  swayFreq: number;
  phase: number;
  rotation: number;
  rotSpeed: number;
  tumblePhase: number;
  tumbleSpeed: number;
  alpha: number;
  kind: "petal" | "mote";
  layer: DepthLayer;
  depth: number;
  palette: SakuraPaletteKey;
}

const SAKURA_PALETTES: Record<SakuraPaletteKey, [string, string, string]> = {
  rose: ["#FFE4F0", "#FFB7D5", "#F58FB9"],
  blush: ["#FFE4F0", "#FFC8E2", "#FFB7D5"],
  orchid: ["#FFE4F0", "#F3A8FF", "#DF88F7"],
  dawn: ["#FFC8E2", "#FFB7D5", "#F3A8FF"],
};

const PALETTE_KEYS: SakuraPaletteKey[] = ["rose", "blush", "orchid", "dawn"];
const SPRITE_CANVAS_SIZE = 84;
const SPRITE_UNIT_RADIUS = 22;

function traceSakuraPetalPath(ctx: CanvasRenderingContext2D, radius: number): void {
  const widthRadius = radius * 0.72;
  ctx.beginPath();
  ctx.moveTo(0, radius);
  ctx.bezierCurveTo(
    -widthRadius * 1.12,
    radius * 0.42,
    -widthRadius * 1.18,
    -radius * 0.56,
    -widthRadius * 0.24,
    -radius
  );
  ctx.lineTo(0, -radius * 0.72);
  ctx.lineTo(widthRadius * 0.24, -radius);
  ctx.bezierCurveTo(
    widthRadius * 1.18,
    -radius * 0.56,
    widthRadius * 1.12,
    radius * 0.42,
    0,
    radius
  );
  ctx.closePath();
}

function createSakuraSpriteMap(): Record<string, HTMLCanvasElement> {
  const spriteMap: Record<string, HTMLCanvasElement> = {};
  const layers: DepthLayer[] = ["far", "near"];
  const center = SPRITE_CANVAS_SIZE / 2;

  for (const paletteKey of PALETTE_KEYS) {
    const [stopLight, stopMid, stopDeep] = SAKURA_PALETTES[paletteKey];
    for (const layer of layers) {
      const sprite = document.createElement("canvas");
      sprite.width = SPRITE_CANVAS_SIZE;
      sprite.height = SPRITE_CANVAS_SIZE;
      const spriteCtx = sprite.getContext("2d");
      if (!spriteCtx) continue;

      spriteCtx.save();
      if (layer === "near" && "filter" in spriteCtx) {
        spriteCtx.filter = "blur(1.5px)";
      }
      spriteCtx.translate(center, center);

      spriteCtx.shadowColor = "rgba(235, 128, 186, 0.28)";
      spriteCtx.shadowBlur = layer === "near" ? 6 : 3.5;

      const fillGrad = spriteCtx.createLinearGradient(
        -SPRITE_UNIT_RADIUS * 0.4,
        -SPRITE_UNIT_RADIUS,
        SPRITE_UNIT_RADIUS * 0.45,
        SPRITE_UNIT_RADIUS
      );
      fillGrad.addColorStop(0, stopLight);
      fillGrad.addColorStop(0.52, stopMid);
      fillGrad.addColorStop(1, stopDeep);

      traceSakuraPetalPath(spriteCtx, SPRITE_UNIT_RADIUS);
      spriteCtx.fillStyle = fillGrad;
      spriteCtx.fill();

      spriteCtx.shadowBlur = 0;
      spriteCtx.lineWidth = layer === "near" ? 1.45 : 1.2;
      spriteCtx.strokeStyle = "rgba(255, 246, 252, 0.78)";
      spriteCtx.stroke();

      spriteCtx.beginPath();
      spriteCtx.moveTo(0, SPRITE_UNIT_RADIUS * 0.72);
      spriteCtx.quadraticCurveTo(
        SPRITE_UNIT_RADIUS * 0.06,
        0,
        0,
        -SPRITE_UNIT_RADIUS * 0.46
      );
      spriteCtx.lineWidth = 0.9;
      spriteCtx.strokeStyle = "rgba(255, 255, 255, 0.45)";
      spriteCtx.stroke();

      spriteCtx.restore();
      spriteMap[`${paletteKey}_${layer}`] = sprite;
    }
  }

  return spriteMap;
}

function createParticles(width: number, height: number): PetalParticle[] {
  const isMobileViewport = width < 768;
  const petalCount = isMobileViewport ? 14 : 28;
  const moteCount = isMobileViewport ? 3 : 5;
  const nearPetalTarget = isMobileViewport ? 5 : 11;
  const particles: PetalParticle[] = [];

  for (let index = 0; index < petalCount; index += 1) {
    const isNear = index >= petalCount - nearPetalTarget;
    const layer: DepthLayer = isNear ? "near" : "far";
    particles.push({
      x: Math.random() * width,
      y: Math.random() * height,
      size: isNear ? 10.6 + Math.random() * 4.2 : 6.4 + Math.random() * 2.8,
      speedY: isNear ? 23 + Math.random() * 12 : 13 + Math.random() * 8,
      driftX: isNear ? 13 + Math.random() * 11 : 7 + Math.random() * 7,
      swayAmp: isNear ? 20 + Math.random() * 14 : 11 + Math.random() * 9,
      swayFreq: isNear ? 0.62 + Math.random() * 0.48 : 0.42 + Math.random() * 0.36,
      phase: Math.random() * Math.PI * 2,
      rotation: Math.random() * Math.PI * 2,
      rotSpeed: (Math.random() - 0.5) * (isNear ? 0.95 : 0.52),
      tumblePhase: Math.random() * Math.PI * 2,
      tumbleSpeed: isNear ? 1.15 + Math.random() * 0.85 : 0.48 + Math.random() * 0.38,
      alpha: isNear ? 0.74 + Math.random() * 0.18 : 0.64 + Math.random() * 0.18,
      kind: "petal",
      layer,
      depth: isNear ? 0.92 + Math.random() * 0.42 : 0.38 + Math.random() * 0.35,
      palette: PALETTE_KEYS[index % PALETTE_KEYS.length],
    });
  }

  for (let moteIndex = 0; moteIndex < moteCount; moteIndex += 1) {
    particles.push({
      x: Math.random() * width,
      y: Math.random() * height,
      size: 2.0 + Math.random() * 2.2,
      speedY: 7 + Math.random() * 9,
      driftX: 5 + Math.random() * 7,
      swayAmp: 8 + Math.random() * 10,
      swayFreq: 0.45 + Math.random() * 0.45,
      phase: Math.random() * Math.PI * 2,
      rotation: 0,
      rotSpeed: 0,
      tumblePhase: 0,
      tumbleSpeed: 0,
      alpha: 0.36 + Math.random() * 0.28,
      kind: "mote",
      layer: "far",
      depth: 0.48 + Math.random() * 0.45,
      palette: PALETTE_KEYS[moteIndex % PALETTE_KEYS.length],
    });
  }

  return particles;
}

function parseObjectPositionToken(token: string | undefined, fallback: number): number {
  if (!token) return fallback;
  const trimmed = token.trim().toLowerCase();
  if (trimmed === "left" || trimmed === "top") return 0;
  if (trimmed === "center") return 0.5;
  if (trimmed === "right" || trimmed === "bottom") return 1;
  if (trimmed.endsWith("%")) {
    const numeric = Number.parseFloat(trimmed);
    if (Number.isFinite(numeric)) {
      return Math.max(0, Math.min(1, numeric / 100));
    }
  }
  return fallback;
}

function resolveMediaObjectPos(
  mediaEl: HTMLElement | null,
  fallbackX: number,
  fallbackY: number
): [number, number] {
  if (!mediaEl || typeof window === "undefined" || typeof window.getComputedStyle !== "function") {
    return [fallbackX, fallbackY];
  }
  const rawObjectPosition = window.getComputedStyle(mediaEl).objectPosition;
  if (!rawObjectPosition) {
    return [fallbackX, fallbackY];
  }
  const parts = rawObjectPosition.trim().split(/\s+/);
  const posX = parseObjectPositionToken(parts[0], fallbackX);
  const posY = parseObjectPositionToken(parts[1] ?? parts[0], fallbackY);
  return [posX, posY];
}

function compileShader(
  gl: WebGLRenderingContext,
  type: number,
  source: string
): WebGLShader | null {
  const shader = gl.createShader(type);
  if (!shader) return null;
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    gl.deleteShader(shader);
    return null;
  }
  return shader;
}

export function BuyerAmbientFX({
  videoRef,
  posterRef,
  particleCanvasRef: externalParticleCanvasRef,
  posterUrl,
  videoFailed,
  reducedMotion,
  enableWebGL = true,
  variant = "hero",
  onWebGLStateChange,
}: BuyerAmbientFXProps) {
  const glCanvasRef = useRef<HTMLCanvasElement | null>(null);
  const fallbackParticleCanvasRef = useRef<HTMLCanvasElement | null>(null);
  const [webglReady, setWebglReady] = useState(false);

  useEffect(() => {
    onWebGLStateChange?.(webglReady);
  }, [onWebGLStateChange, webglReady]);

  useEffect(() => {
    if (reducedMotion) {
      setWebglReady(false);
      return;
    }

    const glCanvas = glCanvasRef.current;
    const particleCanvas = externalParticleCanvasRef?.current ?? fallbackParticleCanvasRef.current;
    if (!particleCanvas) {
      setWebglReady(false);
      return;
    }

    interface HairWebGLResources {
      gl: WebGLRenderingContext;
      program: WebGLProgram;
      vertexShader: WebGLShader;
      fragmentShader: WebGLShader;
      positionBuffer: WebGLBuffer | null;
      texture: WebGLTexture | null;
      uTexture: WebGLUniformLocation | null;
      uResolution: WebGLUniformLocation | null;
      uMediaSize: WebGLUniformLocation | null;
      uObjectPos: WebGLUniformLocation | null;
      uParallax: WebGLUniformLocation | null;
      uTime: WebGLUniformLocation | null;
      uMediaKind: WebGLUniformLocation | null;
    }

    let glResources: HairWebGLResources | null = null;

    const buildHairWebGLResources = (
      canvasElement: HTMLCanvasElement
    ): HairWebGLResources | null => {
      const glContext =
        canvasElement.getContext("webgl", {
          alpha: false,
          antialias: false,
          depth: false,
          stencil: false,
          powerPreference: "default",
          preserveDrawingBuffer: false,
        }) ||
        (canvasElement.getContext("experimental-webgl", {
          alpha: false,
          antialias: false,
          depth: false,
          stencil: false,
        }) as WebGLRenderingContext | null);

      if (!glContext || glContext.isContextLost()) {
        return null;
      }

      const vertexShader = compileShader(glContext, glContext.VERTEX_SHADER, VERTEX_SHADER_SOURCE);
      const fragmentShader = compileShader(
        glContext,
        glContext.FRAGMENT_SHADER,
        FRAGMENT_SHADER_SOURCE
      );
      const program = vertexShader && fragmentShader ? glContext.createProgram() : null;

      if (!vertexShader || !fragmentShader || !program) {
        if (program) glContext.deleteProgram(program);
        if (vertexShader) glContext.deleteShader(vertexShader);
        if (fragmentShader) glContext.deleteShader(fragmentShader);
        return null;
      }

      glContext.attachShader(program, vertexShader);
      glContext.attachShader(program, fragmentShader);
      glContext.linkProgram(program);
      if (!glContext.getProgramParameter(program, glContext.LINK_STATUS)) {
        glContext.deleteProgram(program);
        glContext.deleteShader(vertexShader);
        glContext.deleteShader(fragmentShader);
        return null;
      }

      const positionBuffer = glContext.createBuffer();
      glContext.bindBuffer(glContext.ARRAY_BUFFER, positionBuffer);
      glContext.bufferData(
        glContext.ARRAY_BUFFER,
        new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]),
        glContext.STATIC_DRAW
      );

      const posLoc = glContext.getAttribLocation(program, "aPosition");
      glContext.enableVertexAttribArray(posLoc);
      glContext.vertexAttribPointer(posLoc, 2, glContext.FLOAT, false, 0, 0);

      const texture = glContext.createTexture();
      glContext.bindTexture(glContext.TEXTURE_2D, texture);
      glContext.pixelStorei(glContext.UNPACK_FLIP_Y_WEBGL, 0);
      glContext.texParameteri(glContext.TEXTURE_2D, glContext.TEXTURE_WRAP_S, glContext.CLAMP_TO_EDGE);
      glContext.texParameteri(glContext.TEXTURE_2D, glContext.TEXTURE_WRAP_T, glContext.CLAMP_TO_EDGE);
      glContext.texParameteri(glContext.TEXTURE_2D, glContext.TEXTURE_MIN_FILTER, glContext.LINEAR);
      glContext.texParameteri(glContext.TEXTURE_2D, glContext.TEXTURE_MAG_FILTER, glContext.LINEAR);
      glContext.texImage2D(
        glContext.TEXTURE_2D,
        0,
        glContext.RGBA,
        1,
        1,
        0,
        glContext.RGBA,
        glContext.UNSIGNED_BYTE,
        new Uint8Array([244, 238, 249, 255])
      );

      return {
        gl: glContext,
        program,
        vertexShader,
        fragmentShader,
        positionBuffer,
        texture,
        uTexture: glContext.getUniformLocation(program, "uTexture"),
        uResolution: glContext.getUniformLocation(program, "uResolution"),
        uMediaSize: glContext.getUniformLocation(program, "uMediaSize"),
        uObjectPos: glContext.getUniformLocation(program, "uObjectPos"),
        uParallax: glContext.getUniformLocation(program, "uParallax"),
        uTime: glContext.getUniformLocation(program, "uTime"),
        uMediaKind: glContext.getUniformLocation(program, "uMediaKind"),
      };
    };

    if (enableWebGL && glCanvas) {
      glResources = buildHairWebGLResources(glCanvas);
    }

    if (!glResources) {
      setWebglReady(false);
    }

    const ctx2d = particleCanvas.getContext("2d");
    const sakuraSprites = createSakuraSpriteMap();

    let disposed = false;
    let contextLost = false;
    let textureLoaded = false;
    let posterUploaded = false;
    let mediaWidth = 1920;
    let mediaHeight = 1200;
    let mediaKind = 0.0;
    let cssWidth = window.innerWidth;
    let cssHeight = window.innerHeight;
    let isMobileBucket = cssWidth < 768;
    let objectPosX = variant === "detail" ? 0.56 : isMobileBucket ? 0.43 : 0.62;
    let objectPosY = variant === "detail" ? 0.22 : isMobileBucket ? 0.14 : 0.24;
    let particles = createParticles(cssWidth, cssHeight);

    const targetParallax = { x: 0, y: 0 };
    const currentParallax = { x: 0, y: 0 };

    const syncObjectPosition = () => {
      const fallbackX = variant === "detail" ? 0.56 : cssWidth < 768 ? 0.43 : 0.62;
      const fallbackY = variant === "detail" ? 0.22 : cssWidth < 768 ? 0.14 : 0.24;
      const mediaElement = videoRef.current ?? posterRef?.current ?? null;
      const [nextPosX, nextPosY] = resolveMediaObjectPos(mediaElement, fallbackX, fallbackY);
      objectPosX = nextPosX;
      objectPosY = nextPosY;
    };

    const posterImg = new Image();
    posterImg.decoding = "async";
    posterImg.src = posterUrl;
    posterImg.onload = () => {
      if (disposed || contextLost || !glResources) return;
      const videoEl = videoRef.current;
      const videoReady =
        !videoFailed &&
        videoEl &&
        videoEl.readyState >= 2 &&
        videoEl.videoWidth > 0 &&
        videoEl.videoHeight > 0;
      if (!videoReady && posterImg.naturalWidth > 0 && posterImg.naturalHeight > 0) {
        glResources.gl.bindTexture(glResources.gl.TEXTURE_2D, glResources.texture);
        glResources.gl.texImage2D(
          glResources.gl.TEXTURE_2D,
          0,
          glResources.gl.RGBA,
          glResources.gl.RGBA,
          glResources.gl.UNSIGNED_BYTE,
          posterImg
        );
        mediaWidth = posterImg.naturalWidth;
        mediaHeight = posterImg.naturalHeight;
        mediaKind = mediaWidth / mediaHeight > 1.3 ? 0.0 : 1.0;
        posterUploaded = true;
        textureLoaded = true;
        setWebglReady(true);
      }
    };

    const resize = () => {
      cssWidth = Math.max(1, window.innerWidth);
      cssHeight = Math.max(1, window.innerHeight);
      const nextMobileBucket = cssWidth < 768;
      const dpr = Math.min(window.devicePixelRatio || 1, 1.75);
      if (glCanvas && glResources) {
        glCanvas.width = Math.round(cssWidth * dpr);
        glCanvas.height = Math.round(cssHeight * dpr);
        glResources.gl.viewport(0, 0, glCanvas.width, glCanvas.height);
      }

      particleCanvas.width = Math.round(cssWidth * dpr);
      particleCanvas.height = Math.round(cssHeight * dpr);
      if (ctx2d) {
        ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0);
      }
      if (particles.length === 0 || nextMobileBucket !== isMobileBucket) {
        isMobileBucket = nextMobileBucket;
        particles = createParticles(cssWidth, cssHeight);
      }
      syncObjectPosition();
    };

    resize();

    const handlePointerMove = (event: PointerEvent) => {
      if (event.pointerType === "touch") return;
      const nx = (event.clientX / Math.max(1, window.innerWidth) - 0.5) * 2;
      const ny = (event.clientY / Math.max(1, window.innerHeight) - 0.5) * 2;
      targetParallax.x = Math.max(-6, Math.min(6, nx * 6));
      targetParallax.y = Math.max(-6, Math.min(6, ny * 6));
    };

    const handlePointerLeave = () => {
      targetParallax.x = 0;
      targetParallax.y = 0;
    };

    const handleContextLost = (event: Event) => {
      event.preventDefault();
      contextLost = true;
      textureLoaded = false;
      posterUploaded = false;
      glResources = null;
      setWebglReady(false);
    };

    const handleContextRestored = () => {
      if (disposed || !enableWebGL || !glCanvas) return;
      contextLost = false;
      textureLoaded = false;
      posterUploaded = false;
      glResources = buildHairWebGLResources(glCanvas);
      if (glResources) {
        glResources.gl.viewport(0, 0, glCanvas.width, glCanvas.height);
      }
    };

    if (glCanvas) {
      glCanvas.addEventListener("webglcontextlost", handleContextLost);
      glCanvas.addEventListener("webglcontextrestored", handleContextRestored);
    }
    window.addEventListener("resize", resize);
    window.addEventListener("pointermove", handlePointerMove, { passive: true });
    window.addEventListener("pointerleave", handlePointerLeave);

    let rafId = 0;
    let lastTs = performance.now();
    const startTs = lastTs;

    const renderFrame = (now: number) => {
      if (disposed) return;
      rafId = window.requestAnimationFrame(renderFrame);
      if (document.hidden) {
        lastTs = now;
        return;
      }

      const dt = Math.min(0.05, Math.max(0.001, (now - lastTs) / 1000));
      lastTs = now;
      const elapsed = (now - startTs) / 1000;

      currentParallax.x += (targetParallax.x - currentParallax.x) * 0.065;
      currentParallax.y += (targetParallax.y - currentParallax.y) * 0.065;

      if (glResources && !contextLost) {
        const { gl } = glResources;
        const videoEl = videoRef.current;
        const hasVideoFrame =
          !videoFailed &&
          videoEl &&
          videoEl.readyState >= 2 &&
          videoEl.videoWidth > 0 &&
          videoEl.videoHeight > 0;

        if (hasVideoFrame) {
          gl.bindTexture(gl.TEXTURE_2D, glResources.texture);
          gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, videoEl);
          mediaWidth = videoEl.videoWidth;
          mediaHeight = videoEl.videoHeight;
          mediaKind = mediaWidth / mediaHeight > 1.3 ? 0.0 : 1.0;
          if (!textureLoaded) {
            syncObjectPosition();
            textureLoaded = true;
            setWebglReady(true);
          }
        } else if (
          !posterUploaded &&
          posterImg.complete &&
          posterImg.naturalWidth > 0 &&
          posterImg.naturalHeight > 0
        ) {
          gl.bindTexture(gl.TEXTURE_2D, glResources.texture);
          gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, posterImg);
          mediaWidth = posterImg.naturalWidth;
          mediaHeight = posterImg.naturalHeight;
          mediaKind = mediaWidth / mediaHeight > 1.3 ? 0.0 : 1.0;
          posterUploaded = true;
          if (!textureLoaded) {
            syncObjectPosition();
            textureLoaded = true;
            setWebglReady(true);
          }
        }

        if (textureLoaded) {
          gl.useProgram(glResources.program);
          gl.activeTexture(gl.TEXTURE0);
          gl.bindTexture(gl.TEXTURE_2D, glResources.texture);
          gl.uniform1i(glResources.uTexture, 0);
          gl.uniform2f(glResources.uResolution, cssWidth, cssHeight);
          gl.uniform2f(glResources.uMediaSize, mediaWidth, mediaHeight);
          gl.uniform2f(glResources.uObjectPos, objectPosX, objectPosY);
          gl.uniform2f(glResources.uParallax, currentParallax.x, currentParallax.y);
          gl.uniform1f(glResources.uTime, elapsed);
          gl.uniform1f(glResources.uMediaKind, mediaKind);
          gl.drawArrays(gl.TRIANGLES, 0, 6);
        }
      }

      if (ctx2d) {
        ctx2d.clearRect(0, 0, cssWidth, cssHeight);
        for (let idx = 0; idx < particles.length; idx += 1) {
          const particle = particles[idx];
          particle.y += particle.speedY * dt;
          particle.x += particle.driftX * dt;
          particle.rotation += particle.rotSpeed * dt;

          if (particle.y > cssHeight + 36) {
            particle.y = -32;
            particle.x = Math.random() * cssWidth;
          }
          if (particle.x > cssWidth + 40) {
            particle.x = -36;
          }

          const swayX = Math.sin(elapsed * particle.swayFreq + particle.phase) * particle.swayAmp;
          const swayY =
            particle.layer === "near"
              ? Math.cos(elapsed * particle.swayFreq * 0.85 + particle.phase) * 4.5
              : 0;
          const drawX = particle.x + swayX + currentParallax.x * particle.depth * 1.25;
          const drawY = particle.y + swayY + currentParallax.y * particle.depth * 1.25;
          const pulse = 0.84 + 0.16 * Math.sin(elapsed * 1.35 + particle.phase);
          const alpha = particle.alpha * pulse;

          ctx2d.save();
          ctx2d.translate(drawX, drawY);

          if (particle.kind === "petal") {
            const swayRoll =
              Math.sin(elapsed * (particle.layer === "near" ? 1.15 : 0.78) + particle.phase) *
              (particle.layer === "near" ? 0.42 : 0.24);
            const tumbleAngle = elapsed * particle.tumbleSpeed + particle.tumblePhase;
            const tumbleScaleY =
              particle.layer === "near"
                ? 0.34 + 0.66 * Math.abs(Math.cos(tumbleAngle))
                : 0.68 + 0.32 * Math.abs(Math.cos(tumbleAngle));
            const tumbleScaleX =
              particle.layer === "near"
                ? 0.86 + 0.14 * Math.abs(Math.sin(tumbleAngle * 0.7))
                : 1.0;

            ctx2d.rotate(particle.rotation + swayRoll);
            ctx2d.scale(tumbleScaleX, tumbleScaleY);
            ctx2d.globalAlpha = Math.min(1, alpha);

            const sprite = sakuraSprites[`${particle.palette}_${particle.layer}`];
            if (sprite) {
              const drawSpan = (particle.size / SPRITE_UNIT_RADIUS) * SPRITE_CANVAS_SIZE;
              ctx2d.drawImage(sprite, -drawSpan / 2, -drawSpan / 2, drawSpan, drawSpan);
            } else {
              const [stopLight, stopMid] = SAKURA_PALETTES[particle.palette];
              const grad = ctx2d.createLinearGradient(0, -particle.size, 0, particle.size);
              grad.addColorStop(0, stopLight);
              grad.addColorStop(1, stopMid);
              traceSakuraPetalPath(ctx2d, particle.size);
              ctx2d.fillStyle = grad;
              ctx2d.fill();
            }
          } else {
            const glow = ctx2d.createRadialGradient(0, 0, 0, 0, 0, particle.size * 2.4);
            glow.addColorStop(0, `rgba(255, 250, 253, ${alpha})`);
            glow.addColorStop(0.48, `rgba(255, 183, 213, ${alpha * 0.62})`);
            glow.addColorStop(1, "rgba(255, 255, 255, 0)");
            ctx2d.fillStyle = glow;
            ctx2d.beginPath();
            ctx2d.arc(0, 0, particle.size * 2.4, 0, Math.PI * 2);
            ctx2d.fill();
          }

          ctx2d.restore();
        }
      }
    };

    rafId = window.requestAnimationFrame(renderFrame);

    return () => {
      disposed = true;
      window.cancelAnimationFrame(rafId);
      if (glCanvas) {
        glCanvas.removeEventListener("webglcontextlost", handleContextLost);
        glCanvas.removeEventListener("webglcontextrestored", handleContextRestored);
      }
      window.removeEventListener("resize", resize);
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerleave", handlePointerLeave);
      if (glResources && !contextLost) {
        glResources.gl.deleteTexture(glResources.texture);
        glResources.gl.deleteBuffer(glResources.positionBuffer);
        glResources.gl.deleteProgram(glResources.program);
        glResources.gl.deleteShader(glResources.vertexShader);
        glResources.gl.deleteShader(glResources.fragmentShader);
      }
    };
  }, [
    enableWebGL,
    externalParticleCanvasRef,
    posterRef,
    posterUrl,
    reducedMotion,
    variant,
    videoFailed,
    videoRef,
  ]);

  if (reducedMotion) {
    return null;
  }

  return (
    <>
      {enableWebGL ? (
        <canvas
          ref={glCanvasRef}
          className={`buyer-wallpaper-webgl ${webglReady ? "is-ready" : ""}`}
          data-webgl-ready={webglReady ? "true" : "false"}
          aria-hidden="true"
        />
      ) : null}
      {!externalParticleCanvasRef ? (
        <canvas
          ref={fallbackParticleCanvasRef}
          className="buyer-wallpaper-particles"
          aria-hidden="true"
        />
      ) : null}
    </>
  );
}
