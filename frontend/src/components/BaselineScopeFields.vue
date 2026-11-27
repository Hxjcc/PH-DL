<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Input from './ui/Input.vue'
import Select from './ui/Select.vue'

const props = withDefaults(defineProps<{ modelValue?: string; disabled?: boolean }>(), {
  modelValue: 'all',
  disabled: false,
})
const emit = defineEmits<{ 'update:modelValue': [pages: string]; 'validity-change': [valid: boolean] }>()

const mode = ref('all')
const startPage = ref<number | ''>(1)
const endPage = ref<number | ''>(3)
const options = [
  { label: '全部视频', value: 'all' },
  { label: '仅第 1 页', value: 'first' },
  { label: '指定页码范围', value: 'range' },
]

const pages = computed(() => {
  if (mode.value === 'all') return 'all'
  if (mode.value === 'first') return '1'
  return `${startPage.value}-${endPage.value}`
})
const rangeError = computed(() => {
  if (mode.value !== 'range') return ''
  const start = startPage.value
  const end = endPage.value
  if (start === '' || end === '' || !Number.isInteger(start) || !Number.isInteger(end) || start < 1 || end < 1) {
    return '起止页码必须为大于 0 的整数。'
  }
  return end < start ? '结束页不能小于起始页。' : ''
})

watch(() => props.modelValue, (value) => {
  if (value === pages.value) return
  if (!value || value === 'all') mode.value = 'all'
  else if (value === '1') mode.value = 'first'
  else {
    const match = value.match(/^(\d+)(?:-(\d+))?$/)
    mode.value = 'range'
    startPage.value = match ? Number(match[1]) : 1
    endPage.value = match ? Number(match[2] || match[1]) : 3
  }
}, { immediate: true })

watch(pages, (value) => emit('update:modelValue', value), { immediate: true })
watch(rangeError, (value) => emit('validity-change', !value), { immediate: true })
</script>

<template>
  <div class="baseline-scope space-y-2.5">
    <label class="block space-y-1.5">
      <span class="text-xs font-medium text-foreground">基线扫描范围</span>
      <Select v-model="mode" :options="options" :disabled="disabled" />
    </label>

    <div v-if="mode === 'range'" class="grid grid-cols-2 gap-3">
      <label class="block space-y-1.5">
        <span class="text-xs text-muted-foreground">起始页</span>
        <Input v-model="startPage" type="number" :min="1" :step="1" :disabled="disabled" required />
      </label>
      <label class="block space-y-1.5">
        <span class="text-xs text-muted-foreground">结束页</span>
        <Input v-model="endPage" type="number" :min="Math.max(1, Number(startPage) || 1)" :step="1" :disabled="disabled" required />
      </label>
    </div>
    <p v-if="rangeError" role="alert" class="text-[11px] text-red-500">{{ rangeError }}</p>
    <p class="text-[11px] leading-5 text-muted-foreground">
      <template v-if="mode === 'all'">从第 1 页扫描到最后一页，登记当前列表全部可访问的视频。</template>
      <template v-else-if="mode === 'range'">登记指定页码内的已有视频，包含起止页。</template>
      <template v-else>只登记第 1 页的已有视频。</template>
      仅记录、不下载；后续追更会跳过这些视频。
    </p>
  </div>
</template>
