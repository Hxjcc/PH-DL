<script setup lang="ts">
import { getErrorMessage } from '../utils/errors'
import { computed, onActivated, onDeactivated, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter, type LocationQuery } from 'vue-router'
import {
  Plus,
  RefreshCw,
  Search,
  Trash2,
  Users,
  BookmarkCheck,
  Clock,
  Play,
  Eye,
  Loader2,
  LayoutGrid,
  List,
  Heart,
  Bookmark,
  AlertCircle,
  CheckCircle2,
  PauseCircle,
  Sparkles,
  Pencil,
  ExternalLink,
  ChevronLeft,
  ChevronRight,
  Film,
} from 'lucide-vue-next'
import { api } from '../api/client'
import type { CreateTargetRequest, RuntimeSettings, SiteKey, SubscriptionItem, TargetRecord, TargetType, TargetTypeInput, UpdateTargetRequest } from '../types/api'
import { useAppStore } from '../stores/app'
import { avatarGradient, formatDateTime, siteMeta, targetTypeMeta } from '../utils/format'
import { buildTargetProfileVideosUrl, extractTargetFromInput, parseTargetUsernames } from '../utils/targetInput'
import {
  TARGETS_PAGE_SIZES,
  readListPreference,
  saveListPreference,
} from '../utils/listPreferences'
import Card from '../components/ui/Card.vue'
import Button from '../components/ui/Button.vue'
import Badge from '../components/ui/Badge.vue'
import Input from '../components/ui/Input.vue'
import Textarea from '../components/ui/Textarea.vue'
import Select from '../components/ui/Select.vue'
import Switch from '../components/ui/Switch.vue'
import Dialog from '../components/ui/Dialog.vue'
import ConfirmPopover from '../components/ui/ConfirmPopover.vue'
import SubsPickerModal from '../components/SubsPickerModal.vue'
import BaselineScopeFields from '../components/BaselineScopeFields.vue'
import SiteBadge from '../components/SiteBadge.vue'
import SegmentedControl from '../components/ui/SegmentedControl.vue'

const store = useAppStore()
const route = useRoute()
const router = useRouter()

type TargetSiteFilter = SiteKey | 'all'

function parseTargetsRouteQuery(query: LocationQuery = {}): { site: TargetSiteFilter; page: number } {
  const rawSite = Array.isArray(query.site) ? query.site[0] : query.site
  const normSite = typeof rawSite === 'string' ? rawSite.trim().toLowerCase() : ''
  const rawPage = Array.isArray(query.page) ? query.page[0] : query.page
  const parsedPage = Number.parseInt(typeof rawPage === 'string' ? rawPage : '', 10)
  return {
    site: normSite === 'pornhub' || normSite === 'xhamster' ? normSite : 'all',
    page: Number.isFinite(parsedPage) && parsedPage > 1 ? parsedPage : 1,
  }
}

const initialTargetsQuery: ReturnType<typeof parseTargetsRouteQuery> =
  route.name === 'targets' ? parseTargetsRouteQuery(route.query) : { site: 'all', page: 1 }

const VIEW_MODE_KEY = 'phdl-targets-view'
const SORT_BY_KEY = 'phdl-targets-sort'
const TARGET_SORT_OPTIONS = [
  { label: '按名称 A-Z', value: 'name_asc' },
  { label: '最新加入', value: 'created_desc' },
  { label: '最早加入', value: 'created_asc' },
  { label: '视频最多', value: 'videos_desc' },
] as const
type TargetSort = typeof TARGET_SORT_OPTIONS[number]['value']
const targetPageSizeOptions = TARGETS_PAGE_SIZES.map((value) => ({
  label: value === 0 ? '全部显示' : `每页 ${value} 个`,
  value,
}))

function loadInitialViewMode() {
  try {
    const saved = localStorage.getItem(VIEW_MODE_KEY)
    if (saved === 'table' || saved === 'grid') return saved
  } catch {}
  return 'grid'
}

function loadInitialSortBy(): TargetSort {
  try {
    const saved = localStorage.getItem(SORT_BY_KEY)
    const option = TARGET_SORT_OPTIONS.find((opt) => opt.value === saved)
    if (option) return option.value
  } catch {}
  return 'name_asc'
}

const targets = ref(store.targetsCache ? [...store.targetsCache] : [])
const loading = ref(false)
const search = ref('')
const siteFilter = ref(initialTargetsQuery.site)
const statusFilter = ref('all')
const sortBy = ref(loadInitialSortBy())
const pageSize = ref(readListPreference('targets', TARGETS_PAGE_SIZES, 24))
const currentPage = ref(initialTargetsQuery.page)
const viewMode = ref(loadInitialViewMode())
const subsModalOpen = ref(false)
const addModalOpen = ref(false)
const adding = ref(false)
const updatingTargetId = ref<number | null>(null)
const newBaselineValid = ref(true)
const baselineModalOpen = ref(false)
const baselineTarget = ref<TargetRecord | null>(null)
const baselinePages = ref('all')
const baselinePagesValid = ref(true)
const baselineSubmitting = ref(false)
const editModalOpen = ref(false)
const editingSubmitting = ref(false)
const editForm = reactive<{
  id: number | null
  site: SiteKey
  username: string
  original_username: string
  original_type: TargetType
  display_name: string
  target_type: TargetType
  preferred_quality: string
}>({
  id: null,
  site: 'pornhub',
  username: '',
  original_username: '',
  original_type: 'model',
  display_name: '',
  target_type: 'model',
  preferred_quality: '',
})
const SITE_CHOICES = [
  { label: 'PornHub', value: 'pornhub', dot: 'bg-[#ff9000]' },
  { label: 'xHamster', value: 'xhamster', dot: 'bg-red-500' },
] as const
const PH_EDIT_TYPE_OPTIONS = [
  { label: 'Model', value: 'model' },
  { label: 'Pornstar', value: 'pornstar' },
  { label: 'Favorites (收藏夹)', value: 'favorites' },
] as const
const XH_EDIT_TYPE_OPTIONS = [
  { label: 'Creator', value: 'creator' },
  { label: 'Pornstar', value: 'pornstar' },
  { label: 'User', value: 'users' },
  { label: 'Channel', value: 'channel' },
] as const
const editTargetTypeOptions = computed(() =>
  editForm.site === 'xhamster' ? XH_EDIT_TYPE_OPTIONS : PH_EDIT_TYPE_OPTIONS
)
const isEditLockedCollection = computed(
  () => editForm.target_type === 'liked' || (editForm.site === 'xhamster' && editForm.target_type === 'favorites')
)
const qualityPresetChoices = ['2160', '1440', '1080', '720', '480']

const schedule = reactive<{ enabled: boolean; time: string; scan_mode: RuntimeSettings['update_scan_mode'] }>({
  enabled:
    store.settingsCache !== null
      ? Boolean(store.settingsCache.schedule_enabled)
      : Boolean(store.overview?.schedule?.enabled ?? true),
  time:
    store.settingsCache?.schedule_time ||
    store.overview?.schedule?.time ||
    '04:00',
  scan_mode:
    store.settingsCache?.update_scan_mode ||
    store.overview?.schedule?.scan_mode ||
    'catch_up',
})

const UPDATE_SCAN_MODES = [
  { value: 'catch_up', label: '智能衔接', title: '从首页开始，衔接已知视频后停止（速度快，适合高频追更）' },
  { value: 'full', label: '全量翻页', title: '每次翻完目标全部列表页，不错过重新公开或补漏的旧视频（已知视频直接跳过，不重复解析详情）' },
] as const

const newTarget = reactive<Required<Pick<CreateTargetRequest,
  'site' | 'username' | 'target_type' | 'preferred_quality' | 'init_baseline' | 'baseline_pages'
>>>({
  site: 'pornhub',
  username: '',
  target_type: 'auto',
  preferred_quality: '',
  init_baseline: true,
  baseline_pages: 'all',
})

