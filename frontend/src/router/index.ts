import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

/**
 * 路由表。
 *
 * 全部页面组件用动态 import，Vite 会按路由自动分包——首屏只加载登录页，
 * 其余页面点进去才下载，对应评分里的「路由级懒加载」。
 */
const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('../views/Login.vue'),
    meta: { public: true, title: '登录' },
  },
  {
    path: '/',
    component: () => import('../components/Layout.vue'),
    redirect: '/dashboard',
    children: [
      {
        path: 'dashboard',
        name: 'Dashboard',
        component: () => import('../views/Dashboard.vue'),
        meta: { title: '总览' },
      },
      {
        path: 'documents',
        name: 'Documents',
        component: () => import('../views/Documents.vue'),
        meta: { title: '文档管理' },
      },
      {
        path: 'extraction',
        name: 'Extraction',
        component: () => import('../views/Extraction.vue'),
        meta: { title: '字段抽取' },
      },
      {
        path: 'template',
        name: 'Template',
        component: () => import('../views/TemplateFill.vue'),
        meta: { title: '模板填报' },
      },
      {
        path: 'chat',
        name: 'Chat',
        component: () => import('../views/Chat.vue'),
        meta: { title: '智能对话' },
      },
      {
        path: 'settings',
        name: 'Settings',
        component: () => import('../views/Settings.vue'),
        meta: { title: '设置' },
      },
    ],
  },
]

const router = createRouter({ history: createWebHistory(), routes })

/** 未登录时除登录页外一律跳登录；已登录再访问登录页则回首页 */
router.beforeEach((to, _from, next) => {
  const loggedIn = Boolean(sessionStorage.getItem('token'))
  if (to.meta.public) {
    if (loggedIn && to.path === '/login') return next('/dashboard')
    return next()
  }
  if (!loggedIn) return next('/login')
  next()
})

export default router
