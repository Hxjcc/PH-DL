<script setup>
import { ProgressRoot, ProgressIndicator } from 'reka-ui'
import { cn } from '../../lib/utils'

const props = defineProps({
  modelValue: { type: Number, default: 0 },
  indeterminate: { type: Boolean, default: false },
  class: { type: String, default: '' },
})
</script>

<template>
  <ProgressRoot
    :model-value="indeterminate ? 100 : Math.max(0, Math.min(100, modelValue || 0))"
    :class="cn('relative h-1.5 w-full overflow-hidden rounded-full bg-secondary', props.class)"
  >
    <ProgressIndicator
      v-if="!indeterminate"
      class="h-full w-full flex-1 bg-primary transition-transform duration-300 ease-out"
      :style="{ transform: `translateX(-${100 - Math.max(0, Math.min(100, modelValue || 0))}%)` }"
    />
    <ProgressIndicator
      v-else
      class="h-full w-full bg-primary/80 animate-pulse"
    />
  </ProgressRoot>
</template>
