import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

const lazyPlaceholder = () => import('./views/Placeholder.vue')

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/series/new' },
  { path: '/new', name: 'new', component: () => import('./views/NewDrama.vue'), meta: { title: '新建短剧（单集兼容）' } },
  { path: '/shots', name: 'shots', component: () => import('./views/Shots.vue'), meta: { title: '分镜' } },
  { path: '/queue', name: 'queue', component: () => import('./views/Queue.vue'), meta: { title: '队列' } },
  { path: '/keys', name: 'keys', component: () => import('./views/Keys.vue'), meta: { title: 'Keys' } },
  // M2 系列/角色（风格到集一级；单集 key 复用老单剧流水线）
  { path: '/series/new', name: 'series-new', component: () => import('./views/SeriesNew.vue'), meta: { title: '新建系列' } },
  { path: '/series/:id/characters', name: 'characters', component: () => import('./views/Characters.vue'), meta: { title: '角色' } },
  { path: '/series/:id', name: 'series-detail', component: () => import('./views/SeriesDetail.vue'), meta: { title: '系列详情' } },
  // Fallback to placeholder so unknown routes never crash (B/C will add real views later)
  { path: '/:pathMatch(.*)*', name: 'not-found', component: lazyPlaceholder, meta: { title: '占位' } }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

export default router