const PH_TARGET_TYPE_OPTIONS = [
  { label: '自动识别', value: 'auto' },
  { label: 'Model', value: 'model' },
  { label: 'Pornstar', value: 'pornstar' },
  { label: 'Favorites (收藏夹)', value: 'favorites' },
  { label: 'Liked (我的点赞)', value: 'liked' },
] as const

const XH_TARGET_TYPE_OPTIONS = [
  { label: '自动识别', value: 'auto' },
  { label: 'Creator (创作者)', value: 'creator' },
  { label: 'Pornstar (明星)', value: 'pornstar' },
  { label: 'User (普通用户)', value: 'users' },
  { label: 'Channel (频道)', value: 'channel' },
  { label: 'Favorites (我的收藏)', value: 'favorites' },
] as const

const targetTypeOptions = computed(() =>
  newTarget.site === 'xhamster' ? XH_TARGET_TYPE_OPTIONS : PH_TARGET_TYPE_OPTIONS
)

const isNewSelfCollection = computed(
  () => newTarget.target_type === 'liked' || (newTarget.site === 'xhamster' && newTarget.target_type === 'favorites')
)

watch(
  () => newTarget.site,
  (nextSite) => {
    const valid = new Set<TargetTypeInput>((nextSite === 'xhamster' ? XH_TARGET_TYPE_OPTIONS : PH_TARGET_TYPE_OPTIONS).map((o) => o.value))
    if (!valid.has(newTarget.target_type)) {
      newTarget.target_type = 'auto'
    }
  }
)

watch(
  () => newTarget.username,
  (val) => {
    const text = String(val || '').trim()
    if (!text || /[\s,，;；]/.test(text)) return
    const detected = extractTargetFromInput(text)
    if (!detected) return
    if (detected.site !== newTarget.site) {
      newTarget.site = detected.site
    }
    if (detected.target_type === 'liked' || (detected.site === 'xhamster' && detected.target_type === 'favorites')) {
      newTarget.target_type = detected.target_type
      newTarget.username = ''
    } else if (detected.target_type) {
      newTarget.target_type = detected.target_type
    }
  }
)

function isLocalToday(iso: string | null | undefined) {
  if (!iso) return false
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return false
  const now = new Date()
  return (
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  )
}

function targetTodayNewCount(t: TargetRecord) {
  if (typeof t?.today_new_count === 'number') {
    return Math.max(0, t.today_new_count)
  }
  return Number(t?.last_new_count || 0) > 0 && isLocalToday(t?.last_checked_at)
    ? Number(t.last_new_count)
    : 0
}

function isUpdatedToday(t: TargetRecord) {
  return targetTodayNewCount(t) > 0
}

function targetNewBadgeTitle(t: TargetRecord) {
  const today = targetTodayNewCount(t)
  const lastNew = Number(t?.last_new_count || 0)
  const parts = [`今日扫描新增 ${today} 个视频（仅统计当天追更/检查更新发现，次日自动清零）`]
  if (lastNew > 0 && lastNew !== today && isLocalToday(t?.last_checked_at)) {
    parts.push(`最近一次扫描 +${lastNew}`)
  }
  return parts.join(' · ')
}

const siteScopedTargets = computed(() =>
  siteFilter.value === 'all'
    ? targets.value
    : targets.value.filter((t) => (t.site || 'pornhub') === siteFilter.value)
)
const phTargetCount = computed(() => targets.value.filter((t) => (t.site || 'pornhub') === 'pornhub').length)
const xhTargetCount = computed(() => targets.value.filter((t) => (t.site || 'pornhub') === 'xhamster').length)

const siteFilterTabs = computed(() => [
  { value: 'all', label: '全站', title: '显示全部站点目标' },
  { value: 'pornhub', label: 'PH', dot: 'bg-[#ff9000]', count: phTargetCount.value, title: '仅显示 PornHub 目标' },
  { value: 'xhamster', label: 'XH', dot: 'bg-red-500', count: xhTargetCount.value, title: '仅显示 xHamster 目标' },
] as const)

const enabledCount = computed(() => siteScopedTargets.value.filter((t) => t.enabled).length)
const disabledCount = computed(() => siteScopedTargets.value.filter((t) => !t.enabled).length)
const updatedTodayCount = computed(() => siteScopedTargets.value.filter((t) => isUpdatedToday(t)).length)
const errorCount = computed(() => siteScopedTargets.value.filter((t) => Boolean(t.last_error)).length)

const statusPills = computed(() => [
  { value: 'all', label: '全部', count: siteScopedTargets.value.length, icon: Users },
  { value: 'updated_today', label: '今日更新', count: updatedTodayCount.value, icon: Sparkles },
  { value: 'enabled', label: '已启用', count: enabledCount.value, icon: CheckCircle2 },
  { value: 'disabled', label: '已暂停', count: disabledCount.value, icon: PauseCircle },
  ...(errorCount.value > 0
    ? [{ value: 'error', label: '有异常', count: errorCount.value, icon: AlertCircle }]
    : []),
])

function targetVideoCount(t: TargetRecord) {
  return Math.max(0, Number(t?.video_total_count || 0))
}

function targetVideoCountTitle(t: TargetRecord) {
  const total = targetVideoCount(t)
  const saved = Math.max(0, Number(t?.video_saved_count || 0))
  const baseline = Math.max(0, Number(t?.video_baseline_count || 0))
  if (total === 0) return '去重索引中暂无该目标的视频记录'
  return `共索引 ${total} 个视频（已入库 ${saved} · 仅基线 ${baseline}），点击前往历史记录查看`
}

function openTargetHistory(t: TargetRecord) {
  const site = t.site || 'pornhub'
  const uploader =
    t.target_type === 'liked'
      ? 'My_Liked'
      : site === 'xhamster' && t.target_type === 'favorites'
        ? 'My_Favorites'
        : String(t.username || '').trim()
  if (!uploader) return
  router.push({ name: 'history', query: { uploader, site } })
}

function parseCreatedTs(iso: string | null | undefined) {
  if (!iso) return 0
  const ts = Date.parse(iso)
  return Number.isNaN(ts) ? 0 : ts
}

const filteredTargets = computed(() => {
  const q = search.value.trim().toLowerCase()
  const list = targets.value.filter((t) => {
    const tSite = t.site || 'pornhub'
    if (siteFilter.value !== 'all' && tSite !== siteFilter.value) return false
    if (statusFilter.value === 'updated_today' && !isUpdatedToday(t)) return false
    if (statusFilter.value === 'enabled' && !t.enabled) return false
    if (statusFilter.value === 'disabled' && t.enabled) return false
    if (statusFilter.value === 'error' && !t.last_error) return false
    if (q) {
      const names = [targetDisplayName(t), t.username, t.display_name, siteMeta(tSite).label]
        .map((value) => String(value || '').toLowerCase())
      const typeLabel = String(targetTypeMeta(t.target_type) || '').toLowerCase()
      if (!names.some((name) => name.includes(q)) && !typeLabel.includes(q)) return false
    }
    return true
  })

  if (sortBy.value === 'created_desc') {
    return list.toSorted((a, b) => {
      const diff = parseCreatedTs(b.created_at) - parseCreatedTs(a.created_at)
      if (diff !== 0) return diff
      return Number(b.id || 0) - Number(a.id || 0)
    })
  }
  if (sortBy.value === 'created_asc') {
    return list.toSorted((a, b) => {
      const diff = parseCreatedTs(a.created_at) - parseCreatedTs(b.created_at)
      if (diff !== 0) return diff
      return Number(a.id || 0) - Number(b.id || 0)
    })
  }
  if (sortBy.value === 'videos_desc') {
    return list.toSorted((a, b) => {
      const diff = targetVideoCount(b) - targetVideoCount(a)
      if (diff !== 0) return diff
      return targetDisplayName(a).toLowerCase().localeCompare(targetDisplayName(b).toLowerCase())
    })
  }
  if (statusFilter.value === 'updated_today') {
    return list.toSorted((a, b) => {
      const diff = targetTodayNewCount(b) - targetTodayNewCount(a)
      if (diff !== 0) return diff
      return String(b.last_checked_at || '').localeCompare(String(a.last_checked_at || ''))
    })
  }
  return list.toSorted((a, b) => {
    const diff = targetDisplayName(a).toLowerCase().localeCompare(targetDisplayName(b).toLowerCase())
    if (diff !== 0) return diff
    return Number(a.id || 0) - Number(b.id || 0)
  })
})

