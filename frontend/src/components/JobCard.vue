<script setup>
import { computed, ref, watch } from 'vue'
import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Square,
  RotateCcw,
  Download as DownloadIcon,
  Loader2,
  Trash2,
  RefreshCw,
  Zap,
  Search,
  Database,
  Sparkles,
  Link2,
  FileText,
  Heart,
  Bookmark,
  Clock,
  CloudUpload,
} from 'lucide-vue-next'
import { useAppStore } from '../stores/app'
import { api } from '../api/client'
import {
  avatarGradient,
  formatBytes,
  formatDateTime,
  formatSpeed,
  itemStatusMeta,
  jobStatusMeta,
} from '../utils/format'
import {
  buildItemStatusPills,
  computeJobLiveMetrics,
  filterJobItemsByStatus,
  resolveJobVisual,
} from '../utils/jobMeta'
import { buildExportFilename } from '../utils/exportFilename'
import Card from './ui/Card.vue'
import Button from './ui/Button.vue'
import Checkbox from './ui/Checkbox.vue'
import ConfirmPopover from './ui/ConfirmPopover.vue'
import AnimatedCollapse from './ui/AnimatedCollapse.vue'
import SiteBadge from './SiteBadge.vue'

const CATEGORY_ICONS = {
  download: DownloadIcon,
  link: Link2,
  file: FileText,
  refresh: RefreshCw,
  zap: Zap,
  search: Search,
  database: Database,
  rotate: RotateCcw,
  sparkles: Sparkles,
}

const props = defineProps({
  job: { type: Object, required: true },
  defaultExpanded: { type: Boolean, default: false },
  selectable: { type: Boolean, default: false },
  selected: { type: Boolean, default: false },
  focused: { type: Boolean, default: false },
  routeActive: { type: Boolean, default: true },
})

const emit = defineEmits(['toggle-select', 'deleted', 'activate'])

const store = useAppStore()
const expanded = ref(props.defaultExpanded)
const loadingItems = ref(false)
const busyAction = ref(false)
const detailsMounted = ref(props.defaultExpanded)
const itemsLoaded = ref(false)
const itemsLoadError = ref('')
const deletePopoverMounted = ref(false)
const deletePopoverDefaultOpen = ref(false)
const itemStatusFilter = ref('all')
const ITEMS_PER_PAGE = 50
const itemPage = ref(1)
const itemListScrollRef = ref(null)
const brokenAvatars = ref(new Set())

const isActive = computed(() => ['queued', 'scanning', 'running'].includes(props.job.status))
const tracksLiveProgress = computed(() => props.routeActive && ['scanning', 'running'].includes(props.job.status))
const items = computed(() => store.jobItems[props.job.id] || [])
const statusMeta = computed(() => jobStatusMeta(props.job.status))

const visual = computed(() => resolveJobVisual(props.job, store.targetsCache || []))
const categoryIcon = computed(() => CATEGORY_ICONS[visual.value.category.icon] || DownloadIcon)
const liveMetrics = computed(() =>
  computeJobLiveMetrics(
    props.job,
    tracksLiveProgress.value ? store.liveProgress : null,
    isActive.value ? store.jobItems[props.job.id] : null,
    tracksLiveProgress.value ? store.scanStatus : null
  )
)

const showItemUploader = computed(() => {
  if (visual.value.avatars.length !== 1) return true
  const singleUser = (visual.value.avatars[0]?.username || '').toLowerCase()
  if (!singleUser) return true
  return items.value.some((it) => it.uploader && it.uploader.toLowerCase() !== singleUser)
})

function isAvatarValid(entry) {
  return Boolean(entry?.avatarUrl) && !brokenAvatars.value.has(entry.avatarUrl)
}

function markAvatarBroken(url) {
  if (!url || brokenAvatars.value.has(url)) return
  const next = new Set(brokenAvatars.value)
  next.add(url)
  brokenAvatars.value = next
}

function capsuleClass(cap) {
  if (cap.tone === 'emerald') {
    return 'border-emerald-500/25 bg-emerald-500/8 text-emerald-700 dark:text-emerald-300'
  }
  if (cap.tone === 'amber') {
    return 'border-amber-500/25 bg-amber-500/8 text-amber-700 dark:text-amber-300'
  }
  if (cap.tone === 'brand') {
    return 'border-brand/25 bg-brand/8 text-foreground'
  }
  return 'border-border/80 bg-surface-subtle text-muted-foreground'
}

function qualityBadgeClass(q) {
  const num = parseInt(String(q || ''), 10)
  if (num >= 2160) return 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300'
  if (num >= 1440) return 'border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-300'
  return 'border-border/80 bg-secondary text-secondary-foreground'
}

const canRetry = computed(
  () => !isActive.value && (
    props.job.failed_items > 0 || props.job.status === 'cancelled' ||
    (['failed', 'partial_failed'].includes(props.job.status) && Boolean(props.job.error_message))
  )
)
const isMetadataJob = computed(() => props.job.kind === 'enrich_metadata')
const isExportJob = computed(() => Boolean(props.job.params?.export_only || props.job.export_text))
const hasNoJobItems = computed(
  () =>
    isExportJob.value ||
    props.job.kind === 'check_updates' ||
    props.job.kind === 'baseline_sync'
)
const detailsOpen = computed(() => expanded.value)
const detailsContentKey = computed(() => {
  if (loadingItems.value && items.value.length === 0) return 'loading'
  if (itemsLoadError.value && items.value.length === 0) return 'error'
  if (items.value.length === 0) return 'empty'
  const heightBucket = visibleItems.value.length >= 9 ? '9+' : String(visibleItems.value.length)
  return `ready:${itemStatusFilter.value}:${heightBucket}:${totalItemPages.value > 1 ? 1 : 0}`
})

