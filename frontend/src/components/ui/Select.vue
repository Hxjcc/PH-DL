<script setup lang="ts" generic="T extends string | number | null | undefined">
import { computed } from 'vue'
import {
  SelectRoot,
  SelectTrigger,
  SelectValue,
  SelectPortal,
  SelectContent,
  SelectViewport,
  SelectItem,
  SelectItemIndicator,
  SelectItemText,
  type AcceptableValue,
} from 'reka-ui'
import type { ClassValue } from 'clsx'
import { Check, ChevronsUpDown } from 'lucide-vue-next'
import { cn } from '../../lib/utils'
import { useRipple } from '../../composables/useRipple'

defineOptions({ inheritAttrs: false })

const EMPTY_SENTINEL = '__SHADCN_EMPTY__'

type EncodedValue = T extends string | number ? `${T}` : ''

const props = withDefaults(defineProps<{
  modelValue?: string | number | null
  options?: readonly { label: string; value: T }[]
  placeholder?: string
  disabled?: boolean
  class?: ClassValue
}>(), {
  modelValue: '',
  options: () => [],
  placeholder: '请选择',
  disabled: false,
  class: '',
})

const emit = defineEmits<{ 'update:modelValue': [value: EncodedValue]; change: [value: EncodedValue] }>()
const { ripples, handlePointerDown } = useRipple(() => props.disabled)

const normalizedOptions = computed(() =>
  props.options.map((option) => ({
    label: option.label,
    raw: option.value,
    encoded:
      option.value === '' || option.value === null || option.value === undefined
        ? EMPTY_SENTINEL
        : String(option.value),
  }))
)

const encodedValue = computed(() => {
  if (props.modelValue === '' || props.modelValue === null || props.modelValue === undefined) {
    return normalizedOptions.value.some((option) => option.encoded === EMPTY_SENTINEL)
      ? EMPTY_SENTINEL
      : undefined
  }
  return String(props.modelValue)
})

// Keep the label independent of popup items registering and unregistering.
const selectedLabel = computed(() =>
  normalizedOptions.value.find((option) => option.encoded === encodedValue.value)?.label ?? props.placeholder
)

const triggerClass = computed(() =>
  cn(
    'group relative flex h-9 w-full overflow-hidden select-none items-center justify-between gap-2 whitespace-nowrap rounded-lg border border-input bg-card px-3 py-1 text-sm text-foreground shadow-xs transition-[border-color,box-shadow,background-color] duration-150 hover:border-foreground/20 focus-visible:outline-none focus-visible:border-foreground/35 focus-visible:ring-[3px] focus-visible:ring-foreground/10 disabled:cursor-not-allowed disabled:opacity-50 cursor-pointer data-[state=open]:border-brand/35 data-[state=open]:ring-[3px] data-[state=open]:ring-brand/10',
    props.class
  )
)

const isCompact = computed(() =>
  /(?:^|\s)text-(?:xs|\[(?:10|11|12)px\])(?:\s|$)/.test(triggerClass.value)
)

function handleUpdate(value: AcceptableValue) {
  // Reka receives only the encoded values above, including for numeric options.
  const next = (value === EMPTY_SENTINEL ? '' : value) as EncodedValue
  emit('update:modelValue', next)
  emit('change', next)
}
</script>

<template>
  <SelectRoot
    :model-value="encodedValue"
    :disabled="disabled"
    @update:model-value="handleUpdate"
  >
    <SelectTrigger
      v-bind="$attrs"
      @pointerdown="handlePointerDown"
      :class="triggerClass"
    >
      <span
        v-for="r in ripples"
        :key="r.id"
        class="btn-ripple-circle"
        :style="{ left: `${r.x}px`, top: `${r.y}px`, width: `${r.size}px`, height: `${r.size}px`, backgroundColor: 'rgba(128, 128, 128, 0.14)' }"
      />
      <SelectValue :placeholder="placeholder" class="select-trigger-label relative truncate">{{ selectedLabel }}</SelectValue>
      <ChevronsUpDown
        class="h-3.5 w-3.5 shrink-0 opacity-50 transition-[transform,color] duration-200 ease-[cubic-bezier(0.22,1,0.36,1)] group-data-[state=open]:rotate-180 group-data-[state=open]:text-brand"
      />
    </SelectTrigger>

    <SelectPortal>
      <SelectContent
        position="popper"
        align="start"
        :body-lock="false"
        :side-offset="5"
        :collision-padding="8"
        class="select-content z-[60] max-h-72 min-w-[var(--reka-select-trigger-width)] max-w-[var(--reka-select-content-available-width)] overflow-hidden rounded-lg border border-border bg-popover p-1 text-popover-foreground shadow-lg outline-none data-[state=open]:animate-select-in data-[state=closed]:animate-select-out"
      >
        <SelectViewport>
          <SelectItem
            v-for="option in normalizedOptions"
            :key="option.encoded"
            :value="option.encoded"
            :class="
              cn(
                'relative flex w-full cursor-pointer select-none items-center rounded-sm outline-none transition-colors duration-100 data-[disabled]:pointer-events-none data-[disabled]:opacity-50 data-[highlighted]:bg-accent data-[highlighted]:text-accent-foreground data-[state=checked]:bg-brand/10 data-[state=checked]:font-medium data-[state=checked]:text-brand',
                isCompact ? 'py-1.5 pl-2 pr-6 text-xs' : 'py-1.5 pl-2.5 pr-7 text-sm'
              )
            "
          >
            <SelectItemText class="min-w-0 truncate">{{ option.label }}</SelectItemText>
            <span
              :class="
                cn(
                  'absolute flex h-3.5 w-3.5 items-center justify-center',
                  isCompact ? 'right-1.5' : 'right-2'
                )
              "
            >
              <SelectItemIndicator>
                <Check :class="cn('animate-check-in text-brand', isCompact ? 'h-3 w-3' : 'h-3.5 w-3.5')" />
              </SelectItemIndicator>
            </span>
          </SelectItem>
        </SelectViewport>
      </SelectContent>
    </SelectPortal>
  </SelectRoot>
</template>
