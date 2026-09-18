<template>
  <div class="app-shell">
    <GlassBar position="top">
      <span class="display app-title">free-ai-video</span>
      <nav class="app-nav" aria-label="主导航">
        <RouterLink to="/series/new" class="nav-link button">新建系列</RouterLink>
        <RouterLink to="/new" class="nav-link button">单集</RouterLink>
        <RouterLink to="/shots" class="nav-link button">分镜</RouterLink>
        <RouterLink to="/queue" class="nav-link button">队列</RouterLink>
        <RouterLink to="/keys" class="nav-link button">Keys</RouterLink>
      </nav>
    </GlassBar>

    <main class="app-main">
      <RouterView />
    </main>

    <GlassBar position="bottom">
      <span class="status-dot" aria-hidden="true" />
      <span class="status-text">本地引擎就绪 · localhost</span>
    </GlassBar>
  </div>
</template>

<script setup lang="ts">
import GlassBar from './components/GlassBar.vue'
</script>

<style scoped>
.app-shell {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

.app-title {
  font-size: clamp(1.1rem, 0.9rem + 1.5vw, 1.5rem);
  line-height: 1.1;
  letter-spacing: -0.02em;
}

.app-nav {
  display: flex;
  gap: 0.25rem;
  margin-left: auto;
}

.nav-link {
  text-decoration: none;
  color: inherit;
  padding: 0.4rem 0.8rem;
  border-radius: 999px;
  font-size: 0.85rem;
  /* transform/opacity only */
  will-change: transform, opacity;
}

.nav-link.router-link-active {
  background: rgba(0, 0, 0, 0.07);
}

.app-main {
  flex: 1;
  padding: 1rem;
  max-width: 960px;
  width: 100%;
  margin: 0 auto;
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: #30d158;
  will-change: transform, opacity;
}

.status-text {
  font-size: 0.8rem;
  opacity: 0.75;
}
</style>
