<script setup>
import { cn } from '../../lib/utils'
import { ref } from 'vue'

const inputElement = ref(null)
defineExpose({ focus: (options) => inputElement.value?.focus(options) })

const props = defineProps({
  modelValue: { type: [String, Number], default: '' },
  type: { type: String, default: 'text' },
  placeholder: { type: String, default: '' },
  disabled: { type: Boolean, default: false },
  autofocus: { type: Boolean, default: false },
  min: { type: [String, Number], default: undefined },
  max: { type: [String, Number], default: undefined },
  step: { type: [String, Number], default: undefined },
  class: { type: String, default: '' },
})

const emit = defineEmits(['update:modelValue'])

function onInput(e) {
  if (props.type === 'number') {
    const val = e.target.value
    emit('update:modelValue', val === '' ? '' : Number(val))
  } else {
    emit('update:modelValue', e.target.value)
  }
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
