<script setup>
import { computed, nextTick, onActivated, onDeactivated, onUnmounted, reactive, ref, shallowRef, watch } from 'vue'
import { useRouter } from 'vue-router'
import {
  Search,
  Trash2,
  ChevronLeft,
  ChevronRight,
  RefreshCw,
  Loader2,
  Database,
  CloudUpload,
  HardDriveDownload,
  Bookmark,
  Layers,
  Sparkles,
} from 'lucide-vue-next'
import { api } from '../api/client'
import { useAppStore } from '../stores/app'
import { formatBytes, formatDateTime, formatPublishedDate, formatVideoDuration, videoStatusMeta } from '../utils/format'
import Card from '../components/ui/Card.vue'
import Button from '../components/ui/Button.vue'
import Badge from '../components/ui/Badge.vue'
import Input from '../components/ui/Input.vue'
import Select from '../components/ui/Select.vue'
import Checkbox from '../components/ui/Checkbox.vue'
import ConfirmPopover from '../components/ui/ConfirmPopover.vue'
import VideoPlaybackDialog from '../components/VideoPlaybackDialog.vue'
import { HISTORY_PAGE_SIZES } from '../utils/listPreferences'

const store = useAppStore()
const router = useRouter()

const cached = store.hasRouteCache('history') ? store.historyCache : null
const loading = ref(false)
const fetching = ref(false)
const refreshing = ref(false)
const enriching = ref(false)
const batchDeleting = ref(false)
const playingVideo = shallowRef(null)
let playbackTrigger = null
const selectedViewkeys = ref(new Set())
const deleteLocalSingle = ref(false)
const deleteLocalBatch = ref(false)
const activeDeleteKey = ref('')
const deletePopoverDefaultOpen = ref(false)
const items = shallowRef(cached ? [...cached.items] : [])
const total = ref(cached ? cached.total : 0)
const facets = ref(
  cached
    ? {
        uploaders: [...(cached.facets?.uploaders || [])],
        uploader_labels: { ...(cached.facets?.uploader_labels || {}) },
        qualities: [...(cached.facets?.qualities || [])],
      }
    : { uploaders: [], uploader_labels: {}, qualities: [] }
)
const stats = ref(
  cached
    ? { ...cached.stats }
    : store.overview?.videos
      ? { ...store.overview.videos }
      : { total: 0, uploaded: 0, downloaded: 0, baseline: 0 }
)

const filters = reactive({
  search: '',
  uploader: '',
  status: '',
  quality: '',
  sort_by: 'activity',
  page: 1,
  page_size: store.historyPageSize,
})

const sortOptions = [
  { label: '按记录时间', value: 'activity' },
  { label: '按发布日期', value: 'published' },
]

const pageSizeOptions = HISTORY_PAGE_SIZES.map((value) => ({ label: `${value} 条`, value }))

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / filters.page_size)))
const metadataCandidates = computed(
  () => stats.value.baseline_missing_metadata ?? stats.value.baseline ?? 0
)

const selectedCount = computed(() => selectedViewkeys.value.size)
const selectedHasLocalFiles = computed(() =>
  items.value.some(
    (v) => selectedViewkeys.value.has(v.viewkey) && (v.status === 'downloaded' || Boolean(v.local_path))
  )
)
const allSelected = computed(
  () => items.value.length > 0 && selectedViewkeys.value.size === items.value.length
)
const partiallySelected = computed(
  () => selectedViewkeys.value.size > 0 && selectedViewkeys.value.size < items.value.length
)

function toggleSelectVideo(viewkey) {
  const next = new Set(selectedViewkeys.value)
  if (next.has(viewkey)) next.delete(viewkey)
  else next.add(viewkey)
  selectedViewkeys.value = next
}

function toggleSelectAll() {
  selectedViewkeys.value = allSelected.value
    ? new Set()
    : new Set(items.value.map((video) => video.viewkey))
}

function clearSelection() {
  selectedViewkeys.value = new Set()
}

