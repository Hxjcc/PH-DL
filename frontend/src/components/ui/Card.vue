<script setup lang="ts">
import type { ClassValue } from 'clsx'
import { cn } from '../../lib/utils'

const props = withDefaults(defineProps<{ class?: ClassValue }>(), { class: '' })
</script>

<template>
  <div :class="cn('rounded-2xl border border-border bg-card text-card-foreground shadow-[var(--card-shadow)]', props.class)">
    <slot />
  </div>
</template>
