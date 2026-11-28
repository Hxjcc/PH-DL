<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = withDefaults(defineProps<{
  open?: boolean
  contentKey?: string | number | boolean
}>(), {
  open: false,
  contentKey: '',
})

const emit = defineEmits<{ 'after-enter': []; 'after-leave': [] }>()

type Phase = 'enter' | 'leave' | 'resize'

const wrapper = ref<HTMLElement | null>(null)
const content = ref<HTMLElement | null>(null)

let fallbackTimer: ReturnType<typeof setTimeout> | null = null
let rafId: number | null = null
let animToken = 0
let activePhase: Phase | null = null

function prefersReducedMotion() {
  return typeof window !== 'undefined' && Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches)
}

function clearFallback() {
  if (fallbackTimer) {
    clearTimeout(fallbackTimer)
    fallbackTimer = null
  }
}

function clearRafs() {
  if (typeof cancelAnimationFrame === 'function') {
    if (rafId !== null) {
      cancelAnimationFrame(rafId)
      rafId = null
    }
  } else {
    rafId = null
  }
}

function clearPending() {
  clearFallback()
  clearRafs()
}

function applySettledState(isOpen: boolean) {
  const el = wrapper.value
  if (!el) return
  el.style.transition = 'none'
  el.style.height = isOpen ? 'auto' : '0px'
  el.style.opacity = isOpen ? '1' : '0'
}

function measureContentHeight() {
  const inner = content.value
  if (!inner) return 0
  const rectH = inner.getBoundingClientRect().height
  return Math.ceil(Math.max(rectH, inner.scrollHeight || 0))
}

function finishTransition(token: number, phase: Phase) {
  if (token !== animToken) return
  clearPending()
  activePhase = null
  if (phase === 'leave') {
    if (!props.open) {
      applySettledState(false)
      emit('after-leave')
    }
  } else if (props.open) {
    applySettledState(true)
    if (phase === 'enter') emit('after-enter')
  }
}

function animateHeight(fromPx: number, toPx: number, phase: Phase, fromOpacity: number, toOpacity: number) {
  const el = wrapper.value
  if (!el) return
  clearPending()
  const token = ++animToken

  const skipAnimation =
    prefersReducedMotion() ||
    Math.abs(toPx - fromPx) < 1 ||
    (typeof document !== 'undefined' && document.hidden)

  if (skipAnimation) {
    activePhase = null
    applySettledState(phase !== 'leave')
    if (phase === 'enter') emit('after-enter')
    else if (phase === 'leave') emit('after-leave')
    return
  }

  activePhase = phase
  el.style.transition = 'none'
  el.style.height = `${fromPx}px`
  el.style.opacity = String(fromOpacity)

  const startTransition = () => {
    if (token !== animToken || !wrapper.value) return
    wrapper.value.style.transition = ''
    wrapper.value.style.height = `${toPx}px`
    wrapper.value.style.opacity = String(toOpacity)
    clearFallback()
    fallbackTimer = setTimeout(() => finishTransition(token, phase), 260)
  }

  // Commit the initial fixed height cleanly, then start the transition on the next VSync frame.
  void el.offsetHeight
  if (typeof requestAnimationFrame === 'function') {
    fallbackTimer = setTimeout(() => finishTransition(token, phase), 320)
    rafId = requestAnimationFrame(() => {
      rafId = null
      startTransition()
    })
  } else {
    startTransition()
  }
}

function onTransitionEnd(e: TransitionEvent) {
  if (e.target !== wrapper.value || e.propertyName !== 'height' || !activePhase) return
  finishTransition(animToken, activePhase)
}

onMounted(() => {
  applySettledState(props.open)
})

watch(
  [() => props.open, () => props.contentKey],
  async ([isOpen], [wasOpen]) => {
    const el = wrapper.value
    if (!el) return
    if (isOpen !== wasOpen) {
      if (isOpen) {
        const startHeight = Math.ceil(el.getBoundingClientRect().height)
        const startOpacity = startHeight > 0 ? Number(getComputedStyle(el).opacity || 1) : 0
        // Lock current height before Vue mounts/patches slot content so the frame never jumps to auto.
        el.style.transition = 'none'
        el.style.height = `${startHeight}px`
        el.style.opacity = String(startOpacity)
        await nextTick()
        if (!props.open) return
        const targetHeight = measureContentHeight()
        animateHeight(startHeight, targetHeight, 'enter', startOpacity, 1)
      } else {
        const startHeight = Math.ceil(el.getBoundingClientRect().height)
        const startOpacity = activePhase ? Number(getComputedStyle(el).opacity || 1) : 1
        animateHeight(startHeight, 0, 'leave', startOpacity, 0)
      }
      return
    }
    if (!isOpen) return
    const startHeight = Math.ceil(el.getBoundingClientRect().height)
    const currentOpacity = activePhase ? Number(getComputedStyle(el).opacity || 1) : 1
    // Freeze height before slot content swaps so height:auto never flashes the new size for 1 frame.
    el.style.transition = 'none'
    el.style.height = `${startHeight}px`
    el.style.opacity = String(currentOpacity)
    await nextTick()
    if (!props.open) return
    const targetHeight = measureContentHeight()
    if (Math.abs(targetHeight - startHeight) >= 1) {
      animateHeight(startHeight, targetHeight, activePhase === 'enter' ? 'enter' : 'resize', currentOpacity, 1)
    } else if (!activePhase) {
      applySettledState(true)
    }
  }
)

onBeforeUnmount(() => {
  animToken += 1
  clearPending()
})
</script>

<template>
  <div
    ref="wrapper"
    class="animated-collapse"
    :inert="!open"
    :aria-hidden="!open"
    @transitionend="onTransitionEnd"
  >
    <div ref="content" class="collapse-content">
      <slot />
    </div>
  </div>
</template>

<style scoped>
.animated-collapse {
  height: 0;
  opacity: 0;
  overflow: hidden;
  contain: layout style paint;
  transition:
    height 200ms cubic-bezier(0.22, 1, 0.36, 1),
    opacity 160ms cubic-bezier(0.22, 1, 0.36, 1);
}

.collapse-content {
  display: flow-root;
}

@media (prefers-reduced-motion: reduce) {
  .animated-collapse {
    transition: none;
  }
}
</style>
