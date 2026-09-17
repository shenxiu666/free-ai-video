<template>
  <div v-if="open || rendered" class="sheet-root" :aria-hidden="!open">
    <div class="sheet-backdrop" :class="{ 'is-open': open }" @click="emit('update:open', false)" />
    <div
      ref="surface"
      class="sheet toolbar"
      role="dialog"
      aria-modal="false"
      :style="{ transform: `translateY(${currentY}px)`, opacity: open ? 1 : 0 }"
    >
      <div
        ref="handle"
        class="sheet-handle"
        @pointerdown="onDragStart"
        @pointermove="onDragMove"
        @pointerup="onDragEnd"
        @pointercancel="onDragEnd"
      >
        <span class="sheet-grabber" />
      </div>
      <div class="sheet-content">
        <slot />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import type { AnimationPlaybackControls } from 'motion'
import { springTo, project, nearestSnap, rubberband } from '../motion/springs'

const props = withDefaults(
  defineProps<{
    open: boolean
    /** translateY snap points in px. 0 = fully expanded, larger = more collapsed. */
    snapPoints?: number[]
  }>(),
  { snapPoints: () => [0] }
)

const emit = defineEmits<{
  (e: 'update:open', v: boolean): void
  (e: 'opened'): void
  (e: 'closed'): void
}>()

const surface = ref<HTMLElement | null>(null)
const handle = ref<HTMLElement | null>(null)
const rendered = ref(false)
const currentY = ref(0)

let controls: AnimationPlaybackControls | null = null
let dragging = false
let armed = false
let startClientY = 0
let baseY = 0
let history: Array<{ y: number; t: number }> = []

const HIDDEN_Y = 480

function snaps(): number[] {
  const s = [...props.snapPoints].sort((a, b) => a - b)
  return s.length ? s : [0]
}

function stopAnim() {
  try {
    controls?.stop()
  } catch {
    /* noop */
  }
  controls = null
}

function applyY(y: number) {
  currentY.value = y
}

function resyncFromDom() {
  const el = surface.value
  if (!el) return
  const inline = /translateY\((-?[\d.]+)px\)/.exec(el.style.transform || '')
  if (inline) {
    currentY.value = parseFloat(inline[1])
    return
  }
  const cs = getComputedStyle(el).transform
  if (cs && cs.startsWith('matrix')) {
    const parts = cs.slice(7, -1).split(',').map(Number)
    if (parts.length === 6 && Number.isFinite(parts[5])) currentY.value = parts[5]
  }
}

function goTo(target: number, velocity?: number) {
  const el = surface.value
  stopAnim()
  if (!el) {
    applyY(target)
    return
  }
  // Interruptible spring carrying release velocity (momentum -> bounce 0.2 inside springTo).
  // currentY follows animation frames (no dual-write jump): do NOT jump to target here.
  controls = springTo(
    el,
    { y: target },
    velocity !== undefined
      ? { velocity, bounce: 0.2, onUpdate: (v: unknown) => {
          const y = (v as Record<string, number>)?.['y']
          if (Number.isFinite(y)) applyY(y as number)
          else resyncFromDom()
        } }
      : { onUpdate: (v: unknown) => {
          const y = (v as Record<string, number>)?.['y']
          if (Number.isFinite(y)) applyY(y as number)
        } }
  )
}

function velocityPxPerMs(): number {
  if (history.length < 2) return 0
  const last = history[history.length - 1]
  // Use samples within last ~100ms
  let first = history[0]
  for (let i = history.length - 2; i >= 0; i--) {
    if (last.t - history[i].t > 100) break
    first = history[i]
  }
  const dt = last.t - first.t
  if (dt <= 0) return 0
  return (last.y - first.y) / dt
}

function onDragStart(e: PointerEvent) {
  if (!props.open) return
  // 先读 presentation 值再停动画，避免 cancel 回落到终点导致跳变
  resyncFromDom()
  stopAnim()
  dragging = true
  armed = false
  startClientY = e.clientY
  baseY = currentY.value
  history = [{ y: baseY, t: performance.now() }]
  try {
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  } catch {
    /* noop */
  }
}

