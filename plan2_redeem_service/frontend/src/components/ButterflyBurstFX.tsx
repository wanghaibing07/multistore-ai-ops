import { useEffect, useRef } from "react";
import { tsParticles, type Container } from "@tsparticles/engine";
import { loadSlim } from "@tsparticles/slim";
import { prefersReducedMotion } from "../utils/viewTransition";

const BUTTERFLY_SPRITE_URL = "/assets/stickers/butterfly-particle.png";
const PETAL_SPRITE_URL = "/assets/stickers/petal-particle.png";

let engineInitPromise: Promise<void> | null = null;
let activeContainer: Container | null = null;
let lastStardustAt = 0;

function ensureParticlesEngine(): Promise<void> {
  if (!engineInitPromise) {
    engineInitPromise = loadSlim(tsParticles);
  }
  return engineInitPromise;
}

function resolveAnchorCanvasCenter(
  anchorEl: HTMLElement | null,
  container: Container
): { x: number; y: number } {
  const ratio = container.retina.pixelRatio || 1;
  if (!anchorEl || typeof window === "undefined") {
    return {
      x: (window.innerWidth * 0.5) * ratio,
      y: (window.innerHeight * 0.5) * ratio,
    };
  }
  const rect = anchorEl.getBoundingClientRect();
  return {
    x: (rect.left + rect.width * 0.5) * ratio,
    y: (rect.top + rect.height * 0.5) * ratio,
  };
}

export function triggerButterflyBurst(anchorEl: HTMLElement | null): void {
  if (prefersReducedMotion() || !activeContainer) {
    return;
  }
  const container = activeContainer;
  const ratio = container.retina.pixelRatio || 1;
  const center = resolveAnchorCanvasCenter(anchorEl, container);
  const totalCount = 16;

  for (let index = 0; index < totalCount; index += 1) {
    const isButterfly = index % 3 !== 2;
    const angleDeg = (360 / totalCount) * index + (Math.random() * 18 - 9);
    const jitterX = (Math.random() - 0.5) * 28 * ratio;
    const jitterY = (Math.random() - 0.5) * 14 * ratio;

    container.particles.addParticle(
      {
        x: center.x + jitterX,
        y: center.y + jitterY,
      },
      {
        shape: {
          type: "image",
          options: {
            image: {
              src: isButterfly ? BUTTERFLY_SPRITE_URL : PETAL_SPRITE_URL,
              width: 128,
              height: 128,
              replaceColor: false,
            },
          },
        },
        size: {
          value: isButterfly ? { min: 13, max: 21 } : { min: 9, max: 14 },
        },
        opacity: {
          value: { min: 0.72, max: 0.96 },
          animation: {
            enable: true,
            speed: 1.9,
            startValue: "max",
            destroy: "min",
            sync: false,
          },
        },
        rotate: {
          value: { min: -28, max: 28 },
          direction: index % 2 === 0 ? "clockwise" : "counter-clockwise",
          animation: {
            enable: true,
            speed: isButterfly ? 22 : 34,
            sync: false,
          },
        },
        move: {
          enable: true,
          direction: angleDeg,
          speed: isButterfly ? { min: 4.2, max: 8.4 } : { min: 3.4, max: 6.8 },
          decay: 0.032,
          gravity: {
            enable: true,
            acceleration: -1.6,
          },
          outModes: {
            default: "destroy",
          },
        },
        life: {
          count: 1,
          duration: {
            value: { min: 0.44, max: 0.68 },
          },
        },
      }
    );
  }
}

export function triggerStickerStardust(anchorEl: HTMLElement | null): void {
  if (prefersReducedMotion() || !activeContainer) {
    return;
  }
  const now = performance.now();
  if (now - lastStardustAt < 320) {
    return;
  }
  lastStardustAt = now;

  const container = activeContainer;
  const ratio = container.retina.pixelRatio || 1;
  const center = resolveAnchorCanvasCenter(anchorEl, container);
  const count = 4;

  for (let index = 0; index < count; index += 1) {
    const useButterfly = index % 2 === 0;
    const offsetX = (Math.random() - 0.5) * 44 * ratio;
    const offsetY = (Math.random() - 0.5) * 32 * ratio;
    const angleDeg = -135 + index * 30 + (Math.random() * 14 - 7);

    container.particles.addParticle(
      {
        x: center.x + offsetX,
        y: center.y + offsetY,
      },
      {
        shape: {
          type: "image",
          options: {
            image: {
              src: useButterfly ? BUTTERFLY_SPRITE_URL : PETAL_SPRITE_URL,
              width: 128,
              height: 128,
              replaceColor: false,
            },
          },
        },
        size: {
          value: useButterfly ? { min: 8, max: 12 } : { min: 7, max: 10 },
        },
        opacity: {
          value: { min: 0.75, max: 0.98 },
          animation: {
            enable: true,
            speed: 2.1,
            startValue: "max",
            destroy: "min",
            sync: false,
          },
        },
        rotate: {
          value: { min: 0, max: 360 },
          direction: "random",
          animation: {
            enable: true,
            speed: 28,
            sync: false,
          },
        },
        move: {
          enable: true,
          direction: angleDeg,
          speed: { min: 1.4, max: 3.2 },
          decay: 0.035,
          outModes: {
            default: "destroy",
          },
        },
        life: {
          count: 1,
          duration: {
            value: { min: 0.45, max: 0.7 },
          },
        },
      }
    );
  }
}

export function ButterflyBurstFX() {
  const hostRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (prefersReducedMotion()) {
      return;
    }
    let cancelled = false;
    let mountedContainer: Container | undefined;

    const preloadImages = [BUTTERFLY_SPRITE_URL, PETAL_SPRITE_URL].map((src) => {
      const img = new Image();
      img.decoding = "async";
      img.src = src;
      return img;
    });
    void preloadImages;

    void (async () => {
      try {
        await ensureParticlesEngine();
        if (cancelled || !hostRef.current) {
          return;
        }
        mountedContainer = await tsParticles.load({
          id: "castorice-burst-fx",
          element: hostRef.current,
          options: {
            fullScreen: { enable: false },
            fpsLimit: 60,
            detectRetina: true,
            pauseOnBlur: true,
            background: { color: { value: "transparent" } },
            particles: {
              number: { value: 0 },
              shape: {
                type: "image",
                options: {
                  image: [
                    {
                      src: BUTTERFLY_SPRITE_URL,
                      width: 128,
                      height: 128,
                      replaceColor: false,
                    },
                    {
                      src: PETAL_SPRITE_URL,
                      width: 128,
                      height: 128,
                      replaceColor: false,
                    },
                  ],
                },
              },
            },
            interactivity: {
              events: {
                resize: { enable: true },
              },
            },
          },
        });
        if (cancelled) {
          mountedContainer?.destroy();
          return;
        }
        activeContainer = mountedContainer ?? null;
      } catch {
        activeContainer = null;
      }
    })();

    return () => {
      cancelled = true;
      if (mountedContainer) {
        if (activeContainer === mountedContainer) {
          activeContainer = null;
        }
        mountedContainer.destroy();
      }
    };
  }, []);

  return <div ref={hostRef} className="castorice-burst-layer" aria-hidden="true" />;
}
