<script setup lang="ts">
import { getErrorMessage } from '../utils/errors'
import { computed, nextTick, onActivated, onBeforeUnmount, onDeactivated, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ListChecks,
  RefreshCw,
  Loader2,
  Inbox,
  Trash2,
  Download,
  Database,
} from 'lucide-vue-next'
import { useAppStore } from '../stores/app'
import { api } from '../api/client'
import { resolveJobCategory } from '../utils/jobMeta'
import Card from '../components/ui/Card.vue'
import Button from '../components/ui/Button.vue'
import Badge from '../components/ui/Badge.vue'
import Checkbox from '../components/ui/Checkbox.vue'
import ConfirmPopover from '../components/ui/ConfirmPopover.vue'
import JobCard from '../components/JobCard.vue'
import Select from '../components/ui/Select.vue'
import { JOB_LIMITS } from '../utils/listPreferences'

const store = useAppStore()
const route = useRoute()
const router = useRouter()

const refreshing = ref(false)
const batchDeleting = ref(false)
const selectedJobIds = ref(new Set<number>())
const focusedJobId = ref<number | null>(null)
const changingLimit = ref(false)
const categoryFilter = ref('all')
const isRouteActive = ref(true)
const limitOptions = JOB_LIMITS.map((value) => ({ label: `${value} 条`, value }))
const allLoadedJobs = computed(() => store.jobs.slice(0, store.jobsLimit))

const categoryCounts = computed(() => {
  const counts = { all: allLoadedJobs.value.length, download: 0, update: 0, maintenance: 0 }
  for (const job of allLoadedJobs.value) {
    const group = resolveJobCategory(job).group
    if (counts[group] != null) counts[group] += 1
  }
  return counts
})

const categoryPills = computed(() => {
  const c = categoryCounts.value
  const pills: { value: string; label: string; count: number; icon: typeof Download | null }[] = [
    { value: 'all', label: '全部', count: c.all, icon: null },
  ]
  pills.push({ value: 'download', label: '下载任务', count: c.download, icon: Download })
  pills.push({ value: 'update', label: '自动追更', count: c.update, icon: RefreshCw })
  if (c.maintenance > 0) {
    pills.push({ value: 'maintenance', label: '基线与维护', count: c.maintenance, icon: Database })
  }
  return pills
})

const visibleJobs = computed(() => {
  if (categoryFilter.value === 'all') return allLoadedJobs.value
  return allLoadedJobs.value.filter((job) => resolveJobCategory(job).group === categoryFilter.value)
})

let suppressQueryScroll = false
let syncQueryTimer: ReturnType<typeof setTimeout> | null = null

function clearPendingQuerySync() {
  if (syncQueryTimer) {
    clearTimeout(syncQueryTimer)
    syncQueryTimer = null
  }
}

function readFocusedJob() {
  const raw = Number(route.query.job)
  focusedJobId.value = Number.isInteger(raw) && raw > 0 ? raw : null
}

function syncJobQuery(nextId: number | null, { immediate = false } = {}) {
  clearPendingQuerySync()
  const apply = () => {
    syncQueryTimer = null
    if (route.name !== 'jobs') return
    const currentRaw = route.query.job ? String(route.query.job) : ''
    const nextRaw = nextId ? String(nextId) : ''
    if (currentRaw === nextRaw) return
    suppressQueryScroll = true
    const nextQuery = { ...route.query }
    if (nextRaw) nextQuery.job = nextRaw
    else delete nextQuery.job
    router.replace({ query: nextQuery }).finally(() => {
      suppressQueryScroll = false
    })
  }
  if (immediate) {
    apply()
  } else {
    syncQueryTimer = setTimeout(apply, 360)
  }
}

function handleJobActivate(jobId: number, isExpanded: boolean) {
  if (isExpanded) {
    if (focusedJobId.value === jobId) return
    focusedJobId.value = jobId
    syncJobQuery(jobId)
  } else if (focusedJobId.value === jobId) {
    focusedJobId.value = null
    syncJobQuery(null)
  }
}

