import { computed, nextTick, ref } from 'vue'

const STORAGE_KEY = 'phdl-theme'
const THEME_COLORS = {
  light: '#f8f8fa',
  dark: '#09090b',
}

function getSystemTheme() {
  if (typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: dark)').matches) {
    return 'dark'
  }
  return 'light'
}

function readStoredOverride() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved === 'dark' || saved === 'light') return saved
  } catch {}
  return ''
}

function detectInitialTheme() {
  const override = readStoredOverride()
  if (override) return override
  if (typeof document !== 'undefined' && document.documentElement.classList.contains('dark')) {
    return 'dark'
  }
  return getSystemTheme()
}

const theme = ref(detectInitialTheme())
let activeTransition = null
let fallbackTimer = null

function syncThemeDocumentMeta(rendered, metaScheme) {
  if (typeof document === 'undefined') return
  document.documentElement.classList.toggle('dark', rendered === 'dark')
  const schemeMeta = document.querySelector('meta[name="color-scheme"]')
  if (schemeMeta) {
    schemeMeta.content = metaScheme
  }
  const themeMeta = document.querySelector('meta[name="theme-color"]')
  if (themeMeta) {
    themeMeta.content = THEME_COLORS[rendered] || THEME_COLORS.light
  }
}

function applyTheme(next, { persist = true } = {}) {
  const normalized = next === 'dark' ? 'dark' : 'light'
  theme.value = normalized

  let metaScheme = normalized
  if (persist) {
    const systemPref = getSystemTheme()
    if (normalized === systemPref) {
      metaScheme = 'light dark'
      try {
        localStorage.removeItem(STORAGE_KEY)
      } catch {}
    } else {
      try {
        localStorage.setItem(STORAGE_KEY, normalized)
      } catch {}
    }
  } else {
    metaScheme = readStoredOverride() || 'light dark'
  }

  syncThemeDocumentMeta(normalized, metaScheme)
}

if (typeof window !== 'undefined') {
  window.addEventListener('storage', (event) => {
    if (event.key !== STORAGE_KEY) return
    const override = event.newValue === 'dark' || event.newValue === 'light' ? event.newValue : ''
    const nextRendered = override || getSystemTheme()
    theme.value = nextRendered
    syncThemeDocumentMeta(nextRendered, override || 'light dark')
  })

  const darkMq = window.matchMedia?.('(prefers-color-scheme: dark)')
  darkMq?.addEventListener?.('change', (event) => {
    // Only follow OS changes when no manual override is stored (never retroactively clear a pinned override).
    if (readStoredOverride()) return
    const nextRendered = event.matches ? 'dark' : 'light'
    theme.value = nextRendered
    syncThemeDocumentMeta(nextRendered, 'light dark')
  })
}

function resolveOrigin(event) {
  if (typeof window === 'undefined') return { x: 0, y: 0 }
  const target = event?.currentTarget
  if (target && typeof target.getBoundingClientRect === 'function') {
    const rect = target.getBoundingClientRect()
    if (rect.width > 0 || rect.height > 0) {
      return {
        x: rect.left + rect.width / 2,
        y: rect.top + rect.height / 2,
      }
    }
  }
  if (typeof event?.clientX === 'number' && typeof event?.clientY === 'number' && (event.clientX || event.clientY)) {
    return { x: event.clientX, y: event.clientY }
  }
  return { x: window.innerWidth - 40, y: 28 }
}

function transitionTheme(next, event) {
  const normalized = next === 'dark' ? 'dark' : 'light'
  if (normalized === theme.value) return

  if (typeof document === 'undefined' || typeof window === 'undefined') {
    applyTheme(normalized)
    return
  }

  const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  if (reducedMotion) {
    applyTheme(normalized)
    return
  }

  if (typeof document.startViewTransition !== 'function') {
    const root = document.documentElement
    root.classList.add('theme-fallback-transition')
    applyTheme(normalized)
    clearTimeout(fallbackTimer)
    fallbackTimer = setTimeout(() => {
      root.classList.remove('theme-fallback-transition')
      fallbackTimer = null
    }, 320)
    return
  }

  if (activeTransition) {
    try {
      activeTransition.skipTransition?.()
    } catch {}
    activeTransition = null
  }

  const root = document.documentElement
  const { x, y } = resolveOrigin(event)
  const endRadius = Math.hypot(
    Math.max(x, window.innerWidth - x),
    Math.max(y, window.innerHeight - y)
  )

  root.classList.add('theme-view-transition')
  let transition
  try {
    transition = document.startViewTransition(async () => {
      applyTheme(normalized)
      await nextTick()
      // Flush styles synchronously so the new view-transition snapshot captures final theme tokens.
      void root.offsetHeight
    })
  } catch {
    root.classList.remove('theme-view-transition')
    applyTheme(normalized)
    return
  }

  activeTransition = transition

  transition.ready
    .then(() => {
      root.classList.remove('theme-view-transition')
      root.animate(
        {
          clipPath: [
            `circle(0px at ${x}px ${y}px)`,
            `circle(${endRadius}px at ${x}px ${y}px)`,
          ],
        },
        {
          duration: 460,
          easing: 'cubic-bezier(0.22, 1, 0.36, 1)',
          pseudoElement: '::view-transition-new(root)',
        }
      )
    })
    .catch(() => {
      root.classList.remove('theme-view-transition')
    })

  transition.finished
    .catch(() => {})
    .finally(() => {
      root.classList.remove('theme-view-transition')
      if (activeTransition === transition) {
        activeTransition = null
      }
    })
}

export function useTheme() {
  const isDark = computed(() => theme.value === 'dark')

  function setTheme(next, event) {
    transitionTheme(next, event)
  }

  function toggleTheme(event) {
    transitionTheme(theme.value === 'dark' ? 'light' : 'dark', event)
  }

  return {
    theme,
    isDark,
    setTheme,
    toggleTheme,
  }
}
