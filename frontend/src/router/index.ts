import { createRouter, createWebHistory, type RouteLocationNormalized, type RouteLocationRaw, type RouteRecordRaw } from 'vue-router'
import { useAppStore } from '../stores/app'
import { SESSION_LOGOUT_KEY } from '../stores/sessionCache'

import LoginView from '../views/LoginView.vue'
import DownloadView from '../views/DownloadView.vue'
import JobsView from '../views/JobsView.vue'
import TargetsView from '../views/TargetsView.vue'
import HistoryView from '../views/HistoryView.vue'
import LogsView from '../views/LogsView.vue'
import SettingsView from '../views/SettingsView.vue'

declare module 'vue-router' {
  interface RouteMeta {
    public?: boolean
  }
}

const routes: RouteRecordRaw[] = [
  { path: '/login', name: 'login', component: LoginView, meta: { public: true } },
  { path: '/', name: 'download', component: DownloadView },
  { path: '/jobs', name: 'jobs', component: JobsView },
  { path: '/targets', name: 'targets', component: TargetsView },
  { path: '/history', name: 'history', component: HistoryView },
  { path: '/logs', name: 'logs', component: LogsView },
  { path: '/settings', name: 'settings', component: SettingsView },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

let watchingLogout = false

async function resolveAsyncGuard(
  store: ReturnType<typeof useAppStore>,
  to: RouteLocationNormalized,
): Promise<true | RouteLocationRaw> {
  if (!store.authChecked) {
    await store.checkAuth(to.meta.public ? null : to.name)
  }
  if (!to.meta.public && !store.authenticated) {
    return { name: 'login', query: { next: to.fullPath } }
  }
  if (to.name === 'login' && store.authenticated) {
    return { name: 'download' }
  }
  if (!to.meta.public && store.authenticated) {
    await store.ensureRouteData(to.name)
  }
  return true
}

router.beforeEach((to) => {
  const store = useAppStore()
  if (!watchingLogout && typeof window !== 'undefined') {
    watchingLogout = true
    window.addEventListener('storage', (event) => {
      if (event.key !== SESSION_LOGOUT_KEY || !store.handleCrossTabLogout(Number(event.newValue))) return
      if (router.currentRoute.value.name !== 'login') void router.replace({ name: 'login' })
    })
  }
  // A same-tab reload can paint cached data or the route's loading state while auth is checked.
  if (store.hydratedFromCache && !store.authChecked && !to.meta.public) {
    void store.revalidateSession(to.name).then((ok) => {
      const current = router.currentRoute.value
      if (!ok && current.name !== 'login') {
        return router.replace({ name: 'login', query: { next: current.name ? current.fullPath : to.fullPath } })
      }
      if (ok && current.name && current.name !== to.name && !current.meta.public) {
        return store.ensureRouteData(current.name)
      }
    }).catch(() => {
      // Route components can retry their data requests after an interrupted navigation.
    })
    return true
  }
  if (store.authChecked && !to.meta.public && store.authenticated && store.hasRouteCache(to.name)) {
    return true
  }
  return resolveAsyncGuard(store, to)
})

export default router
