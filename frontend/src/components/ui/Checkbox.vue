<script setup lang="ts">
import { Check, Minus } from 'lucide-vue-next'
import type { ClassValue } from 'clsx'
import { cn } from '../../lib/utils'

const props = withDefaults(defineProps<{
  modelValue?: boolean
  indeterminate?: boolean
  disabled?: boolean
  title?: string
  class?: ClassValue
}>(), {
  modelValue: false,
  indeterminate: false,
  disabled: false,
  title: undefined,
  class: '',
})

const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

function toggle() {
  if (props.disabled) return
  emit('update:modelValue', !props.modelValue)
}
</script>

<template>
  <button
    type="button"
    role="checkbox"
    :aria-checked="indeterminate ? 'mixed' : modelValue"
    :disabled="disabled"
    :title="title"
    :class="
      cn(
        'inline-flex h-4 w-4 shrink-0 align-middle items-center justify-center rounded-[5px] border transition-all duration-150 cursor-pointer active:scale-90 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-foreground/15 disabled:cursor-not-allowed disabled:opacity-40',
        modelValue || indeterminate
          ? 'border-primary bg-primary text-primary-foreground shadow-xs'
          : 'border-input bg-card hover:border-foreground/40',
        props.class
      )
    "
    @click="toggle"
  >
    <Minus v-if="indeterminate && !modelValue" class="h-3 w-3 stroke-[2.5]" />
    <Check
      v-else
      :class="[
        'h-3 w-3 stroke-[2.5] transition-[opacity,transform] duration-150',
        modelValue ? 'opacity-100 scale-100' : 'opacity-0 scale-75',
      ]"
    />
  </button>
</template>
