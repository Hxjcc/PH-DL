<script setup>
import { cn } from '../../lib/utils'
import Button from './Button.vue'

const props = defineProps({
  modelValue: { type: [String, Number], required: true },
  options: { type: Array, default: () => [] }, // [{ value, label, dot?, icon?, count?, title? }]
  ariaLabel: { type: String, default: '' },
  size: { type: String, default: 'md' }, // 'sm' | 'md'
  class: { type: String, default: '' },
})

const emit = defineEmits(['update:modelValue', 'change'])

function select(value) {
  if (value === props.modelValue) return
  emit('update:modelValue', value)
  emit('change', value)
}
</script>

<template>
  <div
    :class="
      cn(
        'inline-flex items-center rounded-lg bg-muted p-0.5 gap-0.5 shrink-0',
        props.class
      )
    "
    role="group"
    :aria-label="ariaLabel || undefined"
  >
    <Button
      v-for="opt in options"
      :key="opt.value"
      variant="ghost"
      size="xs"
      :title="opt.title"
      :aria-pressed="modelValue === opt.value"
      :class="[
        'gap-1.5 rounded-md font-medium shadow-none transition-all duration-200 ease-[cubic-bezier(0.22,1,0.36,1)]',
        size === 'sm' ? 'h-7 px-2.5 text-xs' : 'h-7 px-3 text-xs',
        modelValue === opt.value
          ? 'bg-card text-foreground shadow-xs hover:bg-card hover:text-foreground'
          : 'text-muted-foreground hover:bg-foreground/[0.04] hover:text-foreground',
      ]"
      @click="select(opt.value)"
    >
      <span
        v-if="opt.dot"
        :class="[
          'w-1.5 h-1.5 rounded-full shrink-0 transition-opacity duration-150',
          opt.dot,
          modelValue === opt.value ? 'opacity-100' : 'opacity-40',
        ]"
        aria-hidden="true"
      />
      <component :is="opt.icon" v-if="opt.icon" class="w-3 h-3 shrink-0" />
      <span>{{ opt.label }}</span>
      <span
        v-if="opt.count !== undefined && opt.count !== null"
        :class="[
          'tabular-nums text-[10px] leading-none px-1.5 py-0.5 rounded-full transition-colors',
          modelValue === opt.value
            ? 'bg-secondary text-foreground/85'
            : 'bg-background/60 text-muted-foreground',
        ]"
      >
        {{ opt.count }}
      </span>
    </Button>
  </div>
</template>