const statCards = computed(() => [
  {
    key: '',
    label: '总记录数',
    value: stats.value.total,
    icon: Layers,
    iconBox: 'bg-brand/10 text-brand',
  },
  {
    key: 'uploaded',
    label: '已上传云端',
    value: stats.value.uploaded,
    icon: CloudUpload,
    iconBox: 'bg-emerald-500/12 text-emerald-600 dark:text-emerald-400',
  },
  {
    key: 'downloaded',
    label: '本地已下载',
    value: stats.value.downloaded,
    icon: HardDriveDownload,
    iconBox: 'bg-secondary text-foreground',
  },
  {
    key: 'baseline',
    label: '仅基线去重',
    value: stats.value.baseline,
    icon: Bookmark,
    iconBox: 'bg-zinc-500/12 text-zinc-600 dark:text-zinc-400',
  },
])

const uploaderOptions = computed(() => [
  { label: '全部上传者', value: '' },
  ...(facets.value.uploaders || []).map((u) => ({
    label: facets.value.uploader_labels?.[u] || u,
    value: u,
  })),
])

const statusOptions = [
  { label: '全部状态', value: '' },
  { label: '已上传云端', value: 'uploaded' },
  { label: '本地已下载', value: 'downloaded' },
  { label: '仅基线去重', value: 'baseline' },
]

const qualityOptions = computed(() => [
  { label: '全部画质', value: '' },
  ...(facets.value.qualities || []).map((q) => ({ label: q, value: q })),
])

let debounceTimer = null
let loadingTimer = null
let fetchingTimer = null
let prefetchTimer = null
let reqSeq = 0
let activatedOnce = false
let activeAbortController = null

const QUERY_CACHE_MAX = 48
const QUERY_CACHE_TTL_MS = 45_000
const queryCache = new Map()
const inflightPrefetches = new Map()

function buildQueryCacheKey(page = filters.page) {
  return [
    filters.search.trim(),
    filters.uploader,
    filters.status,
    filters.quality,
    filters.sort_by,
    page,
    filters.page_size,
  ].join('\x1f')
}

function readQueryCache(key) {
  const entry = queryCache.get(key)
  if (!entry) return null
  if (Date.now() - entry.ts > QUERY_CACHE_TTL_MS) {
    queryCache.delete(key)
    return null
  }
  queryCache.delete(key)
  queryCache.set(key, entry)
  return entry
}

function writeQueryCache(key, itemsList, totalCount) {
  queryCache.delete(key)
  queryCache.set(key, { items: itemsList, total: totalCount, ts: Date.now() })
  if (queryCache.size > QUERY_CACHE_MAX) {
    const oldestKey = queryCache.keys().next().value
    if (oldestKey !== undefined) queryCache.delete(oldestKey)
  }
}

function cancelAllPrefetches() {
  clearTimeout(prefetchTimer)
  prefetchTimer = null
  for (const entry of inflightPrefetches.values()) {
    entry.controller.abort()
  }
  inflightPrefetches.clear()
}

function invalidateQueryCache() {
  queryCache.clear()
  cancelAllPrefetches()
}

function prefetchPage(targetPage) {
  if (targetPage < 1 || targetPage > totalPages.value || targetPage === filters.page) return
  const key = buildQueryCacheKey(targetPage)
  if (readQueryCache(key) || inflightPrefetches.has(key)) return

  const controller = new AbortController()
  const query = {
    search: filters.search,
    uploader: filters.uploader,
    status: filters.status,
    quality: filters.quality,
    sort_by: filters.sort_by,
    page: targetPage,
    page_size: filters.page_size,
    include_meta: false,
  }
  const promise = api
    .listVideos(query, { signal: controller.signal })
    .then((res) => {
      const nextItems = res.items || []
      const nextTotal = res.total || 0
      writeQueryCache(key, nextItems, nextTotal)
      return { items: nextItems, total: nextTotal }
    })
    .catch(() => null)
    .finally(() => {
      if (inflightPrefetches.get(key)?.controller === controller) {
        inflightPrefetches.delete(key)
      }
    })

  inflightPrefetches.set(key, { promise, controller })
}

function scheduleAdjacentPrefetch() {
  clearTimeout(prefetchTimer)
  prefetchTimer = setTimeout(() => {
    prefetchTimer = null
    if (filters.page < totalPages.value) {
      prefetchPage(filters.page + 1)
    }
    if (filters.page > 1) {
      prefetchPage(filters.page - 1)
    }
  }, 45)
}

function hasLoadedMeta() {
  return (facets.value.uploaders?.length || 0) > 0 || Number(stats.value.total || 0) > 0
}

function warmDeleteRowPopover(viewkey) {
  if (activeDeleteKey.value !== viewkey) {
    deletePopoverDefaultOpen.value = false
    activeDeleteKey.value = viewkey
  }
}

