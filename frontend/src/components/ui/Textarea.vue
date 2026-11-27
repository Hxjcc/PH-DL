<script setup lang="ts">
import { cn } from '../../lib/utils'
import type { ClassValue } from 'clsx'

const props = withDefaults(defineProps<{
  modelValue?: string
  rows?: string | number
  placeholder?: string
  disabled?: boolean
  class?: ClassValue
}>(), {
  modelValue: '',
  rows: 4,
  placeholder: '',
  disabled: false,
  class: '',
})

const emit = defineEmits<{ 'update:modelValue': [value: string] }>()

function onInput(event: Event) {
  emit('update:modelValue', (event.target as HTMLTextAreaElement).value)
}
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
    @input="onInput"
  />
</template>
