import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import { useAppStore } from './stores/app'
import { setUnauthorizedHandler } from './api/client'
import { useAuthTransition } from './composables/useAuthTransition'
import { isNewBrowserEntry, markBrowserEntry } from './utils/browserEntry'
import '@fontsource-variable/inter'
import '@fontsource-variable/noto-sans-sc'
import './style.css'

const app = createApp(App)
const pinia = createPinia()
app.use(pinia)
app.use(router)

const store = useAppStore(pinia)
const { runAuthTransition } = useAuthTransition()
setUnauthorizedHandler(() => {
  store.handleUnauthorized()
  if (router.currentRoute.value.name !== 'login') {
    router.push({ name: 'login' })
  }
})

router.isReady().then(async () => {
  const route = router.currentRoute.value
  if (store.authenticated && !route.meta.public) {
    const showEntry = isNewBrowserEntry()
    // The same-tab fast path may have mounted from cached auth. Check with the
    // server before announcing entry if this is a new browser session.
    if (showEntry && !store.authChecked && !(await store.revalidateSession(route.name))) {
      await router.replace({ name: 'login', query: { next: route.fullPath } })
    }
    if (store.authenticated && !router.currentRoute.value.meta.public) {
      const first = showEntry && isNewBrowserEntry()
      markBrowserEntry()
      if (first) void runAuthTransition('entry')
    }
  }
  app.mount('#app')
})
