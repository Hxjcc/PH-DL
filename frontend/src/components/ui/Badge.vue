<script setup lang="ts">
import { computed } from 'vue'
import { cva } from 'class-variance-authority'
import type { ClassValue } from 'clsx'
import { cn } from '../../lib/utils'

const badgeVariants = cva(
  'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium transition-colors whitespace-nowrap',
  {
    variants: {
      variant: {
        default: 'border-transparent bg-primary text-primary-foreground',
        secondary: 'border-transparent bg-secondary text-secondary-foreground',
        outline: 'border-border bg-card/90 text-foreground',
        destructive: 'border-red-500/25 bg-red-500/10 text-red-600 dark:text-red-400',
      },
    },
    defaultVariants: {
      variant: 'outline',
    },
  }
)

const props = withDefaults(defineProps<{
  variant?: 'default' | 'secondary' | 'outline' | 'destructive'
  dot?: string
  class?: ClassValue
}>(), {
  variant: 'outline',
  dot: '',
  class: '',
})

const classes = computed(() => cn(badgeVariants({ variant: props.variant }), props.class))
</script>

<template>
  <span :class="classes">
    <span v-if="dot" :class="['w-1.5 h-1.5 rounded-full shrink-0', dot]" />
    <slot />
  </span>
</template>