async function scrollToFocusedJob() {
  if (!focusedJobId.value) return
  const targetJob = allLoadedJobs.value.find((j) => j.id === focusedJobId.value)
  if (targetJob && categoryFilter.value !== 'all' && resolveJobCategory(targetJob).group !== categoryFilter.value) {
    categoryFilter.value = 'all'
  }
  await nextTick()
  document.getElementById(`job-${focusedJobId.value}`)?.scrollIntoView({
    behavior: 'smooth',
    block: 'center',
  })
}

readFocusedJob()
watch(
  () => route.query.job,
  () => {
    readFocusedJob()
    if (!suppressQueryScroll) {
      scrollToFocusedJob()
    }
  }
)

const ACTIVE_JOB_STATUSES = new Set(['queued', 'scanning', 'running'])

const runningJobId = computed(() => store.overview?.current_job?.id || null)
const activeJobCount = computed(
  () => Number(store.overview?.queued_count || 0) + (store.overview?.running ? 1 : 0)
)
const deletableJobs = computed(() =>
  visibleJobs.value.filter((job) => !ACTIVE_JOB_STATUSES.has(job.status))
)
const validSelectedIds = computed(() => {
  const allowed = new Set(deletableJobs.value.map((job) => job.id))
  return Array.from(selectedJobIds.value).filter((id) => allowed.has(id))
})
const allSelected = computed(
  () => deletableJobs.value.length > 0 && validSelectedIds.value.length === deletableJobs.value.length
)
const partiallySelected = computed(
  () => validSelectedIds.value.length > 0 && validSelectedIds.value.length < deletableJobs.value.length
)

function toggleSelectJob(jobId: number) {
  const next = new Set(selectedJobIds.value)
  if (next.has(jobId)) next.delete(jobId)
  else next.add(jobId)
  selectedJobIds.value = next
}

function toggleSelectAll() {
  selectedJobIds.value = allSelected.value
    ? new Set()
    : new Set(deletableJobs.value.map((job) => job.id))
}

function clearSelectedJobs() {
  selectedJobIds.value = new Set()
}

async function changeJobsLimit(value: string) {
  if (Number(value) === store.jobsLimit || !store.setJobsLimit(value)) return
  clearSelectedJobs()
  changingLimit.value = true
  try {
    await store.refreshJobs()
  } finally {
    changingLimit.value = false
  }
}

function handleJobDeleted(jobId: number) {
  if (focusedJobId.value === jobId) {
    focusedJobId.value = null
    syncJobQuery(null, { immediate: true })
  }
  if (!selectedJobIds.value.has(jobId)) return
  const next = new Set(selectedJobIds.value)
  next.delete(jobId)
  selectedJobIds.value = next
}

async function handleRefreshJobs() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    await Promise.all([
      store.refreshJobs(),
      store.refreshOverview(),
      store.refreshTargets(),
      new Promise((resolve) => setTimeout(resolve, 450)),
    ])
  } finally {
    refreshing.value = false
  }
}

async function handleBatchDeleteJobs() {
  const ids = validSelectedIds.value
  if (ids.length === 0 || batchDeleting.value) return
  batchDeleting.value = true
  try {
    const res = await api.batchDeleteJobs(ids)
    const deletedIds = res.deleted_ids || []
    if (deletedIds.length > 0) {
      store.removeJobs(deletedIds)
      if (focusedJobId.value && deletedIds.includes(focusedJobId.value)) {
        focusedJobId.value = null
        syncJobQuery(null, { immediate: true })
      }
    }
    selectedJobIds.value = new Set()
    const skipped = (res.skipped_active_ids || []).length
    if (skipped > 0) {
      store.notify(`已删除 ${deletedIds.length} 条任务，跳过 ${skipped} 条运行中任务`, 'info')
    } else {
      store.notify(`已批量删除 ${deletedIds.length} 条任务记录`, 'success')
    }
  } catch (err: unknown) {
    store.notify(getErrorMessage(err), 'error')
  } finally {
    batchDeleting.value = false
  }
}

let activatedOnce = false