function openDeleteRowPopoverNow(viewkey) {
  deleteLocalSingle.value = false
  deletePopoverDefaultOpen.value = true
  activeDeleteKey.value = viewkey
}

function isDefaultFilters() {
  return (
    !filters.search &&
    !filters.uploader &&
    !filters.status &&
    !filters.quality &&
    filters.sort_by === 'activity' &&
    filters.page === 1 &&
    filters.page_size === store.historyPageSize
  )
}

function applyVideoList(nextItems, nextTotal) {
  items.value = nextItems
  total.value = nextTotal
  if (selectedViewkeys.value.size > 0) {
    const visibleKeys = new Set(nextItems.map((video) => video.viewkey))
    const kept = Array.from(selectedViewkeys.value).filter((key) => visibleKeys.has(key))
    if (kept.length !== selectedViewkeys.value.size) {
      selectedViewkeys.value = new Set(kept)
    }
  }
}

async function loadVideos({ silent = false, includeMeta = true, useCache = false } = {}) {
  const cacheKey = buildQueryCacheKey()
  if (useCache && !includeMeta) {
    const hit = readQueryCache(cacheKey)
    if (hit) {
      if (activeAbortController) {
        activeAbortController.abort()
        activeAbortController = null
      }
      ++reqSeq
      clearTimeout(loadingTimer)
      clearTimeout(fetchingTimer)
      applyVideoList(hit.items, hit.total)
      loading.value = false
      fetching.value = false
      scheduleAdjacentPrefetch()
      return
    }

    const pending = inflightPrefetches.get(cacheKey)
    if (pending) {
      if (activeAbortController) {
        activeAbortController.abort()
        activeAbortController = null
      }
      const seq = ++reqSeq
      clearTimeout(loadingTimer)
      clearTimeout(fetchingTimer)
      if (!silent) {
        fetchingTimer = setTimeout(() => {
          if (seq === reqSeq) fetching.value = true
        }, 120)
      }
      const prefetched = await pending.promise
      if (seq !== reqSeq) return
      clearTimeout(loadingTimer)
      clearTimeout(fetchingTimer)
      if (prefetched) {
        applyVideoList(prefetched.items, prefetched.total)
        loading.value = false
        fetching.value = false
        scheduleAdjacentPrefetch()
        return
      }
      fetching.value = false
    }
  }

  if (activeAbortController) {
    activeAbortController.abort()
  }
  const controller = new AbortController()
  activeAbortController = controller

  const seq = ++reqSeq
  clearTimeout(loadingTimer)
  clearTimeout(fetchingTimer)
  if (!silent && items.value.length === 0) {
    loadingTimer = setTimeout(() => {
      if (seq === reqSeq) loading.value = true
    }, 100)
  } else if (!silent) {
    fetchingTimer = setTimeout(() => {
      if (seq === reqSeq) fetching.value = true
    }, 100)
  }
  try {
    const effectiveIncludeMeta = includeMeta || !hasLoadedMeta()
    const query = effectiveIncludeMeta ? filters : { ...filters, include_meta: false }
    const res = await api.listVideos(query, { signal: controller.signal })
    if (seq !== reqSeq) return
    const nextItems = res.items || []
    const nextTotal = res.total || 0
    applyVideoList(nextItems, nextTotal)
    writeQueryCache(cacheKey, nextItems, nextTotal)
    if (res.facets) {
      facets.value = res.facets
    }
    if (res.stats) {
      stats.value = res.stats
    }
    if (isDefaultFilters() && res.facets && res.stats) {
      store.setHistoryCache(res)
    }
    scheduleAdjacentPrefetch()
  } catch (err) {
    if (err?.name === 'AbortError') return
    if (seq === reqSeq) {
      store.notify(err.message, 'error')
    }
  } finally {
    if (activeAbortController === controller) {
      activeAbortController = null
    }
    if (seq === reqSeq) {
      clearTimeout(loadingTimer)
      clearTimeout(fetchingTimer)
      loading.value = false
      fetching.value = false
    }
  }
}

function triggerSearch() {
  filters.page = 1
  cancelAllPrefetches()
  clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => loadVideos({ includeMeta: false, useCache: true }), 180)
}

