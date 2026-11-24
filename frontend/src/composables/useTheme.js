import { computed, ref } from 'vue'

const STORAGE_KEY = 'phdl-theme'

function detectInitialTheme() {
  if (typeof document !== 'undefined' && document.documentElement.classList.contains('dark')) {
    return 'dark'
  }
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved === 'dark' || saved === 'light') return saved
    if (typeof window !== 'undefined' && window.matchMedia('(prefers-color-scheme: dark)').matches) {
      return 'dark'
    }
  } catch {}
  return 'light'
}

const theme = ref(detectInitialTheme())

function applyTheme(next) {
  theme.value = next
  if (typeof document !== 'undefined') {
    document.documentElement.classList.toggle('dark', next === 'dark')
    document.documentElement.style.colorScheme = next
  }
  try {
    localStorage.setItem(STORAGE_KEY, next)
  } catch {}
}

export function useTheme() {
  const isDark = computed(() => theme.value === 'dark')

  function setTheme(next) {
    applyTheme(next === 'dark' ? 'dark' : 'light')
  }

  function toggleTheme() {
    applyTheme(theme.value === 'dark' ? 'light' : 'dark')
  }

  return {
    theme,
    isDark,
    setTheme,
    toggleTheme,
  }
}
