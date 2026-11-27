import { nextTick, reactive, readonly } from 'vue'

type AuthTransitionKind = 'entry' | 'login' | 'logout'
type AuthTransitionPhase = 'cover' | 'covered' | 'reveal'

const state = reactive<{ visible: boolean; kind: AuthTransitionKind; phase: AuthTransitionPhase }>({
  visible: false,
  kind: 'login',
  phase: 'cover',
})
const wait = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))
const COVER_MS = 280
const ENTRY_HOLD_MS = 120
const REVEAL_MS = 360

export function useAuthTransition() {
  async function run(kind: AuthTransitionKind, action?: () => unknown): Promise<void> {
    if (state.visible) return
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (reduced) {
      await action?.()
      return
    }
    state.kind = kind
    // A fresh document starts behind the cover; there is no old page to cover up.
    state.phase = kind === 'entry' ? 'covered' : 'cover'
    state.visible = true
    await nextTick()
    await wait(kind === 'entry' ? ENTRY_HOLD_MS : COVER_MS)
    try {
      await action?.()
    } finally {
      await nextTick()
      state.phase = 'reveal'
      await wait(REVEAL_MS)
      state.visible = false
    }
  }
  return { authTransition: readonly(state), runAuthTransition: run }
}
