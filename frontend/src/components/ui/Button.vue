<script setup lang="ts">
import { computed } from 'vue'
import { cva } from 'class-variance-authority'
import type { ClassValue } from 'clsx'
import { cn } from '../../lib/utils'
import { useRipple } from '../../composables/useRipple'

const buttonVariants = cva(
  'group relative overflow-hidden inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-lg text-sm font-medium transition-all duration-200 ease-[cubic-bezier(0.22,1,0.36,1)] active:scale-[0.96] motion-reduce:active:scale-100 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50 cursor-pointer select-none [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg]:transition-transform [&_svg]:duration-300 [&_svg]:ease-[cubic-bezier(0.22,1,0.36,1)]',
  {
    variants: {
      variant: {
        default: 'bg-primary text-primary-foreground shadow-xs hover:bg-primary/90',
        destructive: 'bg-destructive text-destructive-foreground shadow-xs hover:bg-destructive/90',
        outline:
          'border border-input bg-card text-foreground shadow-xs hover:bg-accent hover:text-accent-foreground hover:border-foreground/20 hover:shadow-sm',
        secondary:
          'bg-secondary text-secondary-foreground hover:bg-secondary/80',
        ghost: 'hover:bg-accent hover:text-accent-foreground active:scale-[0.94]',
        link: 'text-primary underline-offset-4 hover:underline active:scale-100',
      },
      size: {
        default: 'h-9 px-4 py-2 rounded-lg',
        sm: 'h-8 rounded-lg px-3 text-xs',
        xs: 'h-7 rounded-lg px-2.5 text-xs',
        lg: 'h-10 rounded-xl px-6',
        icon: 'h-8 w-8 rounded-lg p-0',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'default',
    },
  }
)

const props = withDefaults(defineProps<{
  variant?: 'default' | 'destructive' | 'outline' | 'secondary' | 'ghost' | 'link'
  size?: 'default' | 'sm' | 'xs' | 'lg' | 'icon'
  type?: 'button' | 'submit' | 'reset'
  as?: string
  class?: ClassValue
  disabled?: boolean
}>(), {
  variant: 'default',
  size: 'default',
  type: 'button',
  as: 'button',
  class: '',
  disabled: false,
})

const { ripples, handlePointerDown } = useRipple(() => props.disabled || props.variant === 'link')

const isDarkSurface = computed(() => props.variant === 'default' || props.variant === 'destructive')

const classes = computed(() =>
  cn(buttonVariants({ variant: props.variant, size: props.size }), props.class)
)

</script>

<template>
  <component
    :is="as"
    :type="as === 'button' ? type : undefined"
    :disabled="as === 'button' ? disabled : undefined"
    :class="classes"
    @pointerdown="handlePointerDown"
  >
    <span
      v-for="r in ripples"
      :key="r.id"
      class="btn-ripple-circle"
      :style="{
        left: `${r.x}px`,
        top: `${r.y}px`,
        width: `${r.size}px`,
        height: `${r.size}px`,
        backgroundColor: isDarkSurface ? 'rgba(128, 128, 128, 0.28)' : 'rgba(128, 128, 128, 0.14)',
      }"
    />
    <slot />
  </component>
</template>