function changeFilter() {
  filters.page = 1
  cancelAllPrefetches()
  clearTimeout(debounceTimer)
  loadVideos({ includeMeta: false, useCache: true })
}

function changePageSize(value) {
  if (Number(value) === filters.page_size || !store.setHistoryPageSize(value)) return
  filters.page_size = store.historyPageSize
  filters.page = 1
  clearSelection()
  cancelAllPrefetches()
  clearTimeout(debounceTimer)
  loadVideos({ includeMeta: false, useCache: true })
}

function selectStatusCard(statusKey) {
  if (statusKey === '') {
    if (filters.status === '') return
    filters.status = ''
  } else {
    filters.status = filters.status === statusKey ? '' : statusKey
  }
  changeFilter()
}

function goPage(p) {
  if (p < 1 || p > totalPages.value || p === filters.page) return
  activeDeleteKey.value = ''
  deletePopoverDefaultOpen.value = false
  filters.page = p
  loadVideos({ includeMeta: false, useCache: true })
}

function openPlayback(video, event) {
  playbackTrigger = event.currentTarget
  playingVideo.value = video
}

async function closePlayback() {
  const trigger = playbackTrigger
  playingVideo.value = null
  playbackTrigger = null
  await nextTick()
  if (trigger?.isConnected) trigger.focus({ preventScroll: true })
}

async function handleRedownload(v) {
  try {
    const res = await api.redownloadVideo(v.viewkey)
    store.notify(`已创建重下任务 #${res.job.id}`, 'success')
    await store.refreshJobs()
    router.push({ name: 'jobs' })
  } catch (err) {
    store.notify(err.message, 'error')
  }
}

async function handleDelete(v) {
  try {
    const res = await api.deleteVideo(v.viewkey, deleteLocalSingle.value)
    deleteLocalSingle.value = false
    store.notify(
      res?.deleted_local_file ? '已删除去重记录及本地视频文件' : '已从去重库删除记录',
      'info'
    )
    invalidateQueryCache()
    await loadVideos({ silent: true, includeMeta: true })
    if (items.value.length === 0 && filters.page > 1) {
      filters.page -= 1
      await loadVideos({ silent: true, includeMeta: false })
    }
  } catch (err) {
    store.notify(err.message, 'error')
  }
}

async function handleBatchDelete() {
  const viewkeys = Array.from(selectedViewkeys.value)
  if (viewkeys.length === 0 || batchDeleting.value) return
  batchDeleting.value = true
  try {
    const res = await api.batchDeleteVideos(viewkeys, deleteLocalBatch.value)
    deleteLocalBatch.value = false
    const fileNote = res.deleted_files_count > 0 ? `（含 ${res.deleted_files_count} 个本地视频文件）` : ''
    store.notify(`已从去重库删除 ${res.deleted_count} 条记录${fileNote}`, 'info')
    clearSelection()
    invalidateQueryCache()
    await loadVideos({ silent: true, includeMeta: true })
    if (items.value.length === 0 && filters.page > 1) {
      filters.page -= 1
      await loadVideos({ silent: true, includeMeta: false })
    }
  } catch (err) {
    store.notify(err.message, 'error')
  } finally {
    batchDeleting.value = false
  }
}

async function handleRefresh() {
  if (refreshing.value) return
  refreshing.value = true
  invalidateQueryCache()
  try {
    await Promise.all([loadVideos({ silent: true, includeMeta: true }), new Promise((r) => setTimeout(r, 400))])
  } finally {
    refreshing.value = false
  }
}

async function handleEnrichMetadata() {
  if (enriching.value || metadataCandidates.value <= 0) return
  enriching.value = true
  try {
    const res = await api.enrichBaselineMetadata()
    store.notify(`已创建元数据补全任务 #${res.job.id}，共 ${res.count} 条`, 'success')
    await store.refreshJobs()
    router.push({ name: 'jobs' })
  } catch (err) {
    store.notify(err.message, 'error')
  } finally {
    enriching.value = false
  }
}

watch(
  () => store.historyCache,
  (val) => {
    invalidateQueryCache()
    if (!val || !isDefaultFilters() || Number(val.page_size || 30) !== filters.page_size) return
    items.value = [...(val.items || [])]
    total.value = Number(val.total || 0)
    facets.value = {
      uploaders: [...(val.facets?.uploaders || [])],
      uploader_labels: { ...(val.facets?.uploader_labels || {}) },
      qualities: [...(val.facets?.qualities || [])],
    }
    stats.value = { ...(val.stats || { total: 0, uploaded: 0, downloaded: 0, baseline: 0 }) }
  }
)