function statusPillClass(variant) {
  if (variant === 'destructive') return 'border-red-500/25 bg-red-500/10 text-red-600 dark:text-red-400'
  if (variant === 'secondary') return 'border-transparent bg-secondary text-secondary-foreground'
  return 'border-border bg-card/90 text-foreground'
}

function warmDeletePopover() {
  if (!deletePopoverMounted.value) {
    deletePopoverMounted.value = true
  }
}

function openDeletePopoverNow() {
  deletePopoverDefaultOpen.value = true
  deletePopoverMounted.value = true
}

const retryButtonText = computed(() => {
  if (isExportJob.value) return '重试导出'
  if (isMetadataJob.value) return '重试补全'
  if (props.job.status === 'cancelled' && Number(props.job.failed_items || 0) === 0) {
    return '继续未完成'
  }
  if (Number(props.job.failed_items || 0) === 0) return '重试任务'
  if (Number(props.job.failed_items || 0) > 0 && props.job.status !== 'cancelled') {
    return '重试失败项'
  }
  return '重试 / 续传'
})

const retryButtonTitle = computed(() => {
  if (isExportJob.value) return '按原筛选条件重新导出链接'
  if (isMetadataJob.value) return '重新补全仍缺少画质或文件大小的基线视频'
  if (props.job.status === 'cancelled' && Number(props.job.failed_items || 0) === 0) {
    return '继续未完成条目；如果之前还没有生成明细，将按原设置重试'
  }
  if (Number(props.job.failed_items || 0) === 0) return '按原设置重试该任务'
  if (Number(props.job.failed_items || 0) > 0 && props.job.status !== 'cancelled') {
    return '仅重新下载该任务中失败的条目（已下部分自动断点续传）'
  }
  return '继续下载该任务中全部失败与中断条目（已下部分自动断点续传，已完成项自动跳过）'
})

const itemFilterPills = computed(() =>
  buildItemStatusPills(items.value, {
    isMetadataJob: isMetadataJob.value,
    activeFilter: itemStatusFilter.value,
  })
)

const filteredItems = computed(() => filterJobItemsByStatus(items.value, itemStatusFilter.value))
const totalItemPages = computed(() => Math.max(1, Math.ceil(filteredItems.value.length / ITEMS_PER_PAGE)))
const safeItemPage = computed(() => Math.min(Math.max(1, itemPage.value), totalItemPages.value))
const itemPageStart = computed(() => (safeItemPage.value - 1) * ITEMS_PER_PAGE)
const visibleItems = computed(() =>
  filteredItems.value.slice(itemPageStart.value, itemPageStart.value + ITEMS_PER_PAGE)
)
const itemIndexWidthClass = computed(() => {
  const len = filteredItems.value.length
  if (len >= 1000) return 'w-9 sm:w-10'
  if (len >= 100) return 'w-7 sm:w-8'
  return 'w-5 sm:w-6'
})

watch(totalItemPages, (maxPage) => {
  if (itemPage.value > maxPage) {
    itemPage.value = maxPage
  }
})

const transferProgressMap = computed(() => {
  if (!detailsMounted.value || !tracksLiveProgress.value) return {}
  const map = Object.create(null)
  for (const item of visibleItems.value) {
    if (item.status !== 'downloading' && item.status !== 'uploading') continue
    const live = store.liveProgress[item.id]
    const isUploading = item.status === 'uploading'
    const phase = String(live?.phase || (isUploading ? 'uploading' : 'downloading')).toLowerCase()
    const done = live
      ? Number(live.bytes_downloaded || 0)
      : (isUploading ? 0 : Number(item.bytes_downloaded || 0))
    const total = live && Number(live.bytes_total || 0) > 0
      ? Number(live.bytes_total)
      : Number(item.bytes_total || 0)
    const speed = live ? Number(live.speed_bps || 0) : 0
    const pct = phase === 'verifying' || phase === 'remuxing'
      ? 100
      : total > 0
        ? Math.min(isUploading ? 100 : 99.9, Math.round((done / total) * 1000) / 10)
        : 0
    const hasProgress = phase === 'verifying' || phase === 'remuxing' || done > 0 || speed > 0
    map[item.id] = {
      phase,
      doneText: formatBytes(done),
      totalText: total > 0 ? formatBytes(total) : '',
      speedText: speed > 0 ? formatSpeed(speed) : '',
      pct,
      hasProgress,
    }
  }
  return map
})

const schedulerChip = computed(() => {
  if (!tracksLiveProgress.value) return null
  const st = store.schedulerStatus[props.job.id]
  if (!st || typeof st !== 'object') return null
  const level = String(st.level || 'green')
  const freeText = st.free_bytes > 0 ? formatBytes(st.free_bytes) : '0 B'
  const waiting = Number(st.waiting_for_space || 0)
  const dlInfo = `下载 ${st.active_downloads ?? 0}/${st.allowed_downloads ?? st.max_downloads ?? 0}`
  if (waiting > 0 || level === 'red') {
    return {
      text: `等待上传释放空间 · 余量 ${freeText}`,
      toneClass: 'border-red-500/30 bg-red-500/10 text-red-700 dark:text-red-300',
      dotClass: 'bg-red-500',
    }
  }
  if (level === 'orange') {
    return {
      text: `磁盘紧张 ${freeText} · ${dlInfo}`,
      toneClass: 'border-orange-500/30 bg-orange-500/10 text-orange-700 dark:text-orange-300',
      dotClass: 'bg-orange-500',
    }
  }
  if (level === 'yellow') {
    return {
      text: `磁盘收紧 ${freeText} · ${dlInfo}`,
      toneClass: 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300',
      dotClass: 'bg-amber-500',
    }
  }
  return {
    text: `余量 ${freeText} · ${dlInfo}`,
    toneClass: 'border-border/80 bg-card/80 text-muted-foreground',
    dotClass: 'bg-emerald-500',
  }
})