const totalPages = computed(() => {
  if (pageSize.value <= 0) return 1
  return Math.max(1, Math.ceil(filteredTargets.value.length / pageSize.value))
})

const pagedTargets = computed(() => {
  if (pageSize.value <= 0) return filteredTargets.value
  const safePage = Math.min(Math.max(1, currentPage.value), totalPages.value)
  const start = (safePage - 1) * pageSize.value
  return filteredTargets.value.slice(start, start + pageSize.value)
})

let applyingRouteQuery = false

function syncTargetsQueryToUrl() {
  if (route.name !== 'targets' || applyingRouteQuery) return
  const nextQuery: LocationQuery = {}
  if (siteFilter.value !== 'all') nextQuery.site = siteFilter.value
  if (currentPage.value > 1) nextQuery.page = String(currentPage.value)
  const curSite = Array.isArray(route.query.site) ? route.query.site[0] : route.query.site
  const curPage = Array.isArray(route.query.page) ? route.query.page[0] : route.query.page
  if ((curSite || undefined) === nextQuery.site && (curPage || undefined) === nextQuery.page) return
  router.replace({ name: 'targets', query: nextQuery }).catch(() => {})
}

watch([search, siteFilter, statusFilter, sortBy, pageSize], () => {
  if (!applyingRouteQuery) {
    currentPage.value = 1
  }
})

watch([siteFilter, currentPage], () => {
  syncTargetsQueryToUrl()
})

watch(
  () => [route.name, route.query.site, route.query.page],
  ([name]) => {
    if (name !== 'targets') return
    const parsed = parseTargetsRouteQuery(route.query)
    if (parsed.site === siteFilter.value && parsed.page === currentPage.value) return
    applyingRouteQuery = true
    siteFilter.value = parsed.site
    currentPage.value = parsed.page
    Promise.resolve().then(() => {
      applyingRouteQuery = false
    })
  }
)

watch(totalPages, (maxPage) => {
  if (targets.value.length === 0 && store.targetsCache === null) return
  if (currentPage.value > maxPage) {
    currentPage.value = maxPage
  }
})

function setSortBy(value: TargetSort) {
  sortBy.value = value
  try {
    localStorage.setItem(SORT_BY_KEY, value)
  } catch {}
}

function setPageSize(value: string) {
  const num = Number(value)
  pageSize.value = TARGETS_PAGE_SIZES.includes(num) ? num : 24
  saveListPreference('targets', pageSize.value)
}

function goTargetPage(nextPage: number) {
  const clamped = Math.min(Math.max(1, nextPage), totalPages.value)
  if (clamped === currentPage.value) return
  currentPage.value = clamped
}

function setViewMode(mode: 'table' | 'grid') {
  viewMode.value = mode
  try {
    localStorage.setItem(VIEW_MODE_KEY, mode)
  } catch {}
}

function targetDisplayName(t: TargetRecord) {
  if (t.target_type === 'liked') return 'Liked Videos'
  if (t.site === 'xhamster' && t.target_type === 'favorites') {
    return (t.display_name || '').trim() || 'xHamster Favorites'
  }
  return (t.display_name || '').trim() || t.username
}

function targetInitial(t: TargetRecord) {
  const name = targetDisplayName(t).trim()
  return (name[0] || 'P').toUpperCase()
}

function handleAvatarError(t: TargetRecord) {
  t.avatar_url = ''
}

let loadingTimer: ReturnType<typeof setTimeout> | null = null
let activatedOnce = false

async function loadData({ silent = false } = {}) {
  if (loadingTimer !== null) clearTimeout(loadingTimer)
  if (!silent && store.targetsCache === null && targets.value.length === 0) {
    loadingTimer = setTimeout(() => {
      loading.value = true
    }, 120)
  }
  try {
    await Promise.all([store.refreshTargets(), store.refreshSettings()])
  } catch (err: unknown) {
    store.notify(getErrorMessage(err), 'error')
  } finally {
    if (loadingTimer !== null) clearTimeout(loadingTimer)
    loading.value = false
  }
}

watch(
  () => store.targetsCache,
  (val) => {
    if (Array.isArray(val)) {
      targets.value = [...val]
    }
  }
)

watch(
  () => store.settingsCache,
  (val, previous) => {
    if (val) {
      if (!previous || schedule.enabled === Boolean(previous.schedule_enabled)) {
        schedule.enabled = Boolean(val.schedule_enabled)
      }
      if (!previous || schedule.time === (previous.schedule_time || '04:00')) {
        schedule.time = val.schedule_time || '04:00'
      }
      if (!previous || schedule.scan_mode === previous.update_scan_mode) {
        schedule.scan_mode = val.update_scan_mode === 'full' ? 'full' : 'catch_up'
      }
    }
  }
)

onActivated(() => {
  if (!activatedOnce) {
    activatedOnce = true
    if (store.hydratedFromCache) return
    if (store.targetsCache === null || store.settingsCache === null) {
      loadData({ silent: false })
    } else if (Date.now() - Math.min(store.targetsLoadedAt, store.settingsLoadedAt) > 1500) {
      loadData({ silent: true })
    }
    return
  }
  if (Date.now() - Math.min(store.targetsLoadedAt, store.settingsLoadedAt) > 1500) loadData({ silent: true })
})

function clearLoadingTimer() {
  if (loadingTimer !== null) clearTimeout(loadingTimer)
  loading.value = false
}

onDeactivated(clearLoadingTimer)
onUnmounted(clearLoadingTimer)

async function saveSchedule() {
  if (!schedule.time) return false
  try {
    const res = await api.saveSettings({
      schedule_enabled: schedule.enabled,
      schedule_time: schedule.time,
      update_scan_mode: schedule.scan_mode,
    })
    if (res?.settings) {
      store.setSettingsCache(res.settings)
    }
    store.notify('追更策略已保存', 'success')
    await store.refreshOverview()
    return true
  } catch (err: unknown) {
    store.notify(getErrorMessage(err), 'error')
    return false
  }
}

async function setScanMode(mode: RuntimeSettings['update_scan_mode']) {
  if (schedule.scan_mode === mode) return
  const prev = schedule.scan_mode
  schedule.scan_mode = mode
  const ok = await saveSchedule()
  if (!ok) {
    schedule.scan_mode = prev
  }
}

const parsedNewUsernames = computed(() => parseTargetUsernames(newTarget.username))

