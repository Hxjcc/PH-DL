import { ref, type Ref } from 'vue'

export function useQuickScrollReady(rail: Ref<HTMLElement | null>, measure: () => void) {
  const quickScrollReady = ref(false)
  let frame: number | undefined
  let generation = 0

  function resetQuickScrollReady() {
    generation += 1
    quickScrollReady.value = false
    if (frame !== undefined) cancelAnimationFrame(frame)
    frame = undefined
  }

  function measureQuickScroll() {
    resetQuickScrollReady()
    measure()
    if (!rail.value) return

    const current = generation
    function settle() {
      if (current !== generation || !rail.value) return
      frame = requestAnimationFrame(() => {
        measure()
        frame = requestAnimationFrame(() => {
          frame = undefined
          quickScrollReady.value = true
        })
      })
    }

    // The arrows are already measured. Let any font-driven layout changes settle before animating.
    const fonts = document.fonts
    if (fonts?.status === 'loading') void fonts.ready.then(settle)
    else settle()
  }

  return { quickScrollReady, measureQuickScroll, resetQuickScrollReady }
}