function selectItemFilter(val) {
  if (itemStatusFilter.value === val) return
  itemStatusFilter.value = val
  itemPage.value = 1
  if (itemListScrollRef.value) itemListScrollRef.value.scrollTop = 0
}

function goItemPage(nextPage) {
  const clamped = Math.min(Math.max(1, nextPage), totalItemPages.value)
  if (clamped === itemPage.value) return
  itemPage.value = clamped
  if (itemListScrollRef.value) itemListScrollRef.value.scrollTop = 0
}

const exportFilename = computed(() => buildExportFilename(props.job))

function itemStatusLabel(item) {
  if (isMetadataJob.value) {
    return {
      pending: '等待补全',
      done: '已补全',
      failed: '失败',
      cancelled: '已取消',
    }[item.status] || itemStatusMeta(item.status).label
  }
  return itemStatusMeta(item.status).label
}

function visibleItemError(item) {
  const msg = String(item?.error_message || '').trim()
  if (!msg) return ''
  if (item?.status === 'cancelled' && msg === '已取消') return ''
  return msg
}

async function ensureItemsLoaded(force = false, { silent = false } = {}) {
  if (loadingItems.value || hasNoJobItems.value) return
  const expectedTotal = Number(props.job.total_items || 0)
  const cached = store.jobItems[props.job.id]
  if (!force && cached && (expectedTotal <= 0 || cached.length >= expectedTotal || (!isActive.value && itemsLoaded.value))) {
    return
  }
  if (!silent) {
    loadingItems.value = true
    itemsLoadError.value = ''
  }
  try {
    const detail = await store.loadJobDetail(props.job.id)
    itemsLoaded.value = !['queued', 'scanning', 'running'].includes(detail?.job?.status || props.job.status)
  } catch (err) {
    if (!silent) {
      itemsLoadError.value = err.message || '读取视频明细失败，请重试。'
    }
  } finally {
    if (!silent) {
      loadingItems.value = false
    }
  }
}

function prefetchItemsOnHover() {
  if (expanded.value || hasNoJobItems.value) return
  ensureItemsLoaded(false, { silent: true })
}

async function toggleExpand() {
  expanded.value = !expanded.value
  emit('activate', props.job.id, expanded.value)
  if (expanded.value) {
    detailsMounted.value = true
    await ensureItemsLoaded()
  }
}

function handleDetailsAfterLeave() {
  if (!expanded.value) {
    detailsMounted.value = false
    itemPage.value = 1
  }
}

watch(
  () => props.defaultExpanded,
  async (val) => {
    if (!val) return
    const alreadyExpanded = expanded.value && detailsMounted.value
    if (!expanded.value) {
      expanded.value = true
    }
    detailsMounted.value = true
    if (!alreadyExpanded) {
      await ensureItemsLoaded()
    }
  },
  { immediate: true }
)

watch(
  [() => props.job.total_items, () => props.job.status],
  async ([total]) => {
    if (expanded.value && Number(total || 0) > items.value.length) {
      await ensureItemsLoaded(true)
    }
  }
)

async function handleCancel() {
  busyAction.value = true
  try {
    await api.cancelJob(props.job.id)
    store.notify(`已发送停止信号给任务 #${props.job.id}`, 'warning')
  } catch (err) {
    store.notify(err.message, 'error')
  } finally {
    busyAction.value = false
  }
}

async function handleRetry() {
  busyAction.value = true
  try {
    const res = await api.retryFailedJob(props.job.id)
    store.notify(`已创建重试任务 #${res.job.id}`, 'success')
    await store.refreshJobs()
  } catch (err) {
    store.notify(err.message, 'error')
  } finally {
    busyAction.value = false
  }
}

async function handleDelete() {
  busyAction.value = true
  try {
    await api.deleteJob(props.job.id)
    store.removeJobs([props.job.id])
    store.notify(`已删除任务 #${props.job.id}`, 'info')
    emit('deleted', props.job.id)
  } catch (err) {
    store.notify(err.message, 'error')
  } finally {
    busyAction.value = false
  }
}

function downloadExportTxt() {
  const text = props.job.export_text || ''
  if (!text) return
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = exportFilename.value
  a.click()
  URL.revokeObjectURL(url)
}
</script>

