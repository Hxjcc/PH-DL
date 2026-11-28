<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = withDefaults(defineProps<{
  open?: boolean
  contentKey?: string | number | boolean
}>(), {
  open: false,
  contentKey: '',
})

const emit = defineEmits<{ 'after-enter': []; 'after-leave': []; 'after-resize': [] }>()

type Phase = 'enter' | 'leave' | 'resize'

const ENTER_DURATION_MS = 300
const LEAVE_DURATION_MS = 260
const RESIZE_DURATION_MS = 260

const supportsIntrinsicSize =
  typeof CSS !== 'undefined' &&
  typeof CSS.supports === 'function' &&
  CSS.supports('interpolate-size', 'allow-keywords')

const wrapper = ref<HTMLElement | null>(null)
const content = ref<HTMLElement | null>(null)

let fallbackTimer: ReturnType<typeof setTimeout> | null = null
let rafId: number | null = null
let animToken = 0
let activePhase: Phase | null = null

function prefersReducedMotion() {
  return typeof window !== 'undefined' && Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches)
}

function isMotionDisabled() {
  return prefersReducedMotion() || (typeof document !== 'undefined' && document.hidden)
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

function clearInlineOverrides() {
  const el = wrapper.value
  if (!el) return
  el.style.transition = ''
  el.style.blockSize = ''
  el.style.opacity = ''
}

function applySettledState(isOpen: boolean) {
  const el = wrapper.value
  if (!el) return
  if (supportsIntrinsicSize) {
    clearInlineOverrides()
    return
  }
  el.style.transition = 'none'
  el.style.blockSize = isOpen ? 'auto' : '0px'
  el.style.opacity = isOpen ? '1' : '0'
}

function measureContentHeight() {
  const inner = content.value
  if (!inner) return 0
  const rectH = inner.getBoundingClientRect().height
  return Math.ceil(Math.max(rectH, inner.scrollHeight || 0))
}

function emitSettled(phase: Phase) {
  if (phase === 'enter') emit('after-enter')
  else if (phase === 'leave') emit('after-leave')
  else emit('after-resize')
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
    emitSettled(phase)
  }
}

function fallbackDelayFor(phase: Phase) {
  if (phase === 'enter') return ENTER_DURATION_MS + 80
  if (phase === 'leave') return LEAVE_DURATION_MS + 80
  return RESIZE_DURATION_MS + 80
}

function animateHeight(fromPx: number, toPx: number, phase: Phase, fromOpacity: number, toOpacity: number) {
  const el = wrapper.value
  if (!el) return
  clearPending()
  const token = ++animToken

  const skipAnimation = isMotionDisabled() || Math.abs(toPx - fromPx) < 1

  if (skipAnimation) {
    activePhase = null
    applySettledState(phase !== 'leave')
    emitSettled(phase)
    return
  }

  activePhase = phase
  el.style.transition = 'none'
  el.style.blockSize = `${fromPx}px`
  el.style.opacity = String(fromOpacity)

  const startTransition = () => {
    if (token !== animToken || !wrapper.value) return
    wrapper.value.style.transition = ''
    // When native intrinsic interpolation is supported, release to `auto` on enter/resize so
    // the browser interpolates cleanly to the natural block-size without a second layout lock.
    wrapper.value.style.blockSize =
      supportsIntrinsicSize && phase !== 'leave' ? '' : `${toPx}px`
    wrapper.value.style.opacity = supportsIntrinsicSize ? '' : String(toOpacity)
    clearFallback()
    fallbackTimer = setTimeout(() => finishTransition(token, phase), fallbackDelayFor(phase))
  }

  // Commit the initial fixed block-size cleanly, then start the transition on the next VSync frame.
  void el.offsetHeight
  if (typeof requestAnimationFrame === 'function') {
    fallbackTimer = setTimeout(() => finishTransition(token, phase), fallbackDelayFor(phase) + 60)
    rafId = requestAnimationFrame(() => {
      rafId = null
      startTransition()
    })
  } else {
    startTransition()
  }
}

function onTransitionEnd(e: TransitionEvent) {
  if (
    e.target !== wrapper.value ||
    (e.propertyName !== 'block-size' && e.propertyName !== 'height') ||
    !activePhase
  ) {
    return
  }
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
      if (supportsIntrinsicSize) {
        clearPending()
        const token = ++animToken
        const phase: Phase = isOpen ? 'enter' : 'leave'
        clearInlineOverrides()
        if (isMotionDisabled()) {
          activePhase = null
          emitSettled(phase)
          return
        }
        activePhase = phase
        fallbackTimer = setTimeout(() => finishTransition(token, phase), fallbackDelayFor(phase))
        return
      }

      if (isOpen) {
        const startHeight = Math.ceil(el.getBoundingClientRect().height)
        const startOpacity = startHeight > 0 ? Number(getComputedStyle(el).opacity || 1) : 0
        // Lock current height before Vue mounts/patches slot content so the frame never jumps to auto.
        el.style.transition = 'none'
        el.style.blockSize = `${startHeight}px`
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
    // Freeze block-size before slot content swaps so block-size:auto never flashes the new size for 1 frame.
    el.style.transition = 'none'
    el.style.blockSize = `${startHeight}px`
    el.style.opacity = String(currentOpacity)
    await nextTick()
    if (!props.open) return
    const targetHeight = measureContentHeight()
    const resizePhase: Phase = activePhase === 'enter' ? 'enter' : 'resize'
    if (Math.abs(targetHeight - startHeight) >= 1) {
      animateHeight(startHeight, targetHeight, resizePhase, currentOpacity, 1)
    } else if (!activePhase) {
      applySettledState(true)
      emitSettled(resizePhase)
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
    :class="['animated-collapse', { 'is-open': open }]"
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
  interpolate-size: allow-keywords;
  block-size: 0;
  opacity: 0;
  overflow: clip;
  contain: layout style;
  transition:
    block-size 260ms cubic-bezier(0.28, 0.12, 0.2, 1),
    opacity 200ms cubic-bezier(0.28, 0.12, 0.2, 1);
}

.animated-collapse.is-open {
  block-size: auto;
  opacity: 1;
  transition:
    block-size 300ms cubic-bezier(0.28, 0.12, 0.2, 1),
    opacity 240ms cubic-bezier(0.28, 0.12, 0.2, 1);
}

.collapse-content {
  display: flow-root;
  transform: translateY(-6px);
  transition: transform 260ms cubic-bezier(0.28, 0.12, 0.2, 1);
}

.animated-collapse.is-open > .collapse-content {
  transform: translateY(0);
  transition: transform 300ms cubic-bezier(0.28, 0.12, 0.2, 1);
}

@media (prefers-reduced-motion: reduce) {
  .animated-collapse,
  .animated-collapse.is-open,
  .collapse-content,
  .animated-collapse.is-open > .collapse-content {
    transition: none;
    transform: none;
  }
}
</style>
