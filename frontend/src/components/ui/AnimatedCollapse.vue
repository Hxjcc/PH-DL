<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps({
  open: { type: Boolean, default: false },
})

const content = ref(null)
const contentHeight = ref(0)
let observer

function measure() {
  contentHeight.value = Math.ceil(content.value?.getBoundingClientRect().height || 0)
}

onMounted(() => {
  measure()
  observer = new ResizeObserver(measure)
  observer.observe(content.value)
})
onBeforeUnmount(() => observer?.disconnect())
watch(() => props.open, async () => {
  await nextTick()
  measure()
}, { flush: 'post' })
</script>

<template>
  <div
    class="animated-collapse"
    :style="{ height: open ? `${contentHeight}px` : '0px' }"
    :inert="!open"
    :aria-hidden="!open"
  >
    <div ref="content" class="collapse-content">
      <slot />
    </div>
  </div>
</template>

<style scoped>
.animated-collapse {
  overflow: hidden;
  transition: height 280ms cubic-bezier(0.22, 1, 0.36, 1);
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
