<script setup lang="ts" generic="T extends string = 'text'">
import { cn } from '../../lib/utils'
import { ref } from 'vue'
import type { ClassValue } from 'clsx'

type InputValue = T extends 'number' ? number | '' : 'number' extends T ? string | number : string

const inputElement = ref<HTMLInputElement | null>(null)
defineExpose({ focus: (options?: FocusOptions) => inputElement.value?.focus(options) })

const props = withDefaults(defineProps<{
  modelValue?: string | number
  type?: T & string
  placeholder?: string
  disabled?: boolean
  autofocus?: boolean
  min?: string | number
  max?: string | number
  step?: string | number
  class?: ClassValue
}>(), {
  modelValue: '',
  type: () => 'text' as T,
  placeholder: '',
  disabled: false,
  autofocus: false,
  min: undefined,
  max: undefined,
  step: undefined,
  class: '',
})

const emit = defineEmits<{ 'update:modelValue': [value: InputValue] }>()

function onInput(e: Event) {
  const value = (e.target as HTMLInputElement).value
  const next = props.type === 'number' && value !== '' ? Number(value) : value
  emit('update:modelValue', next as InputValue)
}
</script>

<template>
  <input
    ref="inputElement"
    :type="type"
    :value="modelValue"
    :placeholder="placeholder"
    :disabled="disabled"
    :autofocus="autofocus"
    :min="min"
    :max="max"
    :step="step"
    :class="
      cn(
        'flex h-9 w-full rounded-lg border border-input bg-card px-3 py-1 text-sm text-foreground shadow-xs transition-all duration-150 placeholder:text-muted-foreground hover:border-foreground/20 focus-visible:outline-none focus-visible:border-foreground/35 focus-visible:ring-[3px] focus-visible:ring-foreground/10 disabled:cursor-not-allowed disabled:opacity-50',
        props.class
      )
    "
    @input="onInput"
  />
</template>