onActivated(async () => {
  isRouteActive.value = true
  const tasks = []
  if (store.targetsCache === null) {
    tasks.push(store.ensureTargets())
  }
  if (!activatedOnce) {
    activatedOnce = true
    if (!store.hasRouteCache('jobs') || (!store.hydratedFromCache && Date.now() - store.jobsLoadedAt > 1500)) {
      tasks.push(store.refreshJobs())
    }
  } else {
    tasks.push(store.refreshJobs())
  }
  if (tasks.length > 0) await Promise.all(tasks)
  await scrollToFocusedJob()
})

onDeactivated(() => {
  isRouteActive.value = false
  clearPendingQuerySync()
})

onBeforeUnmount(() => {
  clearPendingQuerySync()
})
</script>

<template>
  <div class="space-y-4 sm:space-y-5">
    <div class="flex flex-wrap items-center justify-between gap-3 sm:gap-4">
      <div class="flex items-center gap-3 sm:gap-3.5 min-w-0">
        <div
          class="w-9 h-9 sm:w-10 sm:h-10 rounded-xl bg-brand/10 text-brand flex items-center justify-center shrink-0 ring-1 ring-brand/15"
        >
          <ListChecks class="w-4 h-4 sm:w-5 sm:h-5" />
        </div>
        <div class="min-w-0">
          <h1 class="text-lg sm:text-xl font-bold tracking-tight text-foreground">任务队列</h1>
          <p class="text-xs sm:text-sm text-muted-foreground mt-0.5">
            查看下载、追更与基线任务明细，实时追踪单视频进度与云端同步状态
          </p>
        </div>
      </div>
      <div class="flex items-center gap-2">
        <Badge v-if="activeJobCount > 0" variant="secondary" class="h-7 px-2.5 tabular-nums">
          运行/排队 {{ activeJobCount }}
        </Badge>
        <Button variant="outline" size="sm" :disabled="refreshing" @click="handleRefreshJobs">
          <RefreshCw :class="['w-3.5 h-3.5', refreshing ? 'animate-spin text-brand' : '']" />
          刷新
        </Button>
      </div>
    </div>

    <Card>
      <div class="px-3.5 sm:px-5 py-3 sm:py-3.5 border-b border-border bg-surface-subtle rounded-t-[calc(var(--radius-2xl)-1px)] flex flex-col sm:flex-row sm:flex-wrap sm:items-center sm:justify-between gap-2.5 sm:gap-3 min-h-[56px]">
        <div class="flex flex-wrap items-center gap-2 min-w-0">
          <!-- Category Filter Pills -->
          <div class="flex items-center gap-1.5 flex-nowrap overflow-x-auto no-scrollbar max-w-full sm:flex-wrap">
            <Button
              v-for="pill in categoryPills"
              :key="pill.value"
              variant="outline"
              size="xs"
              :aria-pressed="categoryFilter === pill.value"
              :class="[
                'h-7 px-2.5 rounded-full text-xs gap-1.5 shrink-0 shadow-none hover:shadow-none',
                categoryFilter === pill.value
                  ? 'border-brand/35 bg-brand/10 dark:bg-brand/15 text-brand hover:bg-brand/10 hover:border-brand/35 dark:hover:bg-brand/15'
                  : 'border-border bg-card text-muted-foreground hover:text-foreground',
              ]"
              @click="categoryFilter = pill.value"
            >
              <component :is="pill.icon" v-if="pill.icon" class="w-3 h-3" />
              <span>{{ pill.label }}</span>
              <span class="font-mono text-[10px] opacity-80 tabular-nums">{{ pill.count }}</span>
            </Button>
          </div>

          <template v-if="deletableJobs.length > 0">
            <span class="hidden sm:inline-block h-3.5 w-px bg-border mx-0.5" />
            <label class="hidden sm:inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground cursor-pointer select-none">
              <Checkbox
                :model-value="allSelected"
                :indeterminate="partiallySelected"
                @update:model-value="toggleSelectAll"
              />
              <span>{{ validSelectedIds.length > 0 ? `已选 ${validSelectedIds.length} 项` : '全选' }}</span>
            </label>
          </template>
        </div>

        <div class="flex flex-wrap items-center justify-between sm:justify-end gap-2 sm:gap-1.5">
          <label
            v-if="deletableJobs.length > 0"
            class="inline-flex sm:hidden items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground cursor-pointer select-none"
          >
            <Checkbox
              :model-value="allSelected"
              :indeterminate="partiallySelected"
              @update:model-value="toggleSelectAll"
            />
            <span>{{ validSelectedIds.length > 0 ? `已选 ${validSelectedIds.length} 项` : '全选' }}</span>
          </label>

          <div class="flex flex-wrap items-center gap-1.5 ml-auto sm:ml-0">
            <label for="jobs-display-limit" class="sm:mr-2 inline-flex items-center gap-1.5 sm:gap-2 text-xs text-muted-foreground">
              <span>显示<span class="hidden sm:inline">条数</span></span>
              <Select
                id="jobs-display-limit"
                :model-value="store.jobsLimit"
                :options="limitOptions"
                :disabled="changingLimit"
                class="h-8 w-24 sm:w-28 text-xs"
                @update:model-value="changeJobsLimit"
              />
            </label>
            <template v-if="validSelectedIds.length > 0">
              <Button variant="ghost" size="xs" class="text-muted-foreground" @click="clearSelectedJobs">
                取消
              </Button>
              <ConfirmPopover
                :title="`确定批量删除选中的 ${validSelectedIds.length} 条任务记录？`"
                description="仅清理队列记录，不影响已下载的视频和历史去重库。"
                confirm-text="删除所选"
                @confirm="handleBatchDeleteJobs"
              >
                <Button variant="destructive" size="xs" :disabled="batchDeleting">
                  <Loader2 v-if="batchDeleting" class="w-3.5 h-3.5 animate-spin" />
                  <Trash2 v-else class="w-3.5 h-3.5" />
                  删除所选 ({{ validSelectedIds.length }})
                </Button>
              </ConfirmPopover>
            </template>
          </div>
        </div>
      </div>

      <div v-if="!store.jobsLoaded" class="py-16 rounded-b-[calc(var(--radius-2xl)-1px)] flex items-center justify-center gap-2 text-xs text-muted-foreground leading-none">
        <Loader2 class="w-4 h-4 animate-spin shrink-0 text-brand" />
        <span>任务队列加载中...</span>
      </div>

      <div
        v-else-if="visibleJobs.length === 0"
        class="py-16 px-6 rounded-b-[calc(var(--radius-2xl)-1px)] flex flex-col items-center text-center bg-card"
      >
        <div class="w-11 h-11 rounded-2xl bg-brand/10 text-brand flex items-center justify-center mb-3">
          <Inbox class="w-5 h-5" />
        </div>
        <div class="text-sm font-medium text-foreground">
          {{ allLoadedJobs.length === 0 ? '暂无运行或历史任务' : '该分类下暂无任务记录' }}
        </div>
        <p class="text-xs text-muted-foreground mt-1">
          {{ allLoadedJobs.length === 0 ? '去“新建下载”提交任务，或从“自动追更”触发一次检查' : '点击上方“全部”可查看其他类型的任务' }}
        </p>
      </div>

      <div v-else class="jobs-list p-2.5 sm:p-4 space-y-2.5 sm:space-y-3 bg-background rounded-b-[calc(var(--radius-2xl)-1px)]">
        <JobCard
          v-for="job in visibleJobs"
          :id="`job-${job.id}`"
          :key="job.id"
          v-memo="[job, job.id === runningJobId, job.id === focusedJobId, selectedJobIds.has(job.id), isRouteActive]"
          :job="job"
          :default-expanded="job.id === runningJobId || job.id === focusedJobId"
          :focused="job.id === focusedJobId"
          :selectable="true"
          :selected="selectedJobIds.has(job.id)"
          :route-active="isRouteActive"
          @toggle-select="toggleSelectJob"
          @activate="handleJobActivate"
          @deleted="handleJobDeleted"
        />
      </div>
    </Card>
  </div>
</template>

<style scoped>
.jobs-list {
  contain: layout;
}
</style>