<template>
  <Card
    :class="[
      'job-card overflow-hidden transition-[border-color,background-color] duration-150',
      selected
        ? 'border-brand/45 bg-brand/[0.03] dark:bg-brand/[0.06]'
        : focused
          ? 'border-brand/50 ring-2 ring-brand/10'
          : isActive
            ? 'border-brand/35 shadow-xs'
            : 'hover:border-foreground/20',
    ]"
    @pointerenter="prefetchItemsOnHover"
  >
    <!-- Job Header -->
    <div class="px-3.5 sm:px-5 py-3.5 sm:py-4 cursor-pointer select-none" @click="toggleExpand">
      <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2.5 sm:gap-3.5">
        <!-- Left: Checkbox + Avatar/Icon + Title & Capsules -->
        <div class="min-w-0 w-full sm:w-auto sm:flex-1 flex items-start sm:items-center gap-2.5 sm:gap-3.5">
          <div v-if="selectable" class="shrink-0 pt-2 sm:pt-0" @click.stop>
            <Checkbox
              :model-value="selected"
              :disabled="isActive"
              :title="isActive ? '运行或排队中的任务请先停止后再删除' : '勾选此任务'"
              @update:model-value="emit('toggle-select', job.id)"
            />
          </div>

          <!-- Visual Anchor: Creator Avatar / Stack / Category Tile -->
          <div class="relative w-10 h-10 shrink-0">
            <!-- Single Creator / Liked -->
            <template v-if="visual.avatars.length === 1">
              <div
                v-if="visual.avatars[0].isLiked"
                class="w-10 h-10 rounded-full bg-gradient-to-br from-rose-500 to-pink-600 text-white flex items-center justify-center shadow-xs"
              >
                <Heart class="w-4 h-4 fill-white/95" />
              </div>
              <div
                v-else-if="visual.avatars[0].isFavorites"
                class="w-10 h-10 rounded-full bg-gradient-to-br from-red-500 to-rose-600 text-white flex items-center justify-center shadow-xs"
              >
                <Bookmark class="w-4 h-4 fill-white/95" />
              </div>
              <img
                v-else-if="isAvatarValid(visual.avatars[0])"
                :src="visual.avatars[0].avatarUrl"
                :alt="visual.avatars[0].name"
                class="w-10 h-10 rounded-full object-cover bg-secondary ring-1 ring-border/70 shadow-2xs"
                loading="lazy"
                referrerpolicy="no-referrer"
                @error="markAvatarBroken(visual.avatars[0].avatarUrl)"
              />
              <div
                v-else
                :class="[
                  'w-10 h-10 rounded-full bg-gradient-to-br text-white flex items-center justify-center text-xs font-semibold shadow-xs',
                  avatarGradient(visual.avatars[0].name),
                ]"
              >
                {{ visual.avatars[0].name.slice(0, 1).toUpperCase() }}
              </div>
              <span
                class="absolute -bottom-0.5 -right-0.5 w-[18px] h-[18px] rounded-full bg-card ring-1 ring-border flex items-center justify-center shadow-2xs"
                :title="visual.category.label"
              >
                <component :is="categoryIcon" class="w-2.5 h-2.5 text-foreground/80" />
              </span>
            </template>

            <!-- Multi-Creator Avatar Cluster -->
            <template v-else-if="visual.avatars.length > 1">
              <div class="w-10 h-10 relative">
                <template v-for="(av, idx) in visual.avatars.slice(0, 2)" :key="av.key">
                  <img
                    v-if="isAvatarValid(av)"
                    :src="av.avatarUrl"
                    :alt="av.name"
                    :class="[
                      'w-6 h-6 rounded-full object-cover bg-secondary ring-2 ring-card absolute',
                      idx === 0 ? 'top-0 left-0 z-10' : 'bottom-0 right-0',
                    ]"
                    loading="lazy"
                    referrerpolicy="no-referrer"
                    @error="markAvatarBroken(av.avatarUrl)"
                  />
                  <div
                    v-else
                    :class="[
                      'w-6 h-6 rounded-full bg-gradient-to-br text-white flex items-center justify-center text-[10px] font-semibold ring-2 ring-card absolute',
                      avatarGradient(av.name),
                      idx === 0 ? 'top-0 left-0 z-10' : 'bottom-0 right-0',
                    ]"
                  >
                    {{ av.name.slice(0, 1).toUpperCase() }}
                  </div>
                </template>
              </div>
            </template>

            <!-- Category Icon Tile (URL Batch / Metadata / System) -->
            <div
              v-else
              :class="['w-10 h-10 rounded-xl flex items-center justify-center', visual.category.iconClass]"
            >
              <component :is="categoryIcon" class="w-4 h-4" />
            </div>
          </div>

          <!-- Structured Title + Capsules + Metrics -->
          <div class="min-w-0 flex-1 space-y-1.5">
            <!-- Row 1: Category Pill + Title + Parameter Capsules -->
            <div class="flex flex-wrap items-center gap-1.5 min-w-0">
              <span
                :class="[
                  'inline-flex h-[22px] items-center gap-1 rounded-full border px-2 sm:px-2.5 text-[11px] font-medium leading-none shrink-0 whitespace-nowrap',
                  visual.category.pillClass,
                ]"
              >
                <component :is="categoryIcon" class="w-3 h-3 shrink-0" />
                <span>{{ visual.category.label }}</span>
              </span>
              <SiteBadge v-if="visual.site" :site="visual.site" size="xs" />

              <span
                class="text-sm sm:text-[15px] font-semibold tracking-tight text-foreground truncate max-w-full sm:max-w-[340px]"
                :title="job.label || visual.title"
              >
                {{ visual.title }}
              </span>

              <span
                v-for="(cap, cIdx) in visual.capsules"
                :key="cIdx"
                :class="[
                  'inline-flex h-[22px] items-center rounded-full border px-2 sm:px-2.5 text-[11px] leading-none shrink-0 whitespace-nowrap',
                  cap.handle ? 'font-normal tracking-normal' : cap.mono ? 'font-mono tabular-nums' : 'font-medium',
                  capsuleClass(cap),
                ]"
              >
                <template v-if="cap.handle && cap.text.startsWith('@')">
                  <span class="opacity-65 mr-[1px]">@</span>
                  <span>{{ cap.text.slice(1) }}</span>
                </template>
                <template v-else>{{ cap.text }}</template>
              </span>

              <span class="inline-flex h-[22px] items-center rounded-full bg-secondary/80 px-2 text-[11px] leading-none font-mono text-muted-foreground tabular-nums shrink-0 whitespace-nowrap">
                #{{ job.id }}
              </span>
            </div>

            <!-- Row 2: Compact Metric Chips + Timestamp -->
            <div class="flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground tabular-nums">
              <span class="inline-flex items-center gap-1 rounded-md border border-border/70 bg-surface-subtle px-2 py-0.5 whitespace-nowrap">
                <span>进度</span>
                <strong class="font-semibold text-foreground">{{ liveMetrics.done }}</strong>
                <span class="text-muted-foreground/60">/</span>
                <span>{{ liveMetrics.total }}</span>
              </span>

              <span
                v-if="liveMetrics.uploaded > 0"
                class="inline-flex items-center gap-1 rounded-md border border-emerald-500/20 bg-emerald-500/8 px-2 py-0.5 text-emerald-700 dark:text-emerald-300 font-medium whitespace-nowrap"
              >
                <CloudUpload class="w-3 h-3 shrink-0" />
                已上传 {{ liveMetrics.uploaded }}
              </span>

              <span
                v-if="liveMetrics.skipped > 0"
                class="inline-flex items-center rounded-md border border-border/70 bg-surface-subtle px-2 py-0.5 whitespace-nowrap"
              >
                跳过 {{ liveMetrics.skipped }}
              </span>

              <span
                v-if="liveMetrics.failed > 0"
                class="inline-flex items-center rounded-md border border-red-500/25 bg-red-500/8 px-2 py-0.5 text-red-600 dark:text-red-400 font-medium whitespace-nowrap"
              >
                失败 {{ liveMetrics.failed }}
              </span>

              <span class="inline-flex items-center gap-1 text-muted-foreground/85 pl-0.5 whitespace-nowrap">
                <Clock class="w-3 h-3 opacity-70 shrink-0" />
                <span>{{ formatDateTime(job.created_at) }}</span>
                <span v-if="liveMetrics.durationText" class="text-muted-foreground/70">
                  · {{ liveMetrics.durationText }}
                </span>
              </span>
            </div>
          </div>
        </div>

        <!-- Right: Status Pill + Action Buttons -->
        <div
          class="flex flex-wrap items-center justify-between sm:justify-end gap-1.5 shrink-0 w-full sm:w-auto sm:ml-auto pt-2 sm:pt-0 border-t sm:border-t-0 border-border/60"
          @click.stop
        >
          <span
            :class="[
              'inline-flex items-center gap-1.5 rounded-full border font-medium whitespace-nowrap h-6 px-2.5 text-[11px]',
              statusPillClass(statusMeta.variant),
            ]"
          >
            <span v-if="statusMeta.dot" :class="['w-1.5 h-1.5 rounded-full shrink-0', statusMeta.dot]" />
            {{ statusMeta.label }}
          </span>

          <div class="flex items-center gap-1.5">
            <Button
              v-if="job.export_text"
              variant="outline"
              size="xs"
              :title="`导出为 ${exportFilename}`"
              @click="downloadExportTxt"
            >
              <DownloadIcon class="w-3.5 h-3.5" />
              导出 TXT
            </Button>

            <Button
              v-if="isActive"
              variant="outline"
              size="xs"
              class="text-red-600 dark:text-red-400 hover:text-red-600 hover:bg-red-500/10 border-red-500/30"
              :disabled="busyAction"
              @click="handleCancel"
            >
              <Square class="w-3 h-3" />
              停止
            </Button>

            <Button
              v-if="canRetry"
              variant="outline"
              size="xs"
              :disabled="busyAction"
              :title="retryButtonTitle"
              @click="handleRetry"
            >
              <RotateCcw :class="['w-3.5 h-3.5', busyAction ? 'animate-spin' : '']" />
              {{ retryButtonText }}
            </Button>

            <template v-if="!isActive">
              <ConfirmPopover
                v-if="deletePopoverMounted"
                :title="`删除任务记录 #${job.id}？`"
                description="仅删除队列记录，不会删除已下载的视频与历史去重记录。"
                confirm-text="删除"
                :default-open="deletePopoverDefaultOpen"
                @confirm="handleDelete"
                @open-change="(v) => { if (!v) deletePopoverDefaultOpen = false }"
              >
                <button
                  type="button"
                  class="inline-flex items-center justify-center h-7 w-7 rounded-lg text-muted-foreground hover:bg-accent hover:text-red-600 transition-colors cursor-pointer disabled:pointer-events-none disabled:opacity-50"
                  title="删除任务记录"
                  :disabled="busyAction"
                >
                  <Trash2 class="w-3.5 h-3.5" />
                </button>
              </ConfirmPopover>
              <button
                v-else
                type="button"
                class="inline-flex items-center justify-center h-7 w-7 rounded-lg text-muted-foreground hover:bg-accent hover:text-red-600 transition-colors cursor-pointer disabled:pointer-events-none disabled:opacity-50"
                title="删除任务记录"
                :disabled="busyAction"
                @pointerenter="warmDeletePopover"
                @focus="warmDeletePopover"
                @click="openDeletePopoverNow"
              >
                <Trash2 class="w-3.5 h-3.5" />
              </button>
            </template>

            <button
              type="button"
              class="inline-flex items-center justify-center h-7 w-7 rounded-lg text-muted-foreground hover:bg-accent hover:text-foreground transition-colors cursor-pointer"
              :aria-label="expanded ? '收起任务明细' : '展开任务明细'"
              :aria-expanded="expanded"
              :aria-controls="`job-details-${job.id}`"
              :aria-busy="loadingItems"
              @click="toggleExpand"
            >
              <ChevronDown :class="['w-4 h-4 transition-transform duration-[260ms] ease-[cubic-bezier(0.22,1,0.36,1)]', expanded ? 'rotate-180' : '']" />
            </button>
          </div>
        </div>
      </div>

      <!-- Live Telemetry & Progress Bar Strip -->
      <div
        v-if="isActive"
        class="mt-3 sm:mt-3.5 rounded-xl border border-border/75 bg-surface-subtle/80 dark:bg-surface-subtle/45 px-3 sm:px-3.5 py-2 sm:py-2.5 space-y-1.5"
      >
        <div class="flex flex-wrap items-center justify-between gap-2 text-[11px] tabular-nums">
          <span class="inline-flex items-center gap-1.5 text-muted-foreground">
            <Loader2 class="w-3 h-3 animate-spin text-brand shrink-0 will-change-transform" />
            <span>{{ liveMetrics.phaseText }}</span>
          </span>
          <div class="flex flex-wrap items-center gap-1.5 ml-auto">
            <span
              v-if="schedulerChip"
              :class="[
                'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 font-mono text-[10px] font-medium',
                schedulerChip.toneClass,
              ]"
            >
              <span :class="['w-1.5 h-1.5 rounded-full shrink-0', schedulerChip.dotClass]" />
              {{ schedulerChip.text }}
            </span>
            <span
              v-if="liveMetrics.downloadSpeedBps > 0"
              class="inline-flex items-center rounded-full border border-emerald-500/25 bg-emerald-500/10 px-2 py-0.5 font-mono text-[10px] font-semibold text-emerald-700 dark:text-emerald-300"
              title="实时总下载速率"
            >
              ↓ {{ formatSpeed(liveMetrics.downloadSpeedBps) }}
            </span>
            <span
              v-if="liveMetrics.uploadSpeedBps > 0"
              class="inline-flex items-center rounded-full border border-amber-500/25 bg-amber-500/10 px-2 py-0.5 font-mono text-[10px] font-semibold text-amber-700 dark:text-amber-300"
              title="实时总上传速率"
            >
              ↑ {{ formatSpeed(liveMetrics.uploadSpeedBps) }}
            </span>
            <span
              v-if="!liveMetrics.indeterminate && (liveMetrics.total > 0 || liveMetrics.hasScanProgress)"
              class="font-mono font-semibold text-foreground"
            >
              {{ liveMetrics.percent }}%
            </span>
          </div>
        </div>
        <div class="relative h-1.5 w-full overflow-hidden rounded-full bg-secondary">
          <div
            v-if="liveMetrics.indeterminate"
            class="h-full w-full bg-primary/80 animate-pulse"
          />
          <div
            v-else
            class="h-full w-full bg-primary transition-transform duration-200 ease-out"
            :style="{ transform: `translateX(-${100 - Math.max(0, Math.min(100, liveMetrics.percent || 0))}%)` }"
          />
        </div>
      </div>

      <div
        v-if="job.error_message"
        class="mt-2.5 rounded-lg border border-red-500/20 bg-red-500/8 px-3 py-1.5 text-xs text-red-600 dark:text-red-400"
      >
        {{ job.error_message }}
      </div>
    </div>

    <!-- Expanded Per-Video Rows -->
    <AnimatedCollapse
      :id="`job-details-${job.id}`"
      :open="detailsOpen"
      :content-key="detailsContentKey"
      @after-leave="handleDetailsAfterLeave"
    >
      <div
        v-if="detailsMounted"
        class="job-details border-t border-border bg-surface-subtle/50 dark:bg-surface-subtle/25 px-3 sm:px-5 py-3 sm:py-3.5"
        @click="!focused && emit('activate', job.id, true)"
      >
        <div v-if="loadingItems && items.length === 0" class="py-5 flex items-center justify-center gap-2 text-xs text-muted-foreground leading-none">
          <Loader2 class="w-3.5 h-3.5 animate-spin shrink-0 text-brand" />
          <span>加载视频明细...</span>
        </div>

        <div v-else-if="itemsLoadError && items.length === 0" class="py-4 flex flex-wrap items-center justify-center gap-2 text-xs text-muted-foreground">
          <span>{{ itemsLoadError }}</span>
          <Button variant="outline" size="xs" @click="ensureItemsLoaded(true)">重试加载</Button>
        </div>

        <div v-else-if="items.length === 0" class="py-4 text-center text-xs text-muted-foreground">
          <template v-if="isExportJob">
            {{ job.export_text ? '页面链接导出任务（点击右上角“导出 TXT”下载）' : isActive ? '正在筛选并整理页面链接...' : '没有可导出的符合条件的视频页面链接。' }}
          </template>
          <template v-else-if="job.kind === 'check_updates'">
            {{
              isActive
                ? '正在扫描目标列表检查更新（仅检查，不下载）...'
                : Number(job.done_items || job.total_items || 0) > 0
                  ? `仅检查更新模式：发现 ${job.done_items || job.total_items} 个新视频，不会自动下载或上传（如需下载请在「自动追更」点击「立即全量追更」，或在「实时日志」查看明细）`
                  : '仅检查更新模式：本次未发现新视频'
            }}
          </template>
          <template v-else-if="job.kind === 'baseline_sync'">
            {{
              isActive
                ? '正在扫描并登记基线视频（仅去重，不下载）...'
                : Number(job.done_items || job.total_items || 0) > 0
                  ? `基线初始化完成：已登记 ${job.done_items || job.total_items} 个已有视频用于去重（不下载文件，可在「历史记录」筛选查看）`
                  : '基线初始化完成：未发现可登记的视频'
            }}
          </template>
          <template v-else>
            暂无视频条目（可能仍在扫描列表或无新视频）
          </template>
        </div>

        <div v-else class="space-y-2.5">
          <div v-if="itemFilterPills.length > 1" class="flex flex-wrap items-center justify-between gap-2">
            <div class="flex items-center gap-1.5 flex-nowrap overflow-x-auto no-scrollbar max-w-full sm:flex-wrap">
              <button
                v-for="pill in itemFilterPills"
                :key="pill.value"
                type="button"
                :aria-pressed="itemStatusFilter === pill.value"
                :class="[
                  'inline-flex items-center gap-1.5 h-6 px-2.5 rounded-full border text-[11px] font-medium shrink-0 transition-colors cursor-pointer select-none tabular-nums',
                  itemStatusFilter === pill.value
                    ? 'border-brand/35 bg-brand/10 dark:bg-brand/15 text-brand'
                    : 'border-border bg-card text-muted-foreground hover:text-foreground hover:border-foreground/20',
                ]"
                @click="selectItemFilter(pill.value)"
              >
                <span v-if="pill.dot" :class="['w-1.5 h-1.5 rounded-full shrink-0', pill.dot]" />
                <span>{{ pill.label }}</span>
                <span class="font-mono text-[10px] opacity-80">{{ pill.count }}</span>
              </button>
            </div>
            <span v-if="filteredItems.length > ITEMS_PER_PAGE" class="text-[11px] text-muted-foreground tabular-nums">
              第 {{ itemPageStart + 1 }}–{{ itemPageStart + visibleItems.length }} / 共 {{ filteredItems.length }} 项
            </span>
          </div>

          <div v-if="visibleItems.length === 0" class="py-4 text-center text-xs text-muted-foreground">
            当前筛选下没有条目
          </div>

          <div v-else ref="itemListScrollRef" class="space-y-1.5 max-h-96 overflow-y-auto pr-0.5 sm:pr-1">
            <div
              v-for="(item, idx) in visibleItems"
              :key="item.id"
              class="job-item-row rounded-lg border border-border/80 bg-card px-3 sm:px-3.5 py-2 sm:py-2.5 space-y-1.5 sm:space-y-2 transition-colors hover:border-foreground/20"
            >
              <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-1.5 sm:gap-2.5">
                <!-- Left: Index + Status Badge + Title -->
                <div class="min-w-0 flex-1 flex items-center gap-1.5 sm:gap-2">
                  <span
                    :class="[
                      'shrink-0 text-right text-[11px] font-mono text-muted-foreground/65 tabular-nums',
                      itemIndexWidthClass,
                    ]"
                  >
                    {{ String(itemPageStart + idx + 1).padStart(2, '0') }}
                  </span>
                  <span class="inline-flex items-center justify-center gap-1.5 min-w-[68px] rounded-full border border-border bg-card/90 text-foreground font-medium whitespace-nowrap shrink-0 h-5 px-2 text-[10px]">
                    <span :class="['w-1.5 h-1.5 rounded-full shrink-0', itemStatusMeta(item.status).dot]" />
                    {{ itemStatusLabel(item) }}
                  </span>
                  <span class="text-xs sm:text-[13px] font-medium text-foreground truncate" :title="item.title || item.viewkey">
                    {{ item.title || item.viewkey }}
                  </span>
                </div>

                <!-- Right: Capsules (Uploader, Quality, Size / Live Speed, Source Link) -->
                <div class="flex flex-wrap items-center sm:justify-end gap-1.5 shrink-0 text-[11px] tabular-nums pl-5 sm:pl-0">
                  <span
                    v-if="showItemUploader && item.uploader"
                    class="hidden md:inline-flex h-[21px] items-center rounded-full border border-border/80 bg-surface-subtle px-2.5 text-[11px] leading-none text-muted-foreground max-w-32"
                    :title="item.uploader"
                  >
                    <span class="opacity-65 mr-[1px] shrink-0">@</span>
                    <span class="truncate">{{ item.uploader }}</span>
                  </span>

                  <span
                    v-if="item.quality"
                    :class="[
                      'inline-flex items-center rounded-full border px-2 py-0.5 font-mono text-[10px] font-medium',
                      qualityBadgeClass(item.quality),
                    ]"
                  >
                    {{ item.quality }}
                  </span>

                  <template v-if="item.status === 'downloading'">
                    <template v-if="transferProgressMap[item.id]?.phase === 'remuxing'">
                      <span
                        v-if="transferProgressMap[item.id]?.totalText || transferProgressMap[item.id]?.doneText"
                        class="text-muted-foreground font-mono text-[10px] sm:text-[11px]"
                      >
                        {{ transferProgressMap[item.id].totalText || transferProgressMap[item.id].doneText }}
                      </span>
                      <span class="rounded-full bg-violet-500/10 px-1.5 py-0.5 text-[10px] font-medium text-violet-600 dark:text-violet-400">
                        封装 MP4
                      </span>
                    </template>
                    <template v-else-if="!transferProgressMap[item.id]?.hasProgress && !transferProgressMap[item.id]?.totalText">
                      <span class="text-muted-foreground text-[11px]">等待响应...</span>
                    </template>
                    <template v-else>
                      <span class="text-muted-foreground font-mono text-[10px] sm:text-[11px]">
                        {{ transferProgressMap[item.id]?.doneText || '0 B' }}
                        <template v-if="transferProgressMap[item.id]?.totalText"> / {{ transferProgressMap[item.id].totalText }}</template>
                      </span>
                      <span
                        v-if="transferProgressMap[item.id]?.speedText"
                        class="rounded-full bg-emerald-500/10 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-emerald-600 dark:text-emerald-400"
                      >
                        ↓ {{ transferProgressMap[item.id].speedText }}
                      </span>
                      <span class="font-mono font-semibold text-foreground sm:w-11 text-right">
                        {{ transferProgressMap[item.id]?.pct || 0 }}%
                      </span>
                    </template>
                  </template>
                  <template v-else-if="item.status === 'uploading'">
                    <span
                      v-if="transferProgressMap[item.id]?.hasProgress && transferProgressMap[item.id]?.totalText"
                      class="text-muted-foreground font-mono text-[10px] sm:text-[11px]"
                    >
                      {{ transferProgressMap[item.id].doneText }} / {{ transferProgressMap[item.id].totalText }}
                    </span>
                    <span v-else-if="item.bytes_total > 0" class="text-muted-foreground font-mono text-[10px] sm:text-[11px]">
                      {{ formatBytes(item.bytes_total) }}
                    </span>
                    <span
                      v-if="transferProgressMap[item.id]?.phase === 'verifying'"
                      class="rounded-full bg-sky-500/10 px-1.5 py-0.5 text-[10px] font-medium text-sky-600 dark:text-sky-400"
                    >
                      校验中
                    </span>
                    <span
                      v-else-if="transferProgressMap[item.id]?.speedText"
                      class="rounded-full bg-amber-500/10 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-amber-700 dark:text-amber-300"
                    >
                      ↑ {{ transferProgressMap[item.id].speedText }}
                    </span>
                    <span
                      v-if="transferProgressMap[item.id]?.hasProgress"
                      class="font-mono font-semibold text-foreground sm:w-11 text-right"
                    >
                      {{ transferProgressMap[item.id].pct }}%
                    </span>
                  </template>
                  <span v-else-if="item.bytes_total > 0" class="text-muted-foreground font-mono text-[11px] sm:min-w-[54px] sm:text-right">
                    {{ formatBytes(item.bytes_total) }}
                  </span>
                  <span
                    v-else-if="item.bytes_downloaded > 0"
                    class="rounded-full border border-amber-500/25 bg-amber-500/8 px-2 py-0.5 text-[10px] font-mono text-amber-700 dark:text-amber-300"
                    title="磁盘已保留临时断点文件，下载时自动 Range 续传"
                  >
                    断点 {{ formatBytes(item.bytes_downloaded) }}
                  </span>

                  <a
                    v-if="item.page_url"
                    :href="item.page_url"
                    target="_blank"
                    rel="noopener noreferrer"
                    class="inline-flex h-6 w-6 ml-auto sm:ml-0 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground transition-colors"
                    :title="`打开原视频页 (${item.viewkey || 'PornHub'})`"
                    @click.stop
                  >
                    <svg
                      class="w-3.5 h-3.5"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      stroke-width="2"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                      aria-hidden="true"
                    >
                      <path d="M15 3h6v6" />
                      <path d="M10 14 21 3" />
                      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
                    </svg>
                  </a>
                </div>
              </div>

              <div
                v-if="item.status === 'downloading' || item.status === 'uploading'"
                class="relative h-1 w-full overflow-hidden rounded-full bg-secondary"
              >
                <div
                  v-if="item.status === 'downloading' && transferProgressMap[item.id]?.phase === 'remuxing'"
                  class="h-full w-full bg-violet-500/75 animate-pulse"
                />
                <div
                  v-else-if="item.status === 'uploading' && !transferProgressMap[item.id]?.hasProgress"
                  class="h-full w-full bg-primary/80 animate-pulse"
                />
                <div
                  v-else-if="item.status === 'downloading' && !transferProgressMap[item.id]?.hasProgress && !transferProgressMap[item.id]?.totalText"
                  class="h-full w-full bg-primary/40 animate-pulse"
                />
                <div
                  v-else
                  class="h-full w-full bg-primary transition-transform duration-200 ease-out"
                  :style="{ transform: `translateX(-${100 - Math.max(0, Math.min(100, transferProgressMap[item.id]?.pct || 0))}%)` }"
                />
              </div>

              <div v-if="visibleItemError(item)" class="text-[11px] text-red-600 dark:text-red-400 pl-7">
                {{ visibleItemError(item) }}
              </div>
            </div>
          </div>

          <div
            v-if="totalItemPages > 1"
            class="pt-1 flex flex-wrap items-center justify-between gap-2 text-[11px] text-muted-foreground tabular-nums"
          >
            <span>
              每页 {{ ITEMS_PER_PAGE }} 项 · 第 {{ safeItemPage }} / {{ totalItemPages }} 页
            </span>
            <div class="flex items-center gap-1">
              <Button variant="outline" size="xs" :disabled="safeItemPage <= 1" @click="goItemPage(1)">
                首页
              </Button>
              <Button variant="outline" size="xs" :disabled="safeItemPage <= 1" @click="goItemPage(safeItemPage - 1)">
                <ChevronLeft class="w-3.5 h-3.5" />
                上一页
              </Button>
              <Button variant="outline" size="xs" :disabled="safeItemPage >= totalItemPages" @click="goItemPage(safeItemPage + 1)">
                下一页
                <ChevronRight class="w-3.5 h-3.5" />
              </Button>
              <Button variant="outline" size="xs" :disabled="safeItemPage >= totalItemPages" @click="goItemPage(totalItemPages)">
                末页
              </Button>
            </div>
          </div>
        </div>
      </div>
    </AnimatedCollapse>
  </Card>
</template>

<style scoped>
.job-card {
  overflow-anchor: none;
  contain: layout style;
}

.job-details {
  contain: layout paint;
}
</style>