onActivated(() => {
  if (!activatedOnce) {
    activatedOnce = true
    if (store.hydratedFromCache && store.hasRouteCache('history')) {
      scheduleAdjacentPrefetch()
      return
    }
    if (!store.hasRouteCache('history')) {
      loadVideos({ silent: false, includeMeta: true })
    } else if (Date.now() - store.historyLoadedAt > 1500) {
      loadVideos({ silent: true, includeMeta: true })
    } else {
      scheduleAdjacentPrefetch()
    }
    return
  }
  invalidateQueryCache()
  loadVideos({ silent: true, includeMeta: true })
})

onUnmounted(() => {
  clearTimeout(debounceTimer)
  clearTimeout(loadingTimer)
  clearTimeout(fetchingTimer)
  cancelAllPrefetches()
  activeAbortController?.abort()
})

onDeactivated(() => {
  cancelAllPrefetches()
  playingVideo.value = null
  playbackTrigger = null
})
</script>

<template>
  <div class="space-y-6">
    <!-- Page Header with tinted section icon -->
    <div class="flex flex-wrap items-center justify-between gap-4">
      <div class="flex items-center gap-3.5">
        <div
          class="w-10 h-10 rounded-xl bg-brand/10 text-brand flex items-center justify-center shrink-0 ring-1 ring-brand/15"
        >
          <Database class="w-5 h-5" />
        </div>
        <div>
          <h1 class="text-xl font-bold tracking-tight text-foreground">历史记录</h1>
          <p class="text-xs sm:text-sm text-muted-foreground mt-0.5">
            统一 SQLite 视频去重索引库，点击统计卡片可快速筛选对应状态
          </p>
        </div>
      </div>
      <ConfirmPopover
        :title="`补全 ${metadataCandidates} 条基线视频的画质和大小？`"
        description="会解析视频详情并用 Range 请求读取媒体大小，不会下载视频文件。记录较多时可能需要数分钟。"
        confirm-text="开始补全"
        @confirm="handleEnrichMetadata"
      >
        <Button
          variant="outline"
          size="sm"
          class="border-brand/25 bg-brand/5 text-brand hover:bg-brand/10"
          :disabled="enriching || metadataCandidates <= 0"
        >
          <Loader2 v-if="enriching" class="w-3.5 h-3.5 animate-spin" />
          <Sparkles v-else class="w-3.5 h-3.5" />
          {{ enriching ? '提交中...' : '补全基线元数据' }}
          <Badge v-if="metadataCandidates > 0" variant="secondary" class="px-1.5 py-0 text-[10px]">
            {{ metadataCandidates }}
          </Badge>
        </Button>
      </ConfirmPopover>
    </div>

    <!-- Interactive Semantic KPI Cards (inspired by rust-srec Pipeline Statistics) -->
    <div class="grid grid-cols-2 sm:grid-cols-4 gap-3.5">
      <button
        v-for="card in statCards"
        :key="card.key || 'all'"
        type="button"
        :class="[
          'text-left rounded-2xl border bg-card p-4 shadow-[var(--card-shadow)] transition-all duration-200 cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/30',
          filters.status === card.key
            ? 'border-brand/45 ring-2 ring-brand/12'
            : 'border-border hover:border-foreground/20',
        ]"
        @click="selectStatusCard(card.key)"
      >
        <div class="flex items-center justify-between gap-2">
          <span class="text-xs font-medium text-muted-foreground">{{ card.label }}</span>
          <span :class="['w-8 h-8 rounded-full flex items-center justify-center shrink-0', card.iconBox]">
            <component :is="card.icon" class="w-4 h-4" />
          </span>
        </div>
        <div class="mt-2.5 text-2xl font-bold tracking-tight text-foreground tabular-nums">
          {{ card.value }}
        </div>
      </button>
    </div>

    <!-- Filter Bar + Elevated DataTable -->
    <div class="space-y-3">
      <div class="flex flex-wrap items-center gap-2">
        <div class="relative flex-1 min-w-[200px]">
          <span class="pointer-events-none absolute inset-y-0 left-3 flex items-center">
            <Loader2 v-if="fetching" class="w-3.5 h-3.5 text-brand animate-spin" />
            <Search v-else class="w-3.5 h-3.5 text-muted-foreground" />
          </span>
          <Input
            v-model="filters.search"
            placeholder="搜索标题、Viewkey 或上传者..."
            class="pl-8"
            @update:model-value="triggerSearch"
          />
        </div>

        <Select
          v-model="filters.uploader"
          :options="uploaderOptions"
          class="w-40"
          @change="changeFilter"
        />
        <Select
          v-model="filters.status"
          :options="statusOptions"
          class="w-32"
          @change="changeFilter"
        />
        <Select
          v-model="filters.quality"
          :options="qualityOptions"
          class="w-28"
          @change="changeFilter"
        />
        <Select
          v-model="filters.sort_by"
          :options="sortOptions"
          class="w-32"
          @change="changeFilter"
        />
        <label for="history-page-size" class="inline-flex items-center gap-2 text-xs text-muted-foreground">
          每页
          <Select
            id="history-page-size"
            :model-value="filters.page_size"
            :options="pageSizeOptions"
            class="w-24"
            @update:model-value="changePageSize"
          />
        </label>
        <template v-if="selectedCount > 0">
          <Badge variant="secondary" class="h-9 px-2.5 tabular-nums">已选 {{ selectedCount }}</Badge>
          <ConfirmPopover
            :title="`确定从去重库删除选中的 ${selectedCount} 条记录？`"
            description="删除后自动追更会重新视这些视频为未下载。"
            confirm-text="删除所选"
            @open-change="(val) => { if (val) deleteLocalBatch = false }"
            @confirm="handleBatchDelete"
          >
            <template v-if="selectedHasLocalFiles" #extra>
              <label class="flex items-center gap-2 text-xs leading-4 text-foreground cursor-pointer select-none">
                <Checkbox v-model="deleteLocalBatch" />
                <span>同时删除本地已下载的视频文件</span>
              </label>
            </template>
            <Button variant="destructive" size="sm" :disabled="batchDeleting">
              <Loader2 v-if="batchDeleting" class="w-3.5 h-3.5 animate-spin" />
              <Trash2 v-else class="w-3.5 h-3.5" />
              删除所选
            </Button>
          </ConfirmPopover>
        </template>
        <Button variant="outline" size="icon" class="h-9 w-9" title="刷新" @click="handleRefresh">
          <RefreshCw :class="['w-3.5 h-3.5', refreshing ? 'animate-spin text-brand' : '']" />
        </Button>
      </div>

      <Card class="overflow-hidden">
        <div class="overflow-x-auto">
          <table class="w-full min-w-[1060px] text-left text-sm">
            <thead>
              <tr class="border-b border-border bg-surface-subtle text-xs font-medium text-muted-foreground whitespace-nowrap">
                <th class="h-11 pl-5 pr-2 w-12">
                  <Checkbox
                    :model-value="allSelected"
                    :indeterminate="partiallySelected"
                    title="全选本页"
                    @update:model-value="toggleSelectAll"
                  />
                </th>
                <th class="h-11 px-5">视频标题 / Viewkey</th>
                <th class="h-11 px-5 w-40">上传者</th>
                <th class="h-11 px-5 w-24">画质</th>
                <th class="h-11 px-5 w-28">大小</th>
                <th class="h-11 px-5 w-32">状态</th>
                <th class="h-11 px-5 w-44">记录时间</th>
                <th class="h-11 px-5 w-40">操作</th>
              </tr>
            </thead>
            <tbody :class="['divide-y divide-border transition-opacity duration-150', fetching ? 'opacity-65' : '']">
              <tr v-if="loading && items.length === 0">
                <td colspan="8" class="py-12 text-center text-xs text-muted-foreground">
                  <div class="inline-flex items-center justify-center gap-2 leading-none">
                    <Loader2 class="w-4 h-4 animate-spin shrink-0 text-brand" />
                    <span>加载中...</span>
                  </div>
                </td>
              </tr>
              <tr v-else-if="items.length === 0">
                <td colspan="8" class="py-14 text-center text-xs text-muted-foreground">
                  暂无匹配的历史记录
                </td>
              </tr>
              <tr v-for="v in items" :key="v.viewkey" class="hover:bg-accent/50 transition-colors">
                <td class="pl-5 pr-2 py-3.5 w-12">
                  <button
                    type="button"
                    role="checkbox"
                    :aria-checked="selectedViewkeys.has(v.viewkey)"
                    :title="`选择 ${v.viewkey}`"
                    :class="[
                      'inline-flex h-4 w-4 shrink-0 align-middle items-center justify-center rounded-[5px] border transition-colors cursor-pointer active:scale-90 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-foreground/15',
                      selectedViewkeys.has(v.viewkey)
                        ? 'border-primary bg-primary text-primary-foreground shadow-xs'
                        : 'border-input bg-card hover:border-foreground/40',
                    ]"
                    @click="toggleSelectVideo(v.viewkey)"
                  >
                    <svg
                      v-if="selectedViewkeys.has(v.viewkey)"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      stroke-width="2.5"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                      class="h-3 w-3"
                      aria-hidden="true"
                    >
                      <path d="M20 6 9 17l-5-5" />
                    </svg>
                  </button>
                </td>
                <td class="px-5 py-3.5 max-w-0 w-full">
                  <div class="font-medium text-foreground truncate" :title="v.title">
                    {{ v.title || v.viewkey }}
                  </div>
                  <div class="flex items-center gap-1.5 text-[11px] font-mono text-muted-foreground mt-0.5 tabular-nums">
                    <span>{{ v.viewkey }}</span>
                    <template v-if="formatPublishedDate(v.published_at)">
                      <span class="opacity-45 select-none">·</span>
                      <span title="视频发布日期">{{ formatPublishedDate(v.published_at) }}</span>
                    </template>
                    <template v-if="formatVideoDuration(v.duration_seconds)">
                      <span class="opacity-45 select-none">·</span>
                      <span title="视频时长">{{ formatVideoDuration(v.duration_seconds) }}</span>
                    </template>
                  </div>
                </td>
                <td class="px-5 py-3.5 text-xs text-foreground/80 whitespace-nowrap">
                  <div class="truncate max-w-[140px]" :title="v.uploader_display_name || v.uploader">
                    {{ v.uploader_display_name || v.uploader || '—' }}
                  </div>
                </td>
                <td class="px-5 py-3.5 whitespace-nowrap">
                  <span
                    v-if="v.quality"
                    class="inline-flex items-center rounded-full bg-secondary px-2.5 py-0.5 font-mono text-[11px] text-secondary-foreground"
                  >
                    {{ v.quality }}
                  </span>
                  <span v-else class="text-xs text-muted-foreground">—</span>
                </td>
                <td class="px-5 py-3.5 whitespace-nowrap">
                  <span
                    v-if="v.file_size > 0"
                    class="inline-flex items-center rounded-full border border-border bg-surface-subtle px-2.5 py-0.5 text-[11px] tabular-nums text-muted-foreground"
                  >
                    {{ formatBytes(v.file_size) }}
                  </span>
                  <span v-else class="text-xs text-muted-foreground">—</span>
                </td>
                <td class="px-5 py-3.5 whitespace-nowrap">
                  <span class="inline-flex items-center gap-1.5 rounded-full border border-border bg-card/90 text-foreground px-2.5 py-0.5 text-xs font-medium whitespace-nowrap">
                    <span :class="['w-1.5 h-1.5 rounded-full shrink-0', videoStatusMeta(v.status).dot]" />
                    {{ videoStatusMeta(v.status).label }}
                  </span>
                </td>
                <td class="px-5 py-3.5 text-xs text-muted-foreground tabular-nums whitespace-nowrap">
                  {{ formatDateTime(v.uploaded_at || v.downloaded_at || v.created_at) }}
                </td>
                <td class="px-5 py-3.5 whitespace-nowrap">
                  <div class="inline-flex items-center gap-1.5">
                    <button
                      type="button"
                      class="inline-flex items-center justify-center h-7 w-7 rounded-lg bg-secondary text-secondary-foreground border border-transparent hover:border-brand/35 hover:bg-brand/12 hover:text-brand aria-expanded:border-brand/35 aria-expanded:bg-brand/12 aria-expanded:text-brand transition-colors cursor-pointer"
                      aria-label="在线播放"
                      aria-haspopup="dialog"
                      :aria-expanded="playingVideo?.viewkey === v.viewkey"
                      title="在线播放"
                      @click="openPlayback(v, $event)"
                    >
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="w-3.5 h-3.5" aria-hidden="true">
                        <polygon points="6 3 20 12 6 21 6 3" />
                      </svg>
                    </button>
                    <a
                      v-if="v.video_page_url"
                      :href="v.video_page_url"
                      target="_blank"
                      rel="noopener noreferrer"
                      class="inline-flex items-center justify-center h-7 w-7 rounded-lg text-muted-foreground hover:bg-accent hover:text-foreground transition-colors"
                      title="打开原网页"
                    >
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="w-3.5 h-3.5" aria-hidden="true">
                        <path d="M15 3h6v6" /><path d="M10 14 21 3" /><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
                      </svg>
                    </a>
                    <button
                      type="button"
                      class="inline-flex items-center justify-center h-7 w-7 rounded-lg text-muted-foreground hover:text-brand hover:bg-brand/10 transition-colors cursor-pointer"
                      title="强制重新下载"
                      @click="handleRedownload(v)"
                    >
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="w-3.5 h-3.5" aria-hidden="true">
                        <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" /><path d="M3 3v5h5" />
                      </svg>
                    </button>
                    <ConfirmPopover
                      v-if="activeDeleteKey === v.viewkey"
                      :title="`从去重库删除 [${v.viewkey}]？`"
                      description="删除后自动追更将重新视该视频为未下载。"
                      confirm-text="删除"
                      :default-open="deletePopoverDefaultOpen"
                      @open-change="(val) => { if (val) deleteLocalSingle = false; else deletePopoverDefaultOpen = false }"
                      @confirm="handleDelete(v)"
                    >
                      <template v-if="v.status === 'downloaded' || v.local_path" #extra>
                        <label class="flex items-center gap-2 text-xs leading-4 text-foreground cursor-pointer select-none">
                          <Checkbox v-model="deleteLocalSingle" />
                          <span>同时删除本地已下载的视频文件</span>
                        </label>
                      </template>
                      <button
                        type="button"
                        class="inline-flex items-center justify-center h-7 w-7 rounded-lg text-muted-foreground hover:text-red-500 hover:bg-red-500/10 transition-colors cursor-pointer"
                        title="删除去重记录"
                      >
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="w-3.5 h-3.5" aria-hidden="true">
                          <path d="M3 6h18" /><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6" /><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2" /><line x1="10" x2="10" y1="11" y2="17" /><line x1="14" x2="14" y1="11" y2="17" />
                        </svg>
                      </button>
                    </ConfirmPopover>
                    <button
                      v-else
                      type="button"
                      class="inline-flex items-center justify-center h-7 w-7 rounded-lg text-muted-foreground hover:text-red-500 hover:bg-red-500/10 transition-colors cursor-pointer"
                      title="删除去重记录"
                      @pointerenter="warmDeleteRowPopover(v.viewkey)"
                      @focus="warmDeleteRowPopover(v.viewkey)"
                      @click="openDeleteRowPopoverNow(v.viewkey)"
                    >
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="w-3.5 h-3.5" aria-hidden="true">
                        <path d="M3 6h18" /><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6" /><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2" /><line x1="10" x2="10" y1="11" y2="17" /><line x1="14" x2="14" y1="11" y2="17" />
                      </svg>
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- Pagination Bar -->
        <div class="px-5 py-3.5 border-t border-border bg-surface-subtle flex items-center justify-between text-xs text-muted-foreground">
          <span class="tabular-nums">共 {{ total }} 条 · 第 {{ filters.page }} / {{ totalPages }} 页</span>
          <div class="flex items-center gap-1.5">
            <Button
              variant="outline"
              size="xs"
              :disabled="filters.page <= 1"
              @pointerenter="prefetchPage(filters.page - 1)"
              @focus="prefetchPage(filters.page - 1)"
              @click="goPage(filters.page - 1)"
            >
              <ChevronLeft class="w-3.5 h-3.5" />
              上一页
            </Button>
            <Button
              variant="outline"
              size="xs"
              :disabled="filters.page >= totalPages"
              @pointerenter="prefetchPage(filters.page + 1)"
              @focus="prefetchPage(filters.page + 1)"
              @click="goPage(filters.page + 1)"
            >
              下一页
              <ChevronRight class="w-3.5 h-3.5" />
            </Button>
          </div>
        </div>
      </Card>
    </div>

    <VideoPlaybackDialog v-if="playingVideo" :video="playingVideo" @close="closePlayback" />
  </div>
</template>
