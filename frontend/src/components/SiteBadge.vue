<script setup>
import { computed } from 'vue'
import { siteMeta } from '../utils/format'

const props = defineProps({
  site: { type: String, default: 'pornhub' },
  size: { type: String, default: 'xs' }, // 'xs' | 'sm'
  full: { type: Boolean, default: false },
})

const meta = computed(() => siteMeta(props.site))
const isXh = computed(() => meta.value.key === 'xhamster')
</script>

<template>
  <span
    v-if="full"
    :title="meta.label"
    :class="
      isXh
        ? 'inline-flex items-center rounded-md border border-red-500/30 bg-red-500/10 px-2 py-0.5 font-sans text-[11px] font-semibold leading-4 text-red-600 dark:text-red-400 select-none shrink-0'
        : 'inline-flex items-center gap-[2px] rounded-md border border-zinc-900 bg-[#141414] px-1.5 py-0.5 font-sans text-[11px] font-bold leading-none select-none shrink-0 dark:border-[#ff9000]/40'
    "
  >
    <template v-if="isXh">xHamster</template>
    <template v-else>
      <span class="text-white">Porn</span><span class="rounded-[3px] bg-[#ff9000] px-1 py-[1px] text-[#111111]">hub</span>
    </template>
  </span>

  <span
    v-else-if="isXh"
    :title="meta.label"
    :class="[
      'inline-flex items-center justify-center border border-red-500/30 bg-red-500/10 font-sans font-bold leading-none text-red-600 dark:text-red-400 select-none shrink-0',
      size === 'sm'
        ? 'h-[18px] min-w-[26px] rounded-[5px] px-1.5 text-[10px]'
        : 'h-[16px] min-w-[23px] rounded px-1 text-[9px]',
    ]"
  >
    XH
  </span>

  <span
    v-else
    :title="meta.label"
    :class="[
      'inline-flex items-center justify-center border border-zinc-900 bg-[#141414] font-sans font-bold leading-none select-none shrink-0 dark:border-[#ff9000]/40',
      size === 'sm'
        ? 'h-[18px] min-w-[26px] gap-[1.5px] rounded-[5px] px-[3px] text-[10px]'
        : 'h-[16px] min-w-[23px] gap-[1.5px] rounded px-[2.5px] text-[9px]',
    ]"
  >
    <span class="text-white">P</span><span
      :class="[
        'inline-flex items-center justify-center bg-[#ff9000] text-[#111111] leading-none',
        size === 'sm'
          ? 'h-[13px] rounded-[2.5px] px-[2.5px]'
          : 'h-[11px] rounded-[2px] px-[2px]',
      ]"
    >H</span>
  </span>
</template>
