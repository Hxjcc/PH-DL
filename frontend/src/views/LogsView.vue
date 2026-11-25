<script setup>
import { computed, nextTick, onActivated, ref, watch } from 'vue'
import { Trash2, Search, Copy, Terminal, Activity } from 'lucide-vue-next'
import { useAppStore } from '../stores/app'
import { formatLogTime, formatLogTimestamp } from '../utils/format'
import Card from '../components/ui/Card.vue'
import Button from '../components/ui/Button.vue'
import Badge from '../components/ui/Badge.vue'
import Input from '../components/ui/Input.vue'
import Switch from '../components/ui/Switch.vue'

const store = useAppStore()
const levelFilter = ref('ALL')
const search = ref('')
const autoScroll = ref(true)
const boxRef = ref(null)

const levelPills = [
  { label: '全部', value: 'ALL' },
  { label: 'INFO', value: 'INFO' },
  { label: 'WARNING', value: 'WARNING' },
  { label: 'ERROR', value: 'ERROR' },
]

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  return store.logs.filter((entry) => {
    if (levelFilter.value !== 'ALL' && entry.level !== levelFilter.value) return false
    if (q && !String(entry.message || '').toLowerCase().includes(q)) return false
    return true
  })
})

const loopHealth = computed(() => {
  const lp = store.overview?.event_loop
  if (!lp) return null
  const cur = Number(lp.current_ms || 0)
  const avg = Number(lp.avg_ms || 0)
  const max = Number(lp.max_ms || 0)
  const warns = Number(lp.warning_count || 0)
  const threshold = Number(lp.warning_threshold_ms || 250)
  const warn = cur >= threshold
  return {
    text: `事件循环 ${cur.toFixed(1)}ms`,
    title: `当前 ${cur.toFixed(1)}ms · 平均 ${avg.toFixed(1)}ms · 峰值 ${max.toFixed(1)}ms · 累计延迟告警 ${warns} 次`,
    dot: warn ? 'bg-amber-500 status-dot-glow-amber' : 'bg-emerald-500 status-dot-glow-emerald',
  }
})

async function scrollToBottom() {
  if (!autoScroll.value) return
  await nextTick()
  if (boxRef.value) {
    boxRef.value.scrollTop = boxRef.value.scrollHeight
  }
}

watch(() => filtered.value.length, scrollToBottom)
onActivated(scrollToBottom)

function levelStyle(level) {
  if (level === 'ERROR' || level === 'CRITICAL') return 'text-red-500 dark:text-red-400'
  if (level === 'WARNING') return 'text-amber-600 dark:text-amber-400'
  return 'text-emerald-600 dark:text-emerald-400'
}

async function handleCopy() {
  const text = filtered.value
    .map(
      (e) =>
        `${formatLogTimestamp(e.ts)} [${e.level}]${e.job_id ? ` #${e.job_id}` : ''} ${e.message || ''}`
    )
    .join('\n')
  try {
    await navigator.clipboard.writeText(text)
    store.notify(`已复制 ${filtered.value.length} 条日志`, 'success')
  } catch {
    store.notify('复制失败，请检查剪贴板权限', 'warning')
  }
}

async function handleClear() {
  try {
    await store.clearLogs()
    store.notify('日志缓冲区已清空', 'info')
  } catch (err) {
    store.notify(err.message, 'error')
  }
}
</script>

<template>
  <div class="space-y-5">
    <!-- Page Header -->
    <div class="flex flex-wrap items-center justify-between gap-4">
      <div class="flex items-center gap-3.5">
        <div
          class="w-10 h-10 rounded-xl bg-brand/10 text-brand flex items-center justify-center shrink-0 ring-1 ring-brand/15"
        >
          <Terminal class="w-5 h-5" />
        </div>
        <div>
          <h1 class="text-xl font-bold tracking-tight text-foreground">实时日志</h1>
          <p class="text-xs sm:text-sm text-muted-foreground mt-0.5">
            后端解析、下载与 rclone 上传的实时输出流
          </p>
        </div>
      </div>
      <div class="flex items-center gap-2">
        <Badge
          v-if="loopHealth"
          variant="outline"
          :dot="loopHealth.dot"
          :title="loopHealth.title"
          class="h-8 px-2.5 tabular-nums text-xs"
        >
          <Activity class="w-3 h-3 mr-1 text-muted-foreground" />
          {{ loopHealth.text }}
        </Badge>
        <Button variant="outline" size="sm" :disabled="filtered.length === 0" @click="handleCopy">
          <Copy class="w-3.5 h-3.5" />
          复制全部
        </Button>
        <Button variant="outline" size="sm" @click="handleClear">
          <Trash2 class="w-3.5 h-3.5" />
          清屏
        </Button>
      </div>
    </div>

    <!-- Filter Strip -->
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div class="flex flex-wrap items-center gap-2 flex-1">
        <div class="flex items-center gap-1.5" role="group" aria-label="按日志级别筛选">
          <Button
            v-for="pill in levelPills"
            :key="pill.value"
            variant="outline"
            size="sm"
            :aria-pressed="levelFilter === pill.value"
            :class="[
              'rounded-full px-3 shadow-none hover:shadow-none',
              levelFilter === pill.value
                ? 'bg-brand/10 dark:bg-brand/15 text-brand border-brand/35 hover:bg-brand/10 hover:border-brand/35 dark:hover:bg-brand/15'
                : 'text-muted-foreground border-border hover:text-foreground',
            ]"
            @click="levelFilter = pill.value"
          >
            {{ pill.label }}
          </Button>
        </div>

        <div class="relative flex-1 min-w-[200px] max-w-xs">
          <Search class="w-3.5 h-3.5 text-muted-foreground absolute left-3 top-1/2 -translate-y-1/2" />
          <Input v-model="search" placeholder="过滤日志关键词..." class="pl-8 rounded-full" />
        </div>

        <Badge variant="secondary" class="tabular-nums h-8 px-3">{{ filtered.length }} 条</Badge>
      </div>

      <label class="inline-flex items-center gap-2 text-xs text-muted-foreground cursor-pointer">
        <Switch v-model="autoScroll" />
        <span>自动滚动到底部</span>
      </label>
    </div>

    <!-- Adaptive Light/Dark Log Console -->
    <Card class="overflow-hidden">
      <div
        ref="boxRef"
        class="p-4 font-mono text-xs leading-relaxed h-[calc(100vh-17.5rem)] min-h-[380px] overflow-y-auto space-y-1 bg-surface-subtle/70 dark:bg-[#0c0c0f]"
      >
        <div
          v-if="filtered.length === 0"
          class="h-full flex items-center justify-center text-muted-foreground font-sans text-xs"
        >
          暂无日志，等待实时推送...
        </div>

        <div
          v-for="entry in filtered"
          :key="entry.boot_id ? `${entry.boot_id}:${entry.id}` : entry.id"
          class="flex items-start gap-2.5 px-2.5 py-1 rounded-lg hover:bg-card transition-colors break-all"
        >
          <span class="text-muted-foreground/75 shrink-0 tabular-nums">{{ formatLogTime(entry.ts) }}</span>
          <span :class="['shrink-0 w-16 font-semibold', levelStyle(entry.level)]">
            [{{ entry.level }}]
          </span>
          <span
            v-if="entry.job_id"
            class="shrink-0 rounded-md bg-brand/12 px-1.5 text-[11px] font-medium text-brand"
          >
            #{{ entry.job_id }}
          </span>
          <span class="text-foreground/90">{{ entry.message }}</span>
        </div>
      </div>
    </Card>
  </div>
</template>
