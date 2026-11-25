<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  contentKey: { type: [String, Number, Boolean], default: '' },
})

const emit = defineEmits(['after-enter', 'after-leave'])

const wrapper = ref(null)
const content = ref(null)

let fallbackTimer = null
let animToken = 0
let activePhase = null // 'enter' | 'leave' | 'resize' | null

function prefersReducedMotion() {
  return typeof window !== 'undefined' && Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches)
}

function clearFallback() {
  if (fallbackTimer) {
    clearTimeout(fallbackTimer)
    fallbackTimer = null
  }
}

function applySettledState(isOpen) {
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

function finishTransition(token, phase) {
  if (token !== animToken) return
  clearFallback()
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

function animateHeight(fromPx, toPx, phase, fromOpacity, toOpacity) {
  const el = wrapper.value
  if (!el) return
  clearFallback()
  const token = ++animToken

  if (prefersReducedMotion() || Math.abs(toPx - fromPx) < 1) {
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

  // Force style/layout commit so the browser transitions cleanly from `fromPx` to `toPx`.
  void el.offsetHeight

  el.style.transition = ''
  el.style.height = `${toPx}px`
  el.style.opacity = String(toOpacity)

  fallbackTimer = setTimeout(() => finishTransition(token, phase), 320)
}

function onTransitionEnd(e) {
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
        const startOpacity = Number(getComputedStyle(el).opacity || 1)
        animateHeight(startHeight, 0, 'leave', startOpacity, 0)
      }
      return
    }
    if (!isOpen) return
    const startHeight = Math.ceil(el.getBoundingClientRect().height)
    const currentOpacity = Number(getComputedStyle(el).opacity || 1)
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
  clearFallback()
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
  transition:
    height 260ms cubic-bezier(0.22, 1, 0.36, 1),
    opacity 200ms cubic-bezier(0.22, 1, 0.36, 1);
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
