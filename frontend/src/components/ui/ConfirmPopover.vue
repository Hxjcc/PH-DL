<script setup>
import { ref, watch } from 'vue'
import { PopoverRoot, PopoverTrigger, PopoverPortal, PopoverContent } from 'reka-ui'
import Button from './Button.vue'

const props = defineProps({
  title: { type: String, required: true },
  description: { type: String, default: '' },
  confirmText: { type: String, default: '确认' },
  destructive: { type: Boolean, default: true },
  defaultOpen: { type: Boolean, default: false },
})

const emit = defineEmits(['confirm', 'open-change'])
const open = ref(props.defaultOpen)

watch(open, (val) => {
  emit('open-change', val)
})

function handleConfirm() {
  open.value = false
  emit('confirm')
}
</script>

<template>
  <PopoverRoot v-model:open="open">
    <PopoverTrigger as-child>
      <slot />
    </PopoverTrigger>
    <PopoverPortal>
      <PopoverContent
        side="top"
        align="end"
        :side-offset="6"
        :collision-padding="12"
        class="z-50 w-[min(18rem,calc(100vw-1.5rem))] rounded-xl border border-border bg-popover p-3.5 sm:p-4 text-popover-foreground shadow-md outline-none animate-popover-in"
      >
        <div class="text-xs font-medium text-foreground leading-relaxed">{{ title }}</div>
        <div v-if="description" class="text-[11px] text-muted-foreground mt-1 leading-relaxed">
          {{ description }}
        </div>
        <div v-if="$slots.extra" class="mt-2.5 pt-2.5 border-t border-border/80">
          <slot name="extra" />
        </div>
        <div class="mt-3 flex items-center justify-end gap-1.5">
          <Button variant="outline" size="xs" @click="open = false">取消</Button>
          <Button :variant="destructive ? 'destructive' : 'default'" size="xs" @click="handleConfirm">
            {{ confirmText }}
          </Button>
        </div>
      </PopoverContent>
    </PopoverPortal>
  </PopoverRoot>
</template>