function onDragMove(e: PointerEvent) {
  if (!dragging) return
  // ~10px hysteresis：小抖动不提交方向，保持跟手幻觉
  if (!armed && Math.abs(e.clientY - startClientY) < 10) return
  armed = true
  const raw = baseY + (e.clientY - startClientY) // 1:1 tracking
  // Rubberband past the most-expanded snap (top) and past collapsed end
  const s = snaps()
  const min = s[0]
  const max = s[s.length - 1]
  let y = raw
  if (raw < min) y = min - rubberband(min - raw, 480)
  else if (raw > max) y = max + rubberband(raw - max, 480)
  applyY(y)
  history.push({ y, t: performance.now() })
  if (history.length > 32) history.shift()
  // Never lock input: no preventDefault, content stays interactive
}

function onDragEnd(e: PointerEvent) {
  if (!dragging) return
  dragging = false
  try {
    ;(e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId)
  } catch {
    /* noop */
  }
  // 未过 hysteresis：视为 tap，不做投射
  if (!armed) return
  const v = velocityPxPerMs() // px/ms
  const s = snaps()
  const projected = currentY.value + project(v)
  let target = nearestSnap(projected, s)
  // Fast downward fling past last snap closes（带速交接，无缝）
  if (v > 0.9 && currentY.value > s[s.length - 1] - 40) {
    close(v * 1000)
    return
  }
  // Fast upward fling goes to most expanded
  if (v < -0.9) target = s[0]
  // velocity for motion spring is px/s per axis
  goTo(target, v * 1000)
}

function openTo() {
  rendered.value = true
  nextTick(() => {
    const target = snaps()[0] ?? 0
    // Start from hidden for enter animation
    if (currentY.value >= HIDDEN_Y - 1) applyY(HIDDEN_Y)
    goTo(target)
    emit('opened')
  })
}

function close(velocity?: number) {
  goTo(HIDDEN_Y, velocity)
  // 对称出场：听弹簧完成再卸载，而非固定 timeout（duration 0.4s）
  const c = controls
  if (c && typeof (c as unknown as { finished?: Promise<unknown> }).finished?.then === 'function') {
    ;(c as unknown as { finished: Promise<unknown> }).finished.then(
      () => {
        rendered.value = false
        emit('closed')
      },
      () => {
        rendered.value = false
        emit('closed')
      }
    )
  } else {
    window.setTimeout(() => {
      rendered.value = false
      emit('closed')
    }, 420)
  }
}

// Escape 关闭 + 焦点可达
function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && props.open) emit('update:open', false)
}

watch(
  () => props.open,
  (v) => {
    if (v) openTo()
    else close()
  }
)

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
  if (props.open) {
    currentY.value = HIDDEN_Y
    openTo()
  } else {
    currentY.value = HIDDEN_Y
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  stopAnim()
})
</script>

<style scoped>
.sheet-root {
  position: fixed;
  inset: 0;
  z-index: 60;
  pointer-events: none;
}

.sheet-backdrop {
  position: absolute;
  inset: 0;
  background: rgba(0, 0, 0, 0.28);
  opacity: 0;
  transition: opacity 100ms ease;
  pointer-events: none;
  will-change: opacity;
}

.sheet-backdrop.is-open {
  opacity: 1;
  pointer-events: auto;
}

.sheet {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  max-height: 86vh;
  border-radius: 16px 16px 0 0;
  display: flex;
  flex-direction: column;
  pointer-events: auto;
  /* transform/opacity only */
  will-change: transform, opacity;
}

.sheet-handle {
  display: flex;
  justify-content: center;
  padding: 10px 0 6px;
  cursor: grab;
  touch-action: none;
}

.sheet-handle:active {
  cursor: grabbing;
}

.sheet-grabber {
  width: 40px;
  height: 5px;
  border-radius: 999px;
  background: rgba(0, 0, 0, 0.25);
}

.sheet-content {
  overflow-y: auto;
  padding: 0.5rem 1rem calc(1rem + env(safe-area-inset-bottom, 0px));
  touch-action: pan-y;
  overscroll-behavior: contain;
}
</style>
