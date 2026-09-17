import { animate, type AnimationPlaybackControls } from 'motion'

export interface SpringOpts {
  /** px/s，单轴弹簧速度（Sheet 传 v*1000）。多轴时可用 {x,y}。 */
  velocity?: number | Record<string, number>
  bounce?: number
  duration?: number
  onUpdate?: (latest: Record<string, number | string>) => void
  [key: string]: unknown
}

/**
 * Spring to target vars (transform/opacity only).
 * Default bounce: 0, duration: 0.4. Momentum (velocity passed) -> bounce: 0.2.
 * velocity 为 px/s（与 project(v px/ms)*1000 同单位），motion-dom 单轴传 number。
 */
export function springTo(
  el: HTMLElement,
  vars: Record<string, number | string>,
  opts: SpringOpts = {}
): AnimationPlaybackControls {
  const { velocity, bounce, duration = 0.4, onUpdate, ...rest } = opts
  const finalBounce = bounce ?? (velocity !== undefined ? 0.2 : 0)
  type AnimateOpts = Exclude<Parameters<typeof animate>[2], undefined>
  const springOpts = {
    type: 'spring',
    bounce: finalBounce,
    duration,
    ...(velocity !== undefined ? { velocity } : {}),
    ...(onUpdate ? { onUpdate: onUpdate as AnimateOpts['onUpdate'] } : {}),
    ...rest,
  } as AnimateOpts
  return animate(el, vars as Parameters<typeof animate>[1], springOpts)
}

/**
 * Project inertial displacement (px) from release velocity v (px/ms).
 * Exponential-decay sum: v * d / (1 - d). d defaults to 0.998.
 */
export function project(v: number, d = 0.998): number {
  if (!Number.isFinite(v) || v === 0) return 0
  if (d <= 0 || d >= 1) return 0
  return (v * d) / (1 - d)
}

/** Pick the snap point closest to value. */
export function nearestSnap(value: number, snapPoints: number[]): number {
  if (snapPoints.length === 0) return value
  let best = snapPoints[0]
  let bestDist = Math.abs(value - best)
  for (let i = 1; i < snapPoints.length; i++) {
    const dist = Math.abs(value - snapPoints[i])
    if (dist < bestDist) {
      bestDist = dist
      best = snapPoints[i]
    }
  }
  return best
}

/**
 * iOS-style rubberband resistance for overscroll o (px) given dimension dim (px).
 * Small drags ~ o * c, large drags asymptote to dim.
 */
export function rubberband(o: number, dim: number, c = 0.55): number {
  if (o <= 0 || dim <= 0) return 0
  return (o * dim * c) / (dim + c * o)
}
