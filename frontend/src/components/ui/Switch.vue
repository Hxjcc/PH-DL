<script setup lang="ts">
import { SwitchRoot, SwitchThumb } from 'reka-ui'
import type { ClassValue } from 'clsx'
import { cn } from '../../lib/utils'

const props = withDefaults(defineProps<{
  modelValue?: boolean
  disabled?: boolean
  class?: ClassValue
}>(), {
  modelValue: false,
  disabled: false,
  class: '',
})

const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()
</script>

<template>
  <SwitchRoot
    :model-value="modelValue"
    :disabled="disabled"
    :class="
      cn(
        'group peer inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full p-[2px] transition-all duration-200 ease-[cubic-bezier(0.22,1,0.36,1)] active:scale-95 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-foreground/15 disabled:cursor-not-allowed disabled:opacity-50 data-[state=checked]:bg-primary data-[state=checked]:ring-1 data-[state=checked]:ring-inset data-[state=checked]:ring-primary data-[state=unchecked]:bg-zinc-200 dark:data-[state=unchecked]:bg-zinc-800 data-[state=unchecked]:ring-1 data-[state=unchecked]:ring-inset data-[state=unchecked]:ring-border',
        props.class
      )
    "
    @update:model-value="emit('update:modelValue', $event)"
  >
    <SwitchThumb
      class="pointer-events-none block h-4 w-4 rounded-full shadow-xs transition-transform duration-200 ease-[cubic-bezier(0.22,1,0.36,1)] data-[state=checked]:translate-x-4 data-[state=unchecked]:translate-x-0 data-[state=checked]:bg-primary-foreground data-[state=unchecked]:bg-white dark:data-[state=unchecked]:bg-zinc-300 group-active:scale-90"
    />
  </SwitchRoot>
</template>