async function handleAddTarget() {
  if (newTarget.init_baseline && !newBaselineValid.value) return
  if (!isNewSelfCollection.value && parsedNewUsernames.value.length === 0) {
    store.notify(
      newTarget.username.trim()
        ? '未识别到有效的博主用户名（请勿粘贴视频链接）'
        : '请输入至少一个用户名',
      'warning'
    )
    return
  }
  adding.value = true
  try {
    if (isNewSelfCollection.value || parsedNewUsernames.value.length === 1) {
      const singleUsername = isNewSelfCollection.value ? '' : parsedNewUsernames.value[0]
      const res = await api.createTarget({ ...newTarget, username: singleUsername })
      const addedName = res.target.display_name || res.target.username
      store.notify(
        res.baseline_job
          ? `已添加 '${addedName}' 并启动基线任务 #${res.baseline_job.id}`
          : `已添加追更目标 '${addedName}'`,
        'success'
      )
    } else {
      const res = await api.batchCreateTargets({
        items: parsedNewUsernames.value.map((username) => ({
          site: newTarget.site,
          username,
          target_type: newTarget.target_type,
          preferred_quality: newTarget.preferred_quality.trim(),
        })),
        init_baseline: newTarget.init_baseline,
        baseline_pages: newTarget.baseline_pages,
      })
      const baselineCount = (res.baseline_jobs || []).length
      const skipped = (res.skipped_channels || []).length
      const skipNote = skipped > 0 ? `（已跳过 ${skipped} 个不支持的 Channel）` : ''
      store.notify(
        baselineCount > 0
          ? `已批量添加 ${res.created_count} 个追更目标，并创建 ${baselineCount} 个基线任务${skipNote}`
          : `已批量添加 ${res.created_count} 个追更目标${skipNote}`,
        'success'
      )
      await store.refreshJobs()
    }
    addModalOpen.value = false
    newTarget.username = ''
    newTarget.preferred_quality = ''
    await loadData({ silent: true })
  } catch (err: unknown) {
    store.notify(getErrorMessage(err), 'error')
  } finally {
    adding.value = false
  }
}

async function handleImportSubs(
  picked: SubscriptionItem[],
  opts: { site?: SiteKey; initBaseline?: boolean; baselinePages?: string } = {},
) {
  const initBaseline = Boolean(opts?.initBaseline)
  try {
    const res = await api.batchCreateTargets({
      items: picked.map((p) => ({
        site: p.site || opts?.site || 'pornhub',
        username: p.username,
        target_type: p.type,
        display_name: p.display_name || p.username,
        avatar_url: p.avatar_url || '',
      })),
      init_baseline: initBaseline,
      baseline_pages: opts?.baselinePages || 'all',
    })
    const baselineCount = (res.baseline_jobs || []).length
    store.notify(
      baselineCount > 0
        ? `已批量导入 ${res.created_count} 个追更目标，并创建 ${baselineCount} 个基线任务`
        : `已批量导入 ${res.created_count} 个追更目标`,
      'success'
    )
    await Promise.all([loadData({ silent: true }), store.refreshJobs()])
  } catch (err: unknown) {
    store.notify(getErrorMessage(err), 'error')
  }
}

async function toggleEnabled(t: TargetRecord, val: boolean) {
  const prev = t.enabled
  t.enabled = val
  try {
    const res = await api.updateTarget(t.id, { enabled: val })
    t.enabled = res.target.enabled
    store.setTargetsCache(targets.value)
  } catch (err: unknown) {
    t.enabled = prev
    store.notify(getErrorMessage(err), 'error')
  }
}

async function triggerUpdate(targetIds: number[] | null, autoDownload: boolean) {
  if (Array.isArray(targetIds) && targetIds.length === 1) {
    updatingTargetId.value = targetIds[0]
  }
  try {
    const res = await api.runUpdate({ target_ids: targetIds, auto_download: autoDownload })
    store.notify(`已创建任务 #${res.job.id}`, 'success')
    await store.refreshJobs()
    router.push({ name: 'jobs' })
  } catch (err: unknown) {
    store.notify(getErrorMessage(err), 'error')
  } finally {
    updatingTargetId.value = null
  }
}

function openBaselineDialog(target: TargetRecord) {
  baselineTarget.value = target
  baselinePages.value = 'all'
  baselinePagesValid.value = true
  baselineModalOpen.value = true
}

function openEditDialog(target: TargetRecord) {
  editForm.id = target.id
  editForm.site = target.site || 'pornhub'
  editForm.username = target.username
  editForm.original_username = target.username
  editForm.original_type = target.target_type
  editForm.display_name = target.display_name || ''
  editForm.target_type = target.target_type
  editForm.preferred_quality = target.preferred_quality || ''
  editModalOpen.value = true
}

function toggleEditQuality(q: string) {
  const parts = new Set(
    String(editForm.preferred_quality || '')
      .split(',')
      .map((s) => s.trim().replace(/p$/i, ''))
      .filter(Boolean)
  )
  if (parts.has(q)) parts.delete(q)
  else parts.add(q)
  editForm.preferred_quality = Array.from(parts)
    .sort((a, b) => Number(b) - Number(a))
    .join(',')
}

function isEditQualitySelected(q: string) {
  return String(editForm.preferred_quality || '')
    .split(',')
    .map((s) => s.trim().replace(/p$/i, ''))
    .includes(q)
}

