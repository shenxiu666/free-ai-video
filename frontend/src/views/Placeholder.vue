<template>
  <div class="placeholder">
    <h1 class="display">{{ title }}</h1>
    <p>占位页面：真实视图由后续模块覆盖，本页仅保证路由不报错。</p>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const title = computed(() => (route.meta.title as string) || (route.path as string) || '占位')
</script>

<style scoped>
.placeholder {
  padding: 2rem 0;
}
.placeholder p {
  opacity: 0.7;
}
</style>
