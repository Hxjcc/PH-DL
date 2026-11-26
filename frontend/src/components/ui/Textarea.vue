<script setup>
import { cn } from '../../lib/utils'

const props = defineProps({
  modelValue: { type: String, default: '' },
  rows: { type: [String, Number], default: 4 },
  placeholder: { type: String, default: '' },
  disabled: { type: Boolean, default: false },
  class: { type: String, default: '' },
})

const emit = defineEmits(['update:modelValue'])
</script>

<template>
  <textarea
    :value="modelValue"
    :rows="rows"
    :placeholder="placeholder"
    :disabled="disabled"
    :class="
      cn(
        'ph-textarea flex w-full rounded-lg border border-input bg-card px-3.5 py-2.5 text-sm text-foreground shadow-xs transition-all duration-150 placeholder:text-muted-foreground hover:border-foreground/20 focus-visible:outline-none focus-visible:border-foreground/35 focus-visible:ring-[3px] focus-visible:ring-foreground/10 disabled:cursor-not-allowed disabled:opacity-50 resize-y',
        props.class
      )
    "
    @input="emit('update:modelValue', $event.target.value)"
  />
</template>
