import { createRouter, createWebHistory, type RouteLocationNormalized, type RouteLocationRaw, type RouteRecordRaw } from 'vue-router'
import { useAppStore } from '../stores/app'

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
  // Fast synchronous path on same-tab reload when sessionStorage has valid route cache:
  // mounts #app BEFORE window.load fires so Chrome Paint Holding transitions with zero white flash,
  // while revalidating auth & silently refreshing data in the background.
  if (
    store.hydratedFromCache &&
    !store.authChecked &&
    !to.meta.public &&
    store.hasRouteCache(to.name)
  ) {
    store.revalidateSession(to.name).then((ok) => {
      if (!ok && router.currentRoute.value.name !== 'login') {
        router.replace({ name: 'login', query: { next: to.fullPath } })
      }
    })
    return true
  }
  if (store.authChecked && !to.meta.public && store.authenticated && store.hasRouteCache(to.name)) {
    return true
  }
  return resolveAsyncGuard(store, to)
})

export default router
