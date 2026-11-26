<script setup>
import { computed } from 'vue'
import { siteMeta } from '../utils/format'

const props = defineProps({
  site: { type: String, default: 'pornhub' },
  size: { type: String, default: 'xs' }, // 'xs' | 'sm'
  full: { type: Boolean, default: false },
})

const meta = computed(() => siteMeta(props.site))
</script>

<template>
  <span
    :title="meta.label"
    :class="[
      'inline-flex items-center justify-center border font-sans font-semibold select-none shrink-0',
      meta.pillClass,
      full
        ? 'rounded-md px-2 py-0.5 text-[11px] leading-4'
        : size === 'sm'
          ? 'h-[18px] min-w-[26px] rounded-[5px] px-1.5 text-[10px] leading-none'
          : 'h-[16px] min-w-[23px] rounded px-1 text-[9px] leading-none',
    ]"
  >
    {{ full ? meta.label : meta.code }}
  </span>
</template>
