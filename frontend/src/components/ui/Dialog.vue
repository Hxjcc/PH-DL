<script setup>
import {
  DialogRoot,
  DialogPortal,
  DialogOverlay,
  DialogContent,
  DialogTitle,
  DialogDescription,
  DialogClose,
} from 'reka-ui'
import { X } from 'lucide-vue-next'
import { cn } from '../../lib/utils'

const props = defineProps({
  open: { type: Boolean, default: false },
  title: { type: String, default: '' },
  description: { type: String, default: '' },
  class: { type: String, default: '' },
})

const emit = defineEmits(['update:open', 'close', 'open-auto-focus'])

function onOpenChange(val) {
  emit('update:open', val)
  if (!val) emit('close')
}
</script>

<template>
  <DialogRoot :open="open" @update:open="onOpenChange">
    <DialogPortal>
      <DialogOverlay
        class="fixed inset-0 z-50 bg-black/45 dark:bg-black/65 backdrop-blur-[3px] animate-overlay-in"
      />
      <DialogContent
        @open-auto-focus="emit('open-auto-focus', $event)"
        :class="
          cn(
            'fixed inset-0 m-auto h-fit z-50 w-[calc(100vw-1.5rem)] max-w-lg max-h-[calc(100dvh-2rem)] overflow-y-auto rounded-2xl border border-border bg-card p-4 sm:p-6 text-card-foreground shadow-xl outline-none animate-dialog-in',
            props.class
          )
        "
      >
        <div v-if="title || description" class="space-y-1 pr-7 mb-3.5 sm:mb-4">
          <DialogTitle v-if="title" class="text-base font-semibold tracking-tight text-foreground leading-snug">
            {{ title }}
          </DialogTitle>
          <DialogDescription v-if="description" class="text-xs text-muted-foreground">
            {{ description }}
          </DialogDescription>
        </div>

        <slot />

        <div v-if="$slots.footer" class="mt-4 sm:mt-5 flex flex-wrap items-center justify-end gap-2">
          <slot name="footer" />
        </div>

        <DialogClose
          class="absolute right-3.5 top-3.5 sm:right-4 sm:top-4 rounded-sm opacity-60 transition-opacity hover:opacity-100 focus:outline-none focus:ring-1 focus:ring-ring cursor-pointer"
          aria-label="关闭"
        >
          <X class="h-4 w-4" />
        </DialogClose>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>
