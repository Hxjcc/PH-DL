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

function markAuthenticatedEntry() {
  if (!store.authenticated || router.currentRoute.value.meta.public) return
  const first = isNewBrowserEntry()
  markBrowserEntry()
  if (first) void runAuthTransition('entry')
}

router.isReady().then(() => {
  const route = router.currentRoute.value
  if (store.authenticated && !route.meta.public) {
    if (isNewBrowserEntry() && !store.authChecked) {
      void store.revalidateSession(route.name).then((ok) => {
        if (ok) markAuthenticatedEntry()
      })
    } else {
      markAuthenticatedEntry()
    }
  }
  app.mount('#app')
})
