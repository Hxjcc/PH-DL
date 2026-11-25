<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

const props = defineProps({
  open: { type: Boolean, default: false },
})

const emit = defineEmits(['after-enter', 'after-leave'])

const wrapper = ref(null)
const content = ref(null)
const heightValue = ref(props.open ? 'auto' : '0px')
const transitionEnabled = ref(false)

let rafId = 0
let fallbackTimer = null
let animToken = 0
let activePhase = null

function prefersReducedMotion() {
  return typeof window !== 'undefined' && Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches)
}

function clearPending() {
  if (rafId) {
    cancelAnimationFrame(rafId)
    rafId = 0
  }
  if (fallbackTimer) {
    clearTimeout(fallbackTimer)
    fallbackTimer = null
  }
}

function finishTransition(token, opening) {
  if (token !== animToken) return
  clearPending()
  activePhase = null
  transitionEnabled.value = false
  if (opening && props.open) {
    heightValue.value = 'auto'
    emit('after-enter')
  } else if (!opening && !props.open) {
    heightValue.value = '0px'
    emit('after-leave')
  }
}

function onTransitionEnd(e) {
  if (e.target !== wrapper.value || e.propertyName !== 'height' || !activePhase) return
  finishTransition(animToken, activePhase === 'enter')
}

watch(
  () => props.open,
  async (isOpen) => {
    clearPending()
    const token = ++animToken

    if (isOpen) {
      await nextTick()
      if (token !== animToken) return
      const targetHeight = Math.ceil(content.value?.scrollHeight || 0)
      if (prefersReducedMotion() || targetHeight <= 0) {
        activePhase = null
        transitionEnabled.value = false
        heightValue.value = 'auto'
        emit('after-enter')
        return
      }

      const startHeight = Math.ceil(wrapper.value?.getBoundingClientRect().height || 0)
      if (startHeight >= targetHeight) {
        activePhase = null
        transitionEnabled.value = false
        heightValue.value = 'auto'
        emit('after-enter')
        return
      }

      activePhase = 'enter'
      transitionEnabled.value = false
      heightValue.value = `${startHeight}px`
      await nextTick()
      if (token !== animToken) return

      rafId = requestAnimationFrame(() => {
        rafId = 0
        if (token !== animToken || !props.open) return
        const measured = Math.ceil(content.value?.scrollHeight || 0)
        if (measured <= startHeight) {
          finishTransition(token, true)
          return
        }
        transitionEnabled.value = true
        heightValue.value = `${measured}px`
        fallbackTimer = setTimeout(() => finishTransition(token, true), 240)
      })
    } else {
      const startHeight = Math.ceil(wrapper.value?.getBoundingClientRect().height || 0)
      if (prefersReducedMotion() || startHeight <= 0) {
        activePhase = null
        transitionEnabled.value = false
        heightValue.value = '0px'
        emit('after-leave')
        return
      }

      activePhase = 'leave'
      transitionEnabled.value = false
      heightValue.value = `${startHeight}px`
      await nextTick()
      if (token !== animToken) return

      rafId = requestAnimationFrame(() => {
        rafId = 0
        if (token !== animToken || props.open) return
        transitionEnabled.value = true
        heightValue.value = '0px'
        fallbackTimer = setTimeout(() => finishTransition(token, false), 240)
      })
    }
  }
)

onBeforeUnmount(() => {
  animToken += 1
  clearPending()
})

const wrapperStyle = computed(() => ({
  height: heightValue.value,
}))
</script>

<template>
  <div
    ref="wrapper"
    :class="['animated-collapse', transitionEnabled ? 'animated-collapse-transition' : '']"
    :style="wrapperStyle"
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
  overflow: hidden;
}

.animated-collapse-transition {
  transition: height 180ms cubic-bezier(0.22, 1, 0.36, 1);
  will-change: height;
}

.collapse-content {
  display: flow-root;
}

@media (prefers-reduced-motion: reduce) {
  .animated-collapse-transition {
    transition: none;
    will-change: auto;
  }
}
</style>
