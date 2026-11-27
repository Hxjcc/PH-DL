import { onBeforeUnmount, ref } from 'vue'

interface Ripple {
  id: number
  x: number
  y: number
  size: number
}

export function useRipple(isDisabled: () => boolean = () => false) {
  const ripples = ref<Ripple[]>([])
  const timers = new Set<ReturnType<typeof setTimeout>>()
  let sequence = 0

  function handlePointerDown(event: PointerEvent) {
    const target = event.currentTarget as Element | null
    if (isDisabled() || event.button > 0 || !target) return
    const rect = target.getBoundingClientRect()
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
