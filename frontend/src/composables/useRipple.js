import { onBeforeUnmount, ref } from 'vue'

export function useRipple(isDisabled = () => false) {
  const ripples = ref([])
  const timers = new Set()
  let sequence = 0

  function handlePointerDown(event) {
    if (isDisabled() || event.button > 0 || !event.currentTarget) return
    const rect = event.currentTarget.getBoundingClientRect()
    const id = ++sequence
    ripples.value.push({
      id,
      x: event.clientX - rect.left,
      y: event.clientY - rect.top,
      size: Math.max(rect.width, rect.height),
    })
    const timer = setTimeout(() => {
      ripples.value = ripples.value.filter((ripple) => ripple.id !== id)
      timers.delete(timer)
    }, 520)
    timers.add(timer)
  }

  onBeforeUnmount(() => timers.forEach(clearTimeout))
  return { ripples, handlePointerDown }
}
