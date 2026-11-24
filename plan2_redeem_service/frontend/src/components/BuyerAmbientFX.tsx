import { useEffect, useRef, useState } from "react";

interface BuyerAmbientFXProps {
  videoRef: React.RefObject<HTMLVideoElement | null>;
  posterUrl: string;
  videoFailed: boolean;
  reducedMotion: boolean;
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

vec2 hash22(vec2 p) {
  p = vec2(
    dot(p, vec2(127.1, 311.7)),
    dot(p, vec2(269.5, 183.3))
  );
  return -1.0 + 2.0 * fract(sin(p) * 43758.5453123);
}

float perlinNoise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  vec2 u = f * f * f * (f * (f * 6.0 - 15.0) + 10.0);

  float a = dot(hash22(i + vec2(0.0, 0.0)), f - vec2(0.0, 0.0));
  float b = dot(hash22(i + vec2(1.0, 0.0)), f - vec2(1.0, 0.0));
  float c = dot(hash22(i + vec2(0.0, 1.0)), f - vec2(0.0, 1.0));
  float d = dot(hash22(i + vec2(1.0, 1.0)), f - vec2(1.0, 1.0));

  return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

float rotatedEllipseDist(vec2 p, vec2 center, vec2 radii, float angle) {
  vec2 d = p - center;
  float cs = cos(angle);
  float sn = sin(angle);
  vec2 rot = vec2(d.x * cs - d.y * sn, d.x * sn + d.y * cs);
  return length(rot / radii);
}

float softBlob(vec2 p, vec2 center, vec2 radii, float angle) {
  float d = rotatedEllipseDist(p, center, radii, angle);
  return 1.0 - smoothstep(0.32, 1.0, d);
}

float hardLockZone(vec2 p, vec2 center, vec2 radii, float angle, float feather) {
  float d = rotatedEllipseDist(p, center, radii, angle);
  return 1.0 - smoothstep(1.0, 1.0 + feather, d);
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

  float faceCoreLock = hardLockZone(portraitUV, vec2(0.548, 0.372), vec2(0.148, 0.176), -0.14, 0.30);
  float crownLock = hardLockZone(portraitUV, vec2(0.558, 0.165), vec2(0.135, 0.115), -0.06, 0.28);
  float neckChestLock = hardLockZone(portraitUV, vec2(0.538, 0.648), vec2(0.142, 0.155), 0.0, 0.28);
  float handLock = hardLockZone(portraitUV, vec2(0.655, 0.820), vec2(0.155, 0.145), 0.12, 0.25);
  float leftEarLock = hardLockZone(portraitUV, vec2(0.344, 0.302), vec2(0.044, 0.036), 0.08, 0.28);

  float totalLock = max(
    max(faceCoreLock, crownLock),
    max(max(neckChestLock, handLock), leftEarLock)
  );
  float safeFactor = clamp(1.0 - totalLock, 0.0, 1.0);

  float rightHairMid = softBlob(portraitUV, vec2(0.805, 0.455), vec2(0.145, 0.225), -0.18)
    * smoothstep(0.655, 0.855, portraitUV.x);
  float rightRibbonBow = softBlob(portraitUV, vec2(0.820, 0.240), vec2(0.128, 0.138), -0.22)
    * 0.92;
  float rightHairTail = softBlob(portraitUV, vec2(0.880, 0.798), vec2(0.155, 0.132), 0.14)
    * smoothstep(0.730, 0.890, portraitUV.x);
  float leftHairFall = softBlob(portraitUV, vec2(0.356, 0.515), vec2(0.088, 0.168), 0.14)
    * smoothstep(0.315, 0.560, portraitUV.y)
    * 0.88;
  float leftOuterLocks = softBlob(portraitUV, vec2(0.312, 0.468), vec2(0.058, 0.125), 0.10)
    * 0.72;

  float hairMask = clamp(
    max(max(rightHairMid, rightRibbonBow), max(rightHairTail, max(leftHairFall, leftOuterLocks))),
    0.0,
    1.0
  );

  float windWeight = hairMask * safeFactor;

  float phaseFlow = portraitUV.x * 4.8 - portraitUV.y * 2.6;
  float primarySway = sin(uTime * 1.18 + phaseFlow);
  float secondarySway = sin(uTime * 1.54 + phaseFlow * 1.35 + 1.2);
  float nX = perlinNoise(vec2(portraitUV.x * 3.2 - uTime * 0.22, portraitUV.y * 2.8 + uTime * 0.16));
  float nY = perlinNoise(vec2(portraitUV.x * 2.7 + uTime * 0.18 + 5.3, portraitUV.y * 3.1 - uTime * 0.15));

  vec2 swayPx = vec2(
    primarySway * 1.48 + secondarySway * 0.56 + nX * 0.92,
    sin(uTime * 1.06 + phaseFlow * 0.88 + 0.7) * 0.58 + nY * 0.52
  ) * windWeight;

  float swayMag = length(swayPx);
  if (swayMag > 3.0) {
    swayPx = (swayPx / swayMag) * 3.0;
  }

  vec2 pxToMediaUV = coverScale / safeRes;
  vec2 displacedUV = clamp(mediaUV - swayPx * pxToMediaUV, vec2(0.001), vec2(0.999));

  gl_FragColor = texture2D(uTexture, displacedUV);
}
`;

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
  alpha: number;
  kind: "petal" | "mote";
  depth: number;
  hue: "lavender" | "blush" | "cream";
}

function createParticles(width: number, height: number): PetalParticle[] {
  const count = width < 768 ? 14 : 22;
  const particles: PetalParticle[] = [];
  const hues: Array<PetalParticle["hue"]> = ["lavender", "blush", "cream"];

  for (let index = 0; index < count; index += 1) {
    const isPetal = index % 3 !== 0;
    particles.push({
      x: Math.random() * width,
      y: Math.random() * height,
      size: isPetal ? 5.5 + Math.random() * 6.5 : 1.6 + Math.random() * 2.4,
      speedY: isPetal ? 11 + Math.random() * 14 : 6 + Math.random() * 10,
      driftX: (isPetal ? 7 : 4) + Math.random() * 9,
      swayAmp: isPetal ? 12 + Math.random() * 18 : 6 + Math.random() * 10,
      swayFreq: 0.45 + Math.random() * 0.55,
      phase: Math.random() * Math.PI * 2,
      rotation: Math.random() * Math.PI * 2,
      rotSpeed: (Math.random() - 0.5) * 0.65,
      alpha: isPetal ? 0.24 + Math.random() * 0.24 : 0.28 + Math.random() * 0.34,
      kind: isPetal ? "petal" : "mote",
      depth: 0.45 + Math.random() * 0.85,
      hue: hues[index % hues.length],
    });
  }
  return particles;
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
  posterUrl,
  videoFailed,
  reducedMotion,
  onWebGLStateChange,
}: BuyerAmbientFXProps) {
  const glCanvasRef = useRef<HTMLCanvasElement | null>(null);
  const particleCanvasRef = useRef<HTMLCanvasElement | null>(null);
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
    const particleCanvas = particleCanvasRef.current;
    if (!glCanvas || !particleCanvas) return;

    const gl =
      glCanvas.getContext("webgl", {
        alpha: false,
        antialias: false,
        depth: false,
        stencil: false,
        powerPreference: "default",
        preserveDrawingBuffer: false,
      }) ||
      (glCanvas.getContext("experimental-webgl", {
        alpha: false,
        antialias: false,
        depth: false,
        stencil: false,
      }) as WebGLRenderingContext | null);

    if (!gl) {
      setWebglReady(false);
      return;
    }

    const vertexShader = compileShader(gl, gl.VERTEX_SHADER, VERTEX_SHADER_SOURCE);
    const fragmentShader = compileShader(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER_SOURCE);
    if (!vertexShader || !fragmentShader) {
      if (vertexShader) gl.deleteShader(vertexShader);
      if (fragmentShader) gl.deleteShader(fragmentShader);
      setWebglReady(false);
      return;
    }

    const program = gl.createProgram();
    if (!program) {
      gl.deleteShader(vertexShader);
      gl.deleteShader(fragmentShader);
      setWebglReady(false);
      return;
    }

    gl.attachShader(program, vertexShader);
    gl.attachShader(program, fragmentShader);
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      gl.deleteProgram(program);
      gl.deleteShader(vertexShader);
      gl.deleteShader(fragmentShader);
      setWebglReady(false);
      return;
    }

    const positionBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
    gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]),
      gl.STATIC_DRAW
    );

    const posLoc = gl.getAttribLocation(program, "aPosition");
    gl.enableVertexAttribArray(posLoc);
    gl.vertexAttribPointer(posLoc, 2, gl.FLOAT, false, 0, 0);

    const texture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, 0);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texImage2D(
      gl.TEXTURE_2D,
      0,
      gl.RGBA,
      1,
      1,
      0,
      gl.RGBA,
      gl.UNSIGNED_BYTE,
      new Uint8Array([244, 238, 249, 255])
    );

    const uTexture = gl.getUniformLocation(program, "uTexture");
    const uResolution = gl.getUniformLocation(program, "uResolution");
    const uMediaSize = gl.getUniformLocation(program, "uMediaSize");
    const uObjectPos = gl.getUniformLocation(program, "uObjectPos");
    const uParallax = gl.getUniformLocation(program, "uParallax");
    const uTime = gl.getUniformLocation(program, "uTime");
    const uMediaKind = gl.getUniformLocation(program, "uMediaKind");

    const ctx2d = particleCanvas.getContext("2d");

    let disposed = false;
    let contextLost = false;
    let textureLoaded = false;
    let posterUploaded = false;
    let mediaWidth = 1920;
    let mediaHeight = 1200;
    let mediaKind = 0.0;
    let cssWidth = window.innerWidth;
    let cssHeight = window.innerHeight;
    let particles = createParticles(cssWidth, cssHeight);

    const targetParallax = { x: 0, y: 0 };
    const currentParallax = { x: 0, y: 0 };

    const posterImg = new Image();
    posterImg.decoding = "async";
    posterImg.src = posterUrl;
    posterImg.onload = () => {
      if (disposed || contextLost) return;
      const videoEl = videoRef.current;
      const videoReady =
        !videoFailed &&
        videoEl &&
        videoEl.readyState >= 2 &&
        videoEl.videoWidth > 0 &&
        videoEl.videoHeight > 0;
      if (!videoReady && posterImg.naturalWidth > 0 && posterImg.naturalHeight > 0) {
        gl.bindTexture(gl.TEXTURE_2D, texture);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, posterImg);
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
      const dpr = Math.min(window.devicePixelRatio || 1, 1.75);
      glCanvas.width = Math.round(cssWidth * dpr);
      glCanvas.height = Math.round(cssHeight * dpr);
      gl.viewport(0, 0, glCanvas.width, glCanvas.height);

      particleCanvas.width = Math.round(cssWidth * dpr);
      particleCanvas.height = Math.round(cssHeight * dpr);
      if (ctx2d) {
        ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0);
      }
      if (particles.length === 0) {
        particles = createParticles(cssWidth, cssHeight);
      }
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
      setWebglReady(false);
    };

    const handleContextRestored = () => {
      contextLost = false;
    };

    glCanvas.addEventListener("webglcontextlost", handleContextLost);
    glCanvas.addEventListener("webglcontextrestored", handleContextRestored);
    window.addEventListener("resize", resize);
    window.addEventListener("pointermove", handlePointerMove, { passive: true });
    window.addEventListener("pointerleave", handlePointerLeave);

    let rafId = 0;
    let lastTs = performance.now();
    const startTs = lastTs;

    const renderFrame = (now: number) => {
      if (disposed) return;
      rafId = window.requestAnimationFrame(renderFrame);
      if (document.hidden || contextLost) {
        lastTs = now;
        return;
      }

      const dt = Math.min(0.05, Math.max(0.001, (now - lastTs) / 1000));
      lastTs = now;
      const elapsed = (now - startTs) / 1000;

      currentParallax.x += (targetParallax.x - currentParallax.x) * 0.065;
      currentParallax.y += (targetParallax.y - currentParallax.y) * 0.065;

      const videoEl = videoRef.current;
      const hasVideoFrame =
        !videoFailed &&
        videoEl &&
        videoEl.readyState >= 2 &&
        videoEl.videoWidth > 0 &&
        videoEl.videoHeight > 0;

      if (hasVideoFrame) {
        gl.bindTexture(gl.TEXTURE_2D, texture);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, videoEl);
        mediaWidth = videoEl.videoWidth;
        mediaHeight = videoEl.videoHeight;
        mediaKind = mediaWidth / mediaHeight > 1.3 ? 0.0 : 1.0;
        if (!textureLoaded) {
          textureLoaded = true;
          setWebglReady(true);
        }
      } else if (
        !posterUploaded &&
        posterImg.complete &&
        posterImg.naturalWidth > 0 &&
        posterImg.naturalHeight > 0
      ) {
        gl.bindTexture(gl.TEXTURE_2D, texture);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, posterImg);
        mediaWidth = posterImg.naturalWidth;
        mediaHeight = posterImg.naturalHeight;
        mediaKind = mediaWidth / mediaHeight > 1.3 ? 0.0 : 1.0;
        posterUploaded = true;
        if (!textureLoaded) {
          textureLoaded = true;
          setWebglReady(true);
        }
      }

      if (textureLoaded) {
        const isMobile = cssWidth <= 768;
        gl.useProgram(program);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, texture);
        gl.uniform1i(uTexture, 0);
        gl.uniform2f(uResolution, cssWidth, cssHeight);
        gl.uniform2f(uMediaSize, mediaWidth, mediaHeight);
        gl.uniform2f(uObjectPos, isMobile ? 0.54 : 0.62, isMobile ? 0.16 : 0.24);
        gl.uniform2f(uParallax, currentParallax.x, currentParallax.y);
        gl.uniform1f(uTime, elapsed);
        gl.uniform1f(uMediaKind, mediaKind);
        gl.drawArrays(gl.TRIANGLES, 0, 6);
      }

      if (ctx2d) {
        ctx2d.clearRect(0, 0, cssWidth, cssHeight);
        for (let idx = 0; idx < particles.length; idx += 1) {
          const particle = particles[idx];
          particle.y += particle.speedY * dt;
          particle.x += particle.driftX * dt;
          particle.rotation += particle.rotSpeed * dt;

          if (particle.y > cssHeight + 24) {
            particle.y = -20;
            particle.x = Math.random() * cssWidth;
          }
          if (particle.x > cssWidth + 28) {
            particle.x = -24;
          }

          const swayX = Math.sin(elapsed * particle.swayFreq + particle.phase) * particle.swayAmp;
          const drawX = particle.x + swayX + currentParallax.x * particle.depth * 1.1;
          const drawY = particle.y + currentParallax.y * particle.depth * 1.1;
          const pulse = 0.72 + 0.28 * Math.sin(elapsed * 1.4 + particle.phase);
          const alpha = particle.alpha * pulse;

          ctx2d.save();
          ctx2d.translate(drawX, drawY);

          if (particle.kind === "petal") {
            ctx2d.rotate(particle.rotation + Math.sin(elapsed * 0.8 + particle.phase) * 0.25);
            const grad = ctx2d.createRadialGradient(0, 0, 0.5, 0, 0, particle.size);
            if (particle.hue === "lavender") {
              grad.addColorStop(0, `rgba(255, 250, 255, ${alpha})`);
              grad.addColorStop(0.55, `rgba(224, 190, 255, ${alpha * 0.85})`);
              grad.addColorStop(1, "rgba(216, 180, 254, 0)");
            } else if (particle.hue === "blush") {
              grad.addColorStop(0, `rgba(255, 248, 252, ${alpha})`);
              grad.addColorStop(0.55, `rgba(251, 207, 232, ${alpha * 0.85})`);
              grad.addColorStop(1, "rgba(244, 114, 182, 0)");
            } else {
              grad.addColorStop(0, `rgba(255, 253, 246, ${alpha})`);
              grad.addColorStop(0.6, `rgba(254, 243, 235, ${alpha * 0.78})`);
              grad.addColorStop(1, "rgba(255, 255, 255, 0)");
            }
            ctx2d.fillStyle = grad;
            ctx2d.beginPath();
            ctx2d.ellipse(0, 0, particle.size, particle.size * 0.56, 0, 0, Math.PI * 2);
            ctx2d.fill();
          } else {
            const glow = ctx2d.createRadialGradient(0, 0, 0, 0, 0, particle.size * 2.4);
            glow.addColorStop(0, `rgba(255, 255, 255, ${alpha})`);
            glow.addColorStop(0.45, `rgba(240, 218, 255, ${alpha * 0.55})`);
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
      glCanvas.removeEventListener("webglcontextlost", handleContextLost);
      glCanvas.removeEventListener("webglcontextrestored", handleContextRestored);
      window.removeEventListener("resize", resize);
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerleave", handlePointerLeave);
      if (!contextLost) {
        gl.deleteTexture(texture);
        gl.deleteBuffer(positionBuffer);
        gl.deleteProgram(program);
        gl.deleteShader(vertexShader);
        gl.deleteShader(fragmentShader);
      }
    };
  }, [posterUrl, reducedMotion, videoFailed, videoRef]);

  if (reducedMotion) {
    return null;
  }

  return (
    <>
      <canvas
        ref={glCanvasRef}
        className={`buyer-wallpaper-webgl ${webglReady ? "is-ready" : ""}`}
        data-webgl-ready={webglReady ? "true" : "false"}
        aria-hidden="true"
      />
      <canvas
        ref={particleCanvasRef}
        className="buyer-wallpaper-particles"
        aria-hidden="true"
      />
    </>
  );
}
