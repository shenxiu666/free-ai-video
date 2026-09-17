<template>
  <div class="glassbar toolbar" :class="['glassbar--' + position]">
    <slot />
  </div>
</template>

<script setup lang="ts">
withDefaults(
  defineProps<{
    /** top: sticky top bar / bottom: sticky bottom bar. Content scrolls underneath. */
    position?: 'top' | 'bottom'
  }>(),
  { position: 'top' }
)
</script>

<style scoped>
.glassbar {
  position: sticky;
  z-index: 40;
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: calc(0.6rem + env(safe-area-inset-top, 0px)) 1rem 0.6rem;
  /* transform/opacity only for any motion; never layout props */
  will-change: transform, opacity;
}

.glassbar--top {
  top: 0;
}

.glassbar--bottom {
  bottom: 0;
  padding-top: 0.6rem;
  padding-bottom: calc(0.6rem + env(safe-area-inset-bottom, 0px));
}
</style>