async function handleSaveTargetEdit() {
  if (!editForm.id || editingSubmitting.value) return
  const payload: UpdateTargetRequest = {
    display_name: editForm.display_name.trim(),
    preferred_quality: editForm.preferred_quality.trim(),
  }
  let identityChanged = false
  if (!isEditLockedCollection.value) {
    const rawUsername = String(editForm.username || '').trim()
    const parsed = parseTargetUsernames(rawUsername)
    const normalizedUsername = parsed.length === 1 ? parsed[0] : rawUsername
    if (!normalizedUsername || /[/\\:?#\s]/.test(normalizedUsername)) {
      store.notify('请输入有效的博主用户名（或直接粘贴主页链接）', 'warning')
      return
    }
    payload.username = normalizedUsername
    payload.target_type = editForm.target_type
    identityChanged =
      normalizedUsername.toLowerCase() !== String(editForm.original_username || '').toLowerCase() ||
      editForm.target_type !== editForm.original_type
  }
  editingSubmitting.value = true
  try {
    const res = await api.updateTarget(editForm.id, payload)
    if (identityChanged) {
      await loadData({ silent: true })
    } else {
      const idx = targets.value.findIndex((x) => x.id === editForm.id)
      if (idx >= 0 && res?.target) {
        targets.value[idx] = { ...targets.value[idx], ...res.target }
        store.setTargetsCache(targets.value)
      } else {
        await loadData({ silent: true })
      }
    }
    editModalOpen.value = false
    store.notify(identityChanged ? '监控目标标识与历史归属已更新' : '监控目标配置已更新', 'success')
  } catch (err: unknown) {
    store.notify(getErrorMessage(err), 'error')
  } finally {
    editingSubmitting.value = false
  }
}

async function handleSyncBaseline() {
  if (!baselineTarget.value || !baselinePagesValid.value || baselineSubmitting.value) return
  baselineSubmitting.value = true
  try {
    const res = await api.syncTargetBaseline(baselineTarget.value.id, baselinePages.value)
    store.notify(`已启动基线同步任务 #${res.job.id}`, 'success')
    baselineModalOpen.value = false
    await store.refreshJobs()
  } catch (err: unknown) {
    store.notify(getErrorMessage(err), 'error')
  } finally {
    baselineSubmitting.value = false
  }
}

async function handleDelete(t: TargetRecord) {
  try {
    await api.deleteTarget(t.id)
    targets.value = targets.value.filter((x) => x.id !== t.id)
    store.setTargetsCache(targets.value)
    store.notify(`已移除 '${targetDisplayName(t)}'`, 'info')
  } catch (err: unknown) {
    store.notify(getErrorMessage(err), 'error')
  }
}
</script>

<template>
  <div class="space-y-4 sm:space-y-6">
    <!-- Page Header with tinted section icon -->
    <div class="flex flex-wrap items-center justify-between gap-3 sm:gap-4">
      <div class="flex items-center gap-3 sm:gap-3.5 min-w-0">
        <div
          class="w-9 h-9 sm:w-10 sm:h-10 rounded-xl bg-brand/10 text-brand flex items-center justify-center shrink-0 ring-1 ring-brand/15"
        >
          <RefreshCw class="w-4 h-4 sm:w-5 sm:h-5" />
        </div>
        <div class="min-w-0">
          <h1 class="text-lg sm:text-xl font-bold tracking-tight text-foreground">自动追更</h1>
          <p class="text-xs sm:text-sm text-muted-foreground mt-0.5">
            每日定时检查已启用目标的新发布视频，自动下载并上传云端
          </p>
        </div>
      </div>

      <div class="grid grid-cols-2 w-full sm:flex sm:w-auto sm:flex-wrap items-center gap-2">
        <Button variant="outline" size="sm" class="w-full sm:w-auto" @click="triggerUpdate(null, false)">
          <Eye class="w-3.5 h-3.5" />
          仅检查更新
        </Button>
        <Button size="sm" class="w-full sm:w-auto" @click="triggerUpdate(null, true)">
          <Play class="w-3.5 h-3.5" />
          立即全量追更
        </Button>
      </div>
    </div>

    <!-- Compact Schedule Strip -->
    <Card class="px-3.5 sm:px-5 py-3.5 sm:py-4 flex flex-wrap items-center justify-between gap-3 sm:gap-4">
      <div class="flex flex-wrap items-center gap-2.5 sm:gap-3.5">
        <div class="inline-flex items-center gap-2 text-sm font-medium text-foreground">
          <span class="w-7 h-7 rounded-lg bg-brand/10 text-brand flex items-center justify-center shrink-0">
            <Clock class="w-3.5 h-3.5" />
          </span>
          <span>每日定时执行</span>
        </div>
        <Switch v-model="schedule.enabled" @update:model-value="saveSchedule" />
        <input
          v-model="schedule.time"
          type="time"
          :disabled="!schedule.enabled"
          class="h-8 rounded-lg border border-input bg-background px-2.5 text-xs text-foreground tabular-nums shadow-xs transition-all duration-150 hover:border-foreground/20 focus:outline-none focus:border-foreground/35 focus:ring-[3px] focus:ring-foreground/10 disabled:opacity-50"
          @change="saveSchedule"
        />
        <span class="text-xs text-muted-foreground">
          {{ schedule.enabled ? `每天 ${schedule.time} 自动运行` : '定时调度已暂停' }}
        </span>
        <span class="hidden sm:inline-block h-4 w-px bg-border" aria-hidden="true" />
        <SegmentedControl
          :model-value="schedule.scan_mode"
          :options="UPDATE_SCAN_MODES"
          aria-label="追更扫描模式"
          size="sm"
          @change="setScanMode"
        />
      </div>

      <div class="grid grid-cols-2 w-full sm:flex sm:w-auto items-center gap-2">
        <Button variant="outline" size="sm" class="w-full sm:w-auto" @click="subsModalOpen = true">
          <Users class="w-3.5 h-3.5" />
          从订阅导入
        </Button>
        <Button variant="outline" size="sm" class="w-full sm:w-auto" @click="addModalOpen = true">
          <Plus class="w-3.5 h-3.5" />
          添加目标
        </Button>
      </div>
    </Card>

    <!-- Quick Filter Pills + Search + View Switcher (inspired by rust-srec Streamers toolbar) -->
    <div class="space-y-3 sm:space-y-4">
      <div class="flex flex-col sm:flex-row sm:flex-wrap sm:items-center sm:justify-between gap-2.5 sm:gap-3">
        <!-- Pill Filter Strip -->
        <div class="flex items-center gap-1.5 flex-nowrap overflow-x-auto no-scrollbar max-w-full sm:flex-wrap" role="group" aria-label="按目标状态筛选">
          <Button
            v-for="pill in statusPills"
            :key="pill.value"
            variant="outline"
            size="sm"
            :aria-pressed="statusFilter === pill.value"
            :class="[
              'h-8 px-2.5 sm:px-3 text-xs sm:text-sm gap-1.5 rounded-full shrink-0 shadow-none hover:shadow-none',
              statusFilter === pill.value
                ? 'bg-brand/10 dark:bg-brand/15 text-brand border-brand/35 hover:bg-brand/10 hover:border-brand/35 dark:hover:bg-brand/15'
                : 'text-muted-foreground border-border hover:text-foreground',
            ]"
            @click="statusFilter = pill.value"
          >
            <component :is="pill.icon" class="w-3.5 h-3.5" />
            <span>{{ pill.label }}</span>
            <span
              :class="[
                'tabular-nums text-[11px] px-1.5 py-0 rounded-full',
                statusFilter === pill.value
                  ? 'bg-brand/15 text-brand'
                  : pill.value === 'updated_today' && pill.count > 0
                    ? 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 font-semibold'
                    : 'bg-secondary text-muted-foreground',
              ]"
            >
              {{ pill.count }}
            </span>
          </Button>
          <span class="hidden sm:inline-block h-4 w-px bg-border mx-1 shrink-0" aria-hidden="true" />
          <SegmentedControl
            v-model="siteFilter"
            :options="siteFilterTabs"
            aria-label="按站点筛选"
            size="sm"
            class="h-8"
          />
        </div>

        <!-- Search + Sort + Page Size + View Mode Switcher -->
        <div class="flex flex-col sm:flex-row sm:flex-wrap sm:items-center gap-2 w-full sm:w-auto">
          <div class="flex items-center gap-2 w-full sm:w-auto">
            <div class="relative flex-1 sm:w-56">
              <Search class="w-3.5 h-3.5 text-muted-foreground absolute left-3 top-1/2 -translate-y-1/2" />
              <Input v-model="search" placeholder="搜索监控目标..." class="pl-8 rounded-full" />
            </div>

            <div class="inline-flex sm:hidden items-center rounded-lg border border-border bg-card p-0.5 shrink-0" role="group" aria-label="目标视图">
              <Button
                variant="ghost"
                size="icon"
                :aria-pressed="viewMode === 'grid'"
                :class="[
                  'h-7 w-7 rounded-md',
                  viewMode === 'grid'
                    ? 'bg-brand/12 text-brand hover:bg-brand/12'
                    : 'text-muted-foreground hover:text-foreground',
                ]"
                title="卡片视图"
                @click="setViewMode('grid')"
              >
                <LayoutGrid class="w-3.5 h-3.5" />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                :aria-pressed="viewMode === 'table'"
                :class="[
                  'h-7 w-7 rounded-md',
                  viewMode === 'table'
                    ? 'bg-brand/12 text-brand hover:bg-brand/12'
                    : 'text-muted-foreground hover:text-foreground',
                ]"
                title="列表视图"
                @click="setViewMode('table')"
              >
                <List class="w-3.5 h-3.5" />
              </Button>
            </div>
          </div>

          <div class="grid grid-cols-2 gap-2 sm:flex sm:items-center">
            <Select
              :model-value="sortBy"
              :options="TARGET_SORT_OPTIONS"
              aria-label="排序方式"
              class="h-9 w-full sm:w-32 text-xs"
              @update:model-value="setSortBy"
            />
            <Select
              :model-value="pageSize"
              :options="targetPageSizeOptions"
              aria-label="每页显示数量"
              class="h-9 w-full sm:w-28 text-xs"
              @update:model-value="setPageSize"
            />

            <div class="hidden sm:inline-flex items-center rounded-lg border border-border bg-card p-0.5 shrink-0" role="group" aria-label="目标视图">
              <Button
                variant="ghost"
                size="icon"
                :aria-pressed="viewMode === 'grid'"
                :class="[
                  'h-7 w-7 rounded-md',
                  viewMode === 'grid'
                    ? 'bg-brand/12 text-brand hover:bg-brand/12'
                    : 'text-muted-foreground hover:text-foreground',
                ]"
                title="卡片视图"
                @click="setViewMode('grid')"
              >
                <LayoutGrid class="w-3.5 h-3.5" />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                :aria-pressed="viewMode === 'table'"
                :class="[
                  'h-7 w-7 rounded-md',
                  viewMode === 'table'
                    ? 'bg-brand/12 text-brand hover:bg-brand/12'
                    : 'text-muted-foreground hover:text-foreground',
                ]"
                title="列表视图"
                @click="setViewMode('table')"
              >
                <List class="w-3.5 h-3.5" />
              </Button>
            </div>
          </div>
        </div>
      </div>

      <!-- Loading / Empty State -->
      <Card
        v-if="(loading || (store.hydratedFromCache && store.targetsCache === null)) && targets.length === 0"
        class="py-16 flex items-center justify-center gap-2 text-xs text-muted-foreground leading-none"
      >
        <Loader2 class="w-4 h-4 animate-spin shrink-0 text-brand" />
        <span>加载中...</span>
      </Card>

      <Card v-else-if="filteredTargets.length === 0" class="py-16 px-6 flex flex-col items-center text-center">
        <div class="w-11 h-11 rounded-2xl bg-brand/10 text-brand flex items-center justify-center mb-3">
          <Sparkles class="w-5 h-5" />
        </div>
        <div class="text-sm font-medium text-foreground">
          {{
            targets.length === 0
              ? '暂无追更目标'
              : statusFilter === 'updated_today' && !search.trim()
                ? '今天暂无目标更新'
                : '没有匹配的目标'
          }}
        </div>
        <p class="text-xs text-muted-foreground mt-1 max-w-sm">
          {{
            targets.length === 0
              ? '点击上方“添加目标”或“从订阅导入”即可开启每日自动追更'
              : statusFilter === 'updated_today' && !search.trim()
                ? '点击右上角“仅检查更新”或“立即全量追更”检查今日新视频'
                : '尝试调整筛选条件或清空搜索关键词'
          }}
        </p>
      </Card>

      <!-- Card Grid View (Default, inspired by rust-srec) -->
      <div
        v-else-if="viewMode === 'grid'"
        class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3 sm:gap-4"
      >
        <Card
          v-for="(t, idx) in pagedTargets"
          :key="t.id"
          :class="[
            'group p-4 sm:p-5 flex flex-col justify-between gap-3 sm:gap-4 transition-all duration-200 hover:border-brand/35',
            idx >= 12 && 'cv-deferred-target-card',
          ]"
        >
          <!-- Card Top Row: Status Pill + Enable Switch -->
          <div class="space-y-4">
            <div class="flex items-center justify-between gap-2">
              <span
                v-if="t.last_error"
                class="inline-flex items-center gap-1.5 rounded-full border border-red-500/25 bg-red-500/10 px-2.5 py-0.5 text-[11px] font-medium text-red-600 dark:text-red-400"
              >
                <span class="w-1.5 h-1.5 rounded-full bg-red-500 status-dot-glow-red" />
                上次异常
              </span>
              <span
                v-else-if="t.enabled"
                class="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/25 bg-emerald-500/10 px-2.5 py-0.5 text-[11px] font-medium text-emerald-700 dark:text-emerald-400"
              >
                <span class="w-1.5 h-1.5 rounded-full bg-emerald-500 status-dot-glow-emerald" />
                监控中
              </span>
              <span
                v-else
                class="inline-flex items-center gap-1.5 rounded-full border border-border bg-secondary px-2.5 py-0.5 text-[11px] font-medium text-muted-foreground"
              >
                <span class="w-1.5 h-1.5 rounded-full bg-zinc-400" />
                已暂停
              </span>

              <Switch :model-value="Boolean(t.enabled)" @update:model-value="toggleEnabled(t, $event)" />
            </div>

            <!-- Avatar + Name + Meta -->
            <div class="flex items-center gap-3.5 min-w-0">
              <img
                v-if="t.avatar_url"
                :src="t.avatar_url"
                :alt="targetDisplayName(t)"
                class="w-11 h-11 rounded-full object-cover shrink-0 shadow-xs bg-secondary ring-1 ring-border/60"
                loading="lazy"
                referrerpolicy="no-referrer"
                @error="handleAvatarError(t)"
              />
              <div
                v-else
                :class="[
                  'w-11 h-11 rounded-full bg-gradient-to-br text-white flex items-center justify-center font-semibold text-sm shrink-0 shadow-xs',
                  avatarGradient(targetDisplayName(t)),
                ]"
              >
                <Heart v-if="t.target_type === 'liked'" class="w-5 h-5 fill-white/90" />
                <Bookmark v-else-if="t.site === 'xhamster' && t.target_type === 'favorites'" class="w-5 h-5 fill-white/90" />
                <span v-else>{{ targetInitial(t) }}</span>
              </div>

              <div class="min-w-0 flex-1">
                <div class="font-semibold text-[15px] text-foreground truncate" :title="targetDisplayName(t)">
                  {{ targetDisplayName(t) }}
                </div>
                <div class="mt-1 flex flex-wrap items-center gap-1.5">
                  <SiteBadge :site="t.site" size="sm" />
                  <Badge variant="secondary" class="text-[11px] px-2 py-0">
                    {{ targetTypeMeta(t.target_type) }}
                  </Badge>
                  <button
                    type="button"
                    class="inline-flex items-center gap-1 rounded-full border border-border/80 bg-surface-subtle px-2 py-0 text-[11px] font-medium text-muted-foreground tabular-nums transition-colors hover:border-brand/35 hover:text-foreground cursor-pointer"
                    :title="targetVideoCountTitle(t)"
                    @click="openTargetHistory(t)"
                  >
                    <Film class="w-3 h-3 opacity-75 shrink-0" />
                    <span>{{ targetVideoCount(t) }} 视频</span>
                  </button>
                  <Badge
                    v-if="t.preferred_quality"
                    variant="outline"
                    class="text-[11px] px-2 py-0 font-mono text-muted-foreground"
                  >
                    {{ t.preferred_quality }}
                  </Badge>
                </div>
              </div>
            </div>

            <div
              v-if="t.last_error"
              class="rounded-xl border border-red-500/20 bg-red-500/8 px-3 py-2 text-[11px] text-red-600 dark:text-red-400 line-clamp-2"
              :title="t.last_error"
            >
              {{ t.last_error }}
            </div>
          </div>

          <!-- Card Footer: Metrics + Quick Actions -->
          <div class="pt-3 border-t border-border/80 flex flex-wrap items-center justify-between gap-x-2 gap-y-2">
            <div class="flex items-center gap-2 text-xs text-muted-foreground tabular-nums min-w-0">
              <span class="truncate" :title="`上次检查: ${formatDateTime(t.last_checked_at)}`">
                {{ formatDateTime(t.last_checked_at) }}
              </span>
              <span
                v-if="targetTodayNewCount(t) > 0"
                class="inline-flex items-center rounded-full bg-emerald-500/12 px-2 py-0.5 text-[11px] font-semibold text-emerald-600 dark:text-emerald-400 shrink-0"
                :title="targetNewBadgeTitle(t)"
              >
                今日 +{{ targetTodayNewCount(t) }}
              </span>
            </div>

            <div class="inline-flex items-center gap-0.5 sm:gap-1 shrink-0 ml-auto">
              <Button
                variant="ghost"
                size="icon"
                class="h-7 w-7 text-muted-foreground hover:text-brand hover:bg-brand/10"
                title="立即检查并下载此目标新视频"
                @click="triggerUpdate([t.id], true)"
              >
                <RefreshCw :class="['w-3.5 h-3.5', updatingTargetId === t.id ? 'animate-spin text-brand' : '']" />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                class="h-7 w-7 text-muted-foreground hover:text-foreground"
                title="编辑目标属性与画质偏好"
                @click="openEditDialog(t)"
              >
                <Pencil class="w-3.5 h-3.5" />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                class="h-7 w-7 text-muted-foreground hover:text-foreground"
                title="初始化基线（选择扫描范围，跳过已有视频）"
                @click="openBaselineDialog(t)"
              >
                <BookmarkCheck class="w-3.5 h-3.5" />
              </Button>
              <a
                v-if="buildTargetProfileVideosUrl(t)"
                :href="buildTargetProfileVideosUrl(t)"
                target="_blank"
                rel="noopener noreferrer"
                class="inline-flex items-center justify-center h-7 w-7 rounded-lg text-muted-foreground hover:bg-accent hover:text-foreground transition-colors"
                :title="`打开 ${targetDisplayName(t)} 视频主页`"
              >
                <ExternalLink class="w-3.5 h-3.5" />
              </a>
              <ConfirmPopover
                :title="`移除追更目标 '${targetDisplayName(t)}'？`"
                description="不会删除已有视频的去重历史记录。"
                confirm-text="移除"
                @confirm="handleDelete(t)"
              >
                <Button
                  variant="ghost"
                  size="icon"
                  class="h-7 w-7 text-muted-foreground hover:text-red-500 hover:bg-red-500/10"
                  title="移除目标"
                >
                  <Trash2 class="w-3.5 h-3.5" />
                </Button>
              </ConfirmPopover>
            </div>
          </div>
        </Card>
      </div>

      <!-- Compact Table View (Optional Toggle) -->
      <Card v-else class="overflow-hidden">
        <div class="overflow-x-auto">
          <table class="w-full min-w-[720px] text-left text-sm">
            <thead>
              <tr class="border-b border-border bg-surface-subtle text-xs font-medium text-muted-foreground whitespace-nowrap">
                <th class="h-11 px-5 w-20">状态</th>
                <th class="h-11 px-5">目标用户名</th>
                <th class="h-11 px-5 w-28">类型</th>
                <th class="h-11 px-5 w-28">视频数</th>
                <th class="h-11 px-5 w-44">上次检查</th>
                <th class="h-11 px-5 w-32">今日新增</th>
                <th class="h-11 px-5 w-36 text-right">操作</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-border">
              <tr
                v-for="t in pagedTargets"
                :key="t.id"
                class="hover:bg-accent/50 transition-colors"
              >
                <td class="px-5 py-3.5 whitespace-nowrap">
                  <Switch :model-value="Boolean(t.enabled)" @update:model-value="toggleEnabled(t, $event)" />
                </td>
                <td class="px-5 py-3.5">
                  <div class="flex items-center gap-3">
                    <img
                      v-if="t.avatar_url"
                      :src="t.avatar_url"
                      :alt="targetDisplayName(t)"
                      class="w-8 h-8 rounded-full object-cover shrink-0 bg-secondary ring-1 ring-border/60"
                      loading="lazy"
                      referrerpolicy="no-referrer"
                      @error="handleAvatarError(t)"
                    />
                    <div
                      v-else
                      :class="[
                        'w-8 h-8 rounded-full bg-gradient-to-br text-white flex items-center justify-center font-semibold text-xs shrink-0',
                        avatarGradient(targetDisplayName(t)),
                      ]"
                    >
                      <Heart v-if="t.target_type === 'liked'" class="w-3.5 h-3.5 fill-white/90" />
                      <Bookmark v-else-if="t.site === 'xhamster' && t.target_type === 'favorites'" class="w-3.5 h-3.5 fill-white/90" />
                      <span v-else>{{ targetInitial(t) }}</span>
                    </div>
                    <div class="min-w-0">
                      <div class="font-medium text-foreground truncate">{{ targetDisplayName(t) }}</div>
                      <div v-if="t.preferred_quality" class="text-[11px] text-muted-foreground mt-0.5">
                        偏好画质：{{ t.preferred_quality }}
                      </div>
                      <div v-if="t.last_error" class="text-[11px] text-red-500 mt-0.5">{{ t.last_error }}</div>
                    </div>
                  </div>
                </td>
                <td class="px-5 py-3.5 whitespace-nowrap">
                  <div class="inline-flex items-center gap-1.5">
                    <SiteBadge :site="t.site" size="sm" />
                    <Badge variant="secondary">{{ targetTypeMeta(t.target_type) }}</Badge>
                  </div>
                </td>
                <td class="px-5 py-3.5 whitespace-nowrap">
                  <button
                    type="button"
                    class="inline-flex items-center gap-1 rounded-full border border-border/80 bg-surface-subtle px-2.5 py-0.5 text-xs font-medium text-muted-foreground tabular-nums transition-colors hover:border-brand/35 hover:text-foreground cursor-pointer"
                    :title="targetVideoCountTitle(t)"
                    @click="openTargetHistory(t)"
                  >
                    <Film class="w-3 h-3 opacity-75 shrink-0" />
                    <span>{{ targetVideoCount(t) }}</span>
                  </button>
                </td>
                <td class="px-5 py-3.5 text-xs text-muted-foreground tabular-nums whitespace-nowrap">
                  {{ formatDateTime(t.last_checked_at) }}
                </td>
                <td class="px-5 py-3.5 text-xs tabular-nums whitespace-nowrap">
                  <Badge
                    v-if="targetTodayNewCount(t) > 0"
                    variant="outline"
                    dot="bg-emerald-500"
                    :title="targetNewBadgeTitle(t)"
                  >
                    今日 +{{ targetTodayNewCount(t) }}
                  </Badge>
                  <span v-else class="text-muted-foreground">0</span>
                </td>
                <td class="px-5 py-3.5 text-right whitespace-nowrap">
                  <div class="inline-flex items-center gap-1.5">
                    <Button
                      variant="ghost"
                      size="icon"
                      class="h-7 w-7 text-muted-foreground hover:text-brand hover:bg-brand/10"
                      title="立即检查并下载此目标新视频"
                      @click="triggerUpdate([t.id], true)"
                    >
                      <RefreshCw :class="['w-3.5 h-3.5', updatingTargetId === t.id ? 'animate-spin text-brand' : '']" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      class="h-7 w-7 text-muted-foreground hover:text-foreground"
                      title="编辑目标属性与画质偏好"
                      @click="openEditDialog(t)"
                    >
                      <Pencil class="w-3.5 h-3.5" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      class="h-7 w-7 text-muted-foreground hover:text-foreground"
                      title="初始化基线（选择扫描范围，跳过已有视频）"
                      @click="openBaselineDialog(t)"
                    >
                      <BookmarkCheck class="w-3.5 h-3.5" />
                    </Button>
                    <a
                      v-if="buildTargetProfileVideosUrl(t)"
                      :href="buildTargetProfileVideosUrl(t)"
                      target="_blank"
                      rel="noopener noreferrer"
                      class="inline-flex items-center justify-center h-7 w-7 rounded-lg text-muted-foreground hover:bg-accent hover:text-foreground transition-colors"
                      :title="`打开 ${targetDisplayName(t)} 视频主页`"
                    >
                      <ExternalLink class="w-3.5 h-3.5" />
                    </a>
                    <ConfirmPopover
                      :title="`移除追更目标 '${targetDisplayName(t)}'？`"
                      description="不会删除已有视频的去重历史记录。"
                      confirm-text="移除"
                      @confirm="handleDelete(t)"
                    >
                      <Button
                        variant="ghost"
                        size="icon"
                        class="h-7 w-7 text-muted-foreground hover:text-red-500 hover:bg-red-500/10"
                        title="移除目标"
                      >
                        <Trash2 class="w-3.5 h-3.5" />
                      </Button>
                    </ConfirmPopover>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </Card>

      <!-- Pagination Bar -->
      <div
        v-if="filteredTargets.length > 0 && pageSize > 0 && totalPages > 1"
        class="rounded-2xl border border-border bg-card px-3.5 sm:px-5 py-3 flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground shadow-[var(--card-shadow)]"
      >
        <span class="tabular-nums">共 {{ filteredTargets.length }} 个目标 · 第 {{ currentPage }} / {{ totalPages }} 页</span>
        <div class="flex items-center gap-1.5">
          <Button
            variant="outline"
            size="xs"
            :disabled="currentPage <= 1"
            @click="goTargetPage(currentPage - 1)"
          >
            <ChevronLeft class="w-3.5 h-3.5" />
            上一页
          </Button>
          <Button
            variant="outline"
            size="xs"
            :disabled="currentPage >= totalPages"
            @click="goTargetPage(currentPage + 1)"
          >
            下一页
            <ChevronRight class="w-3.5 h-3.5" />
          </Button>
        </div>
      </div>
    </div>

    <!-- Add Target Dialog -->
    <Dialog
      :open="addModalOpen"
      title="添加自动追更目标"
      description="添加到监控列表后，每日定时任务将增量检测其新发布视频"
      class="max-h-[calc(100dvh-2rem)] overflow-y-auto"
      @close="addModalOpen = false"
    >
      <form id="add-target-form" class="space-y-4" @submit.prevent="handleAddTarget">
        <div class="space-y-1.5">
          <label class="text-xs font-medium text-foreground">来源站点</label>
          <div>
            <SegmentedControl v-model="newTarget.site" :options="SITE_CHOICES" aria-label="选择目标站点" />
          </div>
        </div>

        <div class="space-y-1.5">
          <div class="flex items-center justify-between gap-2">
            <label class="text-xs font-medium text-foreground">用户名</label>
            <span v-if="!isNewSelfCollection && parsedNewUsernames.length > 1" class="text-[11px] font-medium text-brand tabular-nums">
              将添加 {{ parsedNewUsernames.length }} 个目标
            </span>
            <span v-else class="text-[11px] text-muted-foreground">支持多个（逗号、空格或换行分隔）</span>
          </div>
          <Textarea
            v-model="newTarget.username"
            :rows="2"
            :disabled="isNewSelfCollection"
            :placeholder="
              newTarget.target_type === 'liked'
                ? 'Liked 模式无需输入用户名'
                : isNewSelfCollection
                  ? 'xHamster 收藏模式无需输入用户名'
                  : newTarget.site === 'xhamster'
                    ? '例如: fragile-tempt, webtolove（也可直接粘贴 xHamster 主页链接）'
                    : '例如: candy-love, alice（也可换行或粘贴主页链接批量添加）'
            "
          />
        </div>

        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div class="space-y-1.5">
            <label class="text-xs font-medium text-foreground">目标类型</label>
            <Select v-model="newTarget.target_type" :options="targetTypeOptions" />
          </div>
          <div class="space-y-1.5">
            <label class="text-xs font-medium text-foreground">画质偏好（可选）</label>
            <Input v-model="newTarget.preferred_quality" placeholder="如 2160,1080" />
          </div>
        </div>

        <div class="rounded-xl border border-border bg-surface-subtle p-3.5 flex items-start gap-3">
          <Switch v-model="newTarget.init_baseline" class="mt-0.5" />
          <div class="text-xs leading-relaxed">
            <div class="font-medium text-foreground">仅建立基线（推荐）</div>
            <div class="text-muted-foreground mt-0.5">
              先记录所选范围内已有的视频但不下载，后续追更跳过这些视频。
            </div>
          </div>
        </div>
        <BaselineScopeFields
          v-if="newTarget.init_baseline"
          v-model="newTarget.baseline_pages"
          :disabled="adding"
          @validity-change="newBaselineValid = $event"
        />
      </form>

      <template #footer>
        <Button variant="outline" size="sm" @click="addModalOpen = false">取消</Button>
        <Button
          type="submit"
          form="add-target-form"
          size="sm"
          :disabled="adding || (!isNewSelfCollection && parsedNewUsernames.length === 0) || (newTarget.init_baseline && !newBaselineValid)"
        >
          <Loader2 v-if="adding" class="w-3.5 h-3.5 animate-spin" />
          {{ adding ? '保存中...' : parsedNewUsernames.length > 1 && !isNewSelfCollection ? `确认添加 (${parsedNewUsernames.length})` : '确认添加' }}
        </Button>
      </template>
    </Dialog>

    <Dialog
      :open="baselineModalOpen"
      title="初始化视频基线"
      description="登记已有视频以便后续追更时跳过，不会下载视频文件。"
      @close="baselineModalOpen = false"
    >
      <form id="sync-baseline-form" class="space-y-4" @submit.prevent="handleSyncBaseline">
        <div class="rounded-xl border border-border bg-surface-subtle px-3 py-2.5 text-sm font-medium">
          {{ baselineTarget ? targetDisplayName(baselineTarget) : '' }}
        </div>
        <BaselineScopeFields
          v-model="baselinePages"
          :disabled="baselineSubmitting"
          @validity-change="baselinePagesValid = $event"
        />
      </form>
      <template #footer>
        <Button variant="outline" size="sm" @click="baselineModalOpen = false">取消</Button>
        <Button type="submit" form="sync-baseline-form" size="sm" :disabled="baselineSubmitting || !baselinePagesValid">
          <Loader2 v-if="baselineSubmitting" class="w-3.5 h-3.5 animate-spin" />
          {{ baselineSubmitting ? '提交中...' : '开始建立基线' }}
        </Button>
      </template>
    </Dialog>

    <Dialog
      :open="editModalOpen"
      title="编辑监控目标"
      description="修改用户名、目标类型、显示名称或画质偏好；改名时自动同步历史归属"
      @close="editModalOpen = false"
    >
      <form id="edit-target-form" class="space-y-4" @submit.prevent="handleSaveTargetEdit">
        <div class="flex items-center gap-2 text-xs text-muted-foreground">
          <span>所属站点：</span>
          <SiteBadge :site="editForm.site" full />
        </div>
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div class="space-y-1.5">
            <label class="text-xs font-medium text-foreground">
              {{ isEditLockedCollection ? '目标标识（只读）' : '目标标识（用户名）' }}
            </label>
            <Input
              v-if="isEditLockedCollection"
              :model-value="editForm.target_type === 'liked' ? 'Liked Videos' : 'xHamster Favorites'"
              disabled
            />
            <Input
              v-else
              v-model="editForm.username"
              placeholder="如 fragile-tempt（支持粘贴主页链接）"
            />
          </div>
          <div class="space-y-1.5">
            <label class="text-xs font-medium text-foreground">目标类型</label>
            <Input
              v-if="isEditLockedCollection"
              :model-value="editForm.target_type === 'liked' ? '我的点赞 (Liked)' : '我的收藏 (Favorites)'"
              disabled
            />
            <Select v-else v-model="editForm.target_type" :options="editTargetTypeOptions" />
          </div>
        </div>

        <div class="space-y-1.5">
          <label class="text-xs font-medium text-foreground">自定义显示名称（可选）</label>
          <Input
            v-model="editForm.display_name"
            :disabled="isEditLockedCollection"
            placeholder="留空则显示网站名称或用户名"
          />
        </div>

        <div class="space-y-2">
          <div class="flex items-center justify-between">
            <label class="text-xs font-medium text-foreground">专属画质偏好（留空继承全局设置）</label>
            <button
              v-if="editForm.preferred_quality"
              type="button"
              class="text-[11px] text-muted-foreground hover:text-foreground cursor-pointer"
              @click="editForm.preferred_quality = ''"
            >
              恢复继承全局
            </button>
          </div>
          <div class="flex flex-wrap gap-1.5">
            <Button
              v-for="q in qualityPresetChoices"
              :key="q"
              variant="outline"
              size="xs"
              class="rounded-full font-mono text-[11px]"
              :class="isEditQualitySelected(q) ? 'border-brand/40 bg-brand/10 text-brand' : 'text-muted-foreground'"
              @click="toggleEditQuality(q)"
            >
              {{ q }}p
            </Button>
          </div>
          <Input v-model="editForm.preferred_quality" placeholder="例如 2160,1440,1080（留空则继承系统默认）" class="font-mono text-xs" />
        </div>
      </form>
      <template #footer>
        <Button variant="outline" size="sm" @click="editModalOpen = false">取消</Button>
        <Button type="submit" form="edit-target-form" size="sm" :disabled="editingSubmitting">
          <Loader2 v-if="editingSubmitting" class="w-3.5 h-3.5 animate-spin" />
          {{ editingSubmitting ? '保存中...' : '保存修改' }}
        </Button>
      </template>
    </Dialog>

    <SubsPickerModal
      :open="subsModalOpen"
      confirm-label="批量加入追更"
      :show-baseline-option="true"
      @close="subsModalOpen = false"
      @confirm="handleImportSubs"
    />
  </div>
</template>
