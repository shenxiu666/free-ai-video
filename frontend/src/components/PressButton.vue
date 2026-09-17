<template>
  <button
    class="press-btn button"
    :class="{ 'is-pressed': pressed, 'is-disabled': disabled }"
    :disabled="disabled"
    :aria-disabled="disabled"
    type="button"
    @pointerdown="onDown"
    @pointerup="onUp"
    @pointercancel="onCancel"
    @pointerleave="onLeave"
    @keydown.enter="onKeyCommit"
    @keydown.space.prevent="onKeyCommit"
  >
    <slot />
  </button>
</template>

<script setup lang="ts">
import { ref } from 'vue'

const props = withDefaults(defineProps<{ disabled?: boolean }>(), { disabled: false })
const emit = defineEmits<{ (e: 'click', ev: PointerEvent | KeyboardEvent): void }>()

const pressed = ref(false)

function onDown() {
  if (props.disabled) return
  // Immediate highlight: scale .97 via class, no waiting for click
  pressed.value = true
}

function commit(e: PointerEvent | KeyboardEvent) {
  if (props.disabled) return
  emit('click', e)
}

function onUp(e: PointerEvent) {
  if (props.disabled) return
  if (pressed.value) commit(e)
  pressed.value = false
}

function onCancel() {
  pressed.value = false
}

function onLeave() {
  // Pointer left while held: drop highlight but do not commit
  pressed.value = false
}

function onKeyCommit(e: KeyboardEvent) {
  commit(e)
}
</script>

<style scoped>
.press-btn {
  appearance: none;
  border: 1px solid rgba(0, 0, 0, 0.08);
  background: #0071e3;
  color: #fff;
  border-radius: 999px;
  padding: 0.5rem 1.1rem;
  font-size: 0.9rem;
  font-weight: 600;
  cursor: pointer;
  /* transform/opacity only, 100ms */
  transition: transform 100ms ease, opacity 100ms ease;
  will-change: transform;
  touch-action: manipulation;
  user-select: none;
  -webkit-user-select: none;
}

.press-btn.is-pressed {
  transform: scale(0.97);
  opacity: 0.92;
}

.press-btn.is-disabled,
.press-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
  transform: none;
}
</style>
