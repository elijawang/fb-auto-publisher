import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/', redirect: '/accounts' },
  {
    path: '/accounts',
    name: 'Accounts',
    component: () => import('../views/AccountList.vue'),
    meta: { title: '账号管理' },
  },
  {
    path: '/tasks',
    name: 'Tasks',
    component: () => import('../views/TaskList.vue'),
    meta: { title: '任务管理' },
  },
  {
    path: '/tasks/create',
    name: 'TaskCreate',
    component: () => import('../views/TaskCreate.vue'),
    meta: { title: '创建任务' },
  },
  {
    path: '/logs',
    name: 'Logs',
    component: () => import('../views/LogList.vue'),
    meta: { title: '执行日志' },
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to) => {
  document.title = `${to.meta.title || ''} - FB自动发布系统`
})

export default router
