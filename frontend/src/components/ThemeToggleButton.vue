<script setup>
import { Sun, Moon } from 'lucide-vue-next'
import { useTheme } from '../composables/useTheme'
import { cn } from '../lib/utils'
import Button from './ui/Button.vue'

const props = defineProps({
  sizeClass: { type: String, default: 'h-7 w-7' },
  iconClass: { type: String, default: 'w-3.5 h-3.5' },
  class: { type: String, default: '' },
})

const { isDark, toggleTheme } = useTheme()
</script>

<template>
  <Button
    variant="outline"
    size="icon"
    :class="
      cn(
        'relative overflow-hidden rounded-full border-border/80 text-muted-foreground hover:text-foreground',
        props.sizeClass,
        props.class
      )
    "
    :title="isDark ? '切换到浅色主题' : '切换到深色主题'"
    :aria-label="isDark ? '切换到浅色主题' : '切换到深色主题'"
    :aria-pressed="isDark"
    @click="toggleTheme($event)"
  >
    <Sun
      :class="[
        props.iconClass,
        'absolute text-amber-400 transition-[transform,opacity] duration-300 ease-[cubic-bezier(0.22,1,0.36,1)]',
        isDark ? 'rotate-0 scale-100 opacity-100' : '-rotate-90 scale-50 opacity-0',
      ]"
    />
    <Moon
      :class="[
        props.iconClass,
        'absolute transition-[transform,opacity] duration-300 ease-[cubic-bezier(0.22,1,0.36,1)]',
        isDark ? 'rotate-90 scale-50 opacity-0' : 'rotate-0 scale-100 opacity-100',
      ]"
    />
  </Button>
</template>
