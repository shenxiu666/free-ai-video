import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

const lazyPlaceholder = () => import('./views/Placeholder.vue')

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/new' },
  { path: '/new', name: 'new', component: () => import('./views/NewDrama.vue'), meta: { title: '新建短剧' } },
  { path: '/shots', name: 'shots', component: () => import('./views/Shots.vue'), meta: { title: '分镜' } },
  { path: '/queue', name: 'queue', component: () => import('./views/Queue.vue'), meta: { title: '队列' } },
  { path: '/keys', name: 'keys', component: () => import('./views/Keys.vue'), meta: { title: 'Keys' } },
  // Fallback to placeholder so unknown routes never crash (B/C will add real views later)
  { path: '/:pathMatch(.*)*', name: 'not-found', component: lazyPlaceholder, meta: { title: '占位' } }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

export default router
