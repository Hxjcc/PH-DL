<script setup lang="ts">
import { getErrorMessage } from '../utils/errors'
import { computed, nextTick, onActivated, onBeforeUnmount, onDeactivated, onMounted, reactive, ref, watch } from 'vue'
import {
  Download,
  Play,
  RefreshCw,
  Users,
  Upload,
  SlidersHorizontal,
  ChevronDown,
  RotateCcw,
  Trash2,
  Clock,
  CloudUpload,
  Activity,
  Loader2,
  TriangleAlert,
  ListChecks,
  ChevronLeft,
  ChevronRight,
  Zap,
  Search,
  Database,
  Sparkles,
  Link2,
  FileText,
  Heart,
  Bookmark,
  Info,
} from 'lucide-vue-next'
import { useAppStore } from '../stores/app'
import { api } from '../api/client'
import type { CreateJobRequest, SiteKey, SubscriptionItem, TargetRecord, TargetType, TargetTypeInput } from '../types/api'
import { avatarGradient, formatDateTime, formatSpeed, jobStatusMeta } from '../utils/format'
import { computeJobLiveMetrics, resolveJobVisual } from '../utils/jobMeta'
import { parseVideoLinks } from '../utils/videoLinks'
import { extractTargetFromInput, parseTargetUsernames } from '../utils/targetInput'
import { parseSpecificVideoDraft, serializeSpecificVideoSpecs } from '../utils/specificVideos'
import Card from '../components/ui/Card.vue'
import Button from '../components/ui/Button.vue'
import Badge from '../components/ui/Badge.vue'
import Progress from '../components/ui/Progress.vue'
import Input from '../components/ui/Input.vue'
import Textarea from '../components/ui/Textarea.vue'
import Select from '../components/ui/Select.vue'
import Switch from '../components/ui/Switch.vue'
import Tabs from '../components/ui/Tabs.vue'
import TagsInput from '../components/ui/TagsInput.vue'
import ConfirmPopover from '../components/ui/ConfirmPopover.vue'
import SubsPickerModal from '../components/SubsPickerModal.vue'
import SiteBadge from '../components/SiteBadge.vue'
import SegmentedControl from '../components/ui/SegmentedControl.vue'

const store = useAppStore()

type SourceMode = NonNullable<CreateJobRequest['source_mode']>
type PageMode = 'all' | 'range'
type DownloadScope = PageMode | 'specific'
type JobAvatar = ReturnType<typeof resolveJobVisual>['avatars'][number]

interface DownloadForm {
  site: SiteKey
  usernames: string
  target_type: TargetTypeInput
  urlsText: string
  group_by_uploader: boolean
  batch_name: string
  quality: string
  pages: string
  videos: string
  keywords: string[]
  exclude_keywords: string[]
  redownload: boolean
  auto_upload: boolean
  export_only: boolean
  add_to_auto_track: boolean
  subscribe_creator: boolean
}

const sourceTab = ref<SourceMode>('target')
const showAdvanced = ref(true)
const subsModalOpen = ref(false)
const submitting = ref(false)
const triggeringAuto = ref(false)
const tempAction = ref('')
const txtInputRef = ref<HTMLInputElement | null>(null)

const sourceTabs = [
  { value: 'target', label: '按用户名' },
  { value: 'urls', label: '视频链接 / TXT' },
] as const

const SITE_OPTIONS = [
  { label: 'PornHub', value: 'pornhub', dot: 'bg-[#ff9000]' },
  { label: 'xHamster', value: 'xhamster', dot: 'bg-red-500' },
] as const

const PH_TARGET_TYPE_OPTIONS = [
  { label: '自动识别', value: 'auto' },
  { label: 'Model / 个人', value: 'model' },
  { label: 'Pornstar / 明星', value: 'pornstar' },
  { label: '收藏夹 (Favorites)', value: 'favorites' },
  { label: '我的点赞 (Liked)', value: 'liked' },
] as const

const XH_TARGET_TYPE_OPTIONS = [
  { label: '自动识别', value: 'auto' },
  { label: 'Creator / 创作者', value: 'creator' },
  { label: 'Pornstar / 明星', value: 'pornstar' },
  { label: 'User / 普通用户', value: 'users' },
  { label: 'Channel / 频道', value: 'channel' },
  { label: '我的收藏 (Favorites)', value: 'favorites' },
] as const

const targetTypeOptions = computed(() =>
  form.site === 'xhamster' ? XH_TARGET_TYPE_OPTIONS : PH_TARGET_TYPE_OPTIONS
)

const qualityChoices = ['2160', '1440', '1080', '720', '480']
const pageMode = ref<PageMode>('all')
const pageStart = ref<number | ''>(1)
const pageEnd = ref<number | ''>(1)
const videoMode = ref<'scan' | 'specific'>('scan')
const scopeOptions = [
  { label: '全部页', value: 'all' },
  { label: '页码范围', value: 'range' },
  { label: '指定视频', value: 'specific' },
] as const
const downloadScope = computed<DownloadScope>({
  get: () => (videoMode.value === 'specific' ? 'specific' : pageMode.value),
  set: (value) => {
    videoMode.value = value === 'specific' ? 'specific' : 'scan'
    if (value !== 'specific') pageMode.value = value
  },
})
const specificPage = ref<number | ''>(1)
const specificPositions = ref<string[]>([])
const urlDropping = ref(false)

const parsedLinks = computed(() => parseVideoLinks(form.urlsText))
const parsedUrls = computed(() => parsedLinks.value.urls)
const ignoredLinksText = computed(() => {
  const { invalid, duplicates } = parsedLinks.value
  return [
    invalid && `${invalid} 条无效内容`,
    duplicates && `${duplicates} 条重复链接`,
  ].filter(Boolean).join('、')
})
const cannotSubmit = computed(() =>
  submitting.value || (sourceTab.value === 'urls' && parsedUrls.value.length === 0)
)

const selectedQualities = computed(() =>
  new Set(
    form.quality
      .split(',')
      .map((value) => value.trim())
      .filter(Boolean)
  )
)

function toggleQuality(quality: string) {
  const selected = new Set(selectedQualities.value)
  if (selected.has(quality)) selected.delete(quality)
  else selected.add(quality)
  form.quality = Array.from(selected)
    .sort((a, b) => Number(b) - Number(a))
    .join(',')
}

function clearQuality() {
  form.quality = ''
}

watch([pageMode, pageStart, pageEnd], () => {
  if (pageMode.value === 'all') {
    form.pages = ''
    return
  }
  const start = Math.max(1, Number(pageStart.value) || 1)
  const end = Math.max(start, Number(pageEnd.value) || start)
  pageStart.value = start
  pageEnd.value = end
  form.pages = start === end ? String(start) : `${start}-${end}`
})

function parseSpecificDraft(raw: string, currentTags: readonly string[]) {
  const { tags, invalidFound } = parseSpecificVideoDraft(raw, currentTags, specificPage.value)
  if (invalidFound) {
    store.notify('序号格式应为 1,3-5', 'warning')
    if (tags.length === currentTags.length) return false
  }
  return tags
}

watch([videoMode, specificPage, specificPositions], () => {
  if (videoMode.value !== 'specific') {
    form.videos = ''
    if (pageMode.value === 'all') {
      form.pages = ''
    } else {
      const start = Math.max(1, Number(pageStart.value) || 1)
      const end = Math.max(start, Number(pageEnd.value) || start)
      form.pages = start === end ? String(start) : `${start}-${end}`
    }
    return
  }
  const page = Math.max(1, Number(specificPage.value) || 1)
  specificPage.value = page
  form.pages = ''
  form.videos = serializeSpecificVideoSpecs(specificPositions.value, page)
})

const form = reactive<DownloadForm>({
  site: 'pornhub',
  usernames: '',
  target_type: 'auto',
  urlsText: '',
  group_by_uploader: true,
  batch_name: 'url_batch',
  quality: '',
  pages: '',
  videos: '',
  keywords: [],
  exclude_keywords: [],
  redownload: false,
  auto_upload:
    typeof store.overview?.uploader_enabled === 'boolean'
      ? store.overview.uploader_enabled
      : true,
  export_only: false,
  add_to_auto_track: false,
  subscribe_creator: false,
})

watch(() => store.overview?.uploader_enabled, (enabled, previous) => {
  if (typeof enabled === 'boolean' && form.auto_upload === (previous ?? true)) {
    form.auto_upload = enabled
  }
})

const isSelfCollectionTarget = computed(
  () => form.target_type === 'liked' || (form.site === 'xhamster' && form.target_type === 'favorites')
)

const creatorActionsDisabled = computed(
  () => sourceTab.value === 'target' && (form.target_type === 'liked' || form.target_type === 'favorites')
)

const subscribeCreatorDisabled = computed(() => creatorActionsDisabled.value)

watch(
  () => form.site,
  (nextSite) => {
    const validValues = new Set<TargetTypeInput>(
      (nextSite === 'xhamster' ? XH_TARGET_TYPE_OPTIONS : PH_TARGET_TYPE_OPTIONS).map((o) => o.value)
    )
    if (!validValues.has(form.target_type)) {
      form.target_type = 'auto'
    }
  }
)

watch(
  () => form.usernames,
  (val) => {
    const text = String(val || '').trim()
    if (!text || /[\s,，;；]/.test(text)) return
    const detected = extractTargetFromInput(text)
    if (!detected) return
    if (detected.site !== form.site) {
      form.site = detected.site
    }
    if (detected.target_type === 'liked' || (detected.site === 'xhamster' && detected.target_type === 'favorites')) {
      form.target_type = detected.target_type
      form.usernames = ''
    } else if (detected.target_type) {
      form.target_type = detected.target_type
    }
  }
)

const isPartialDownloadScope = computed(() => {
  if (sourceTab.value === 'urls') return true
  return (
    downloadScope.value !== 'all' ||
    form.keywords.length > 0 ||
    form.exclude_keywords.length > 0
  )
})

const showPartialTrackHint = computed(
  () => !creatorActionsDisabled.value && form.add_to_auto_track && isPartialDownloadScope.value
)

const activeFilterCount = computed(() => {
  const base = [form.quality, form.keywords.length, form.exclude_keywords.length]
  if (sourceTab.value === 'target') {
    base.push(form.pages, form.videos)
  }
  return base.filter((value) => (typeof value === 'number' ? value > 0 : String(value || '').trim().length > 0)).length
})

const showTempBanner = computed(
  () => !store.overview?.running && store.tempFiles.length > 0
)
const legacyTempCount = computed(() => store.tempFiles.filter((file) => !file.verified_checkpoint).length)

const recentJobs = computed(() => store.jobs.slice(0, 6))
const isRouteActive = ref(true)
const brokenRecentAvatars = ref(new Set<string>())
const RECENT_CATEGORY_ICONS = {
  download: Download,
  link: Link2,
  file: FileText,
  refresh: RefreshCw,
  zap: Zap,
  search: Search,
  database: Database,
  rotate: RotateCcw,
  sparkles: Sparkles,
}

function isRecentAvatarValid(entry: JobAvatar | undefined) {
  return Boolean(entry?.avatarUrl && !brokenRecentAvatars.value.has(entry.avatarUrl))
}

function markRecentAvatarBroken(url: string | undefined) {
  if (!url || brokenRecentAvatars.value.has(url)) return
  const next = new Set(brokenRecentAvatars.value)
  next.add(url)
  brokenRecentAvatars.value = next
}

const recentJobCards = computed(() => {
  const trackLive = isRouteActive.value
  return recentJobs.value.map((job) => {
    const visual = resolveJobVisual(job, store.targetsCache || [])
    const isActive = ['queued', 'scanning', 'running'].includes(job.status)
    const metrics = computeJobLiveMetrics(
      job,
      trackLive && isActive ? store.liveProgress : null,
      store.jobItems[job.id],
      trackLive && isActive ? store.scanStatus : null
    )
    return {
      job,
      visual,
      metrics,
      isActive,
      icon: RECENT_CATEGORY_ICONS[visual.category.icon] || Download,
    }
  })
})
const quickTargets = computed(() =>
  (store.targetsCache || []).filter((target) => target.enabled)
)
const quickTargetsByName = computed(() => {
  const map = new Map<string, TargetRecord>()
  for (const t of quickTargets.value) {
    const tSite = t.site || 'pornhub'
    const isSelf = t.target_type === 'liked' || (tSite === 'xhamster' && t.target_type === 'favorites')
    if (!isSelf && t.username) {
      map.set(`${tSite}:${t.username.toLowerCase()}`, t)
    }
  }
  return map
})
const selectedUsernameList = computed(() => parseTargetUsernames(form.usernames))
const selectedUsernameSet = computed(() => new Set(selectedUsernameList.value.map((u) => u.toLowerCase())))

function targetName(target: TargetRecord) {
  if (target.target_type === 'liked') return 'Liked Videos'
  if (target.site === 'xhamster' && target.target_type === 'favorites') {
    return (target.display_name || '').trim() || 'xHamster Favorites'
  }
  return (target.display_name || '').trim() || target.username
}

function isQuickTargetSelected(target: TargetRecord) {
  const tSite = target.site || 'pornhub'
  if (tSite !== form.site) return false
  if (target.target_type === 'liked') return form.target_type === 'liked'
  if (tSite === 'xhamster' && target.target_type === 'favorites') return form.target_type === 'favorites'
  return !isSelfCollectionTarget.value && selectedUsernameSet.value.has(target.username.toLowerCase())
}

function syncTargetTypeForNames(names: string[]) {
  if (names.length === 0) {
    form.target_type = 'auto'
    return
  }
  const types = new Set<TargetType>()
  for (const name of names) {
    const matched = quickTargetsByName.value.get(`${form.site}:${name.toLowerCase()}`)
    if (!matched) {
      form.target_type = 'auto'
      return
    }
    types.add(matched.target_type)
  }
  form.target_type = types.size === 1 ? Array.from(types)[0] : 'auto'
}

function fillQuickTarget(target: TargetRecord) {
  sourceTab.value = 'target'
  const tSite = target.site || 'pornhub'
  const siteChanged = tSite !== form.site
  if (siteChanged) {
    form.site = tSite
    form.usernames = ''
    form.target_type = 'auto'
  }
  const isSelf = target.target_type === 'liked' || (tSite === 'xhamster' && target.target_type === 'favorites')
  if (isSelf) {
    if (!siteChanged && form.target_type === target.target_type) {
      form.target_type = 'auto'
    } else {
      form.target_type = target.target_type
      form.usernames = ''
    }
    return
  }

  if (isSelfCollectionTarget.value) {
    form.target_type = 'auto'
  }
  const key = target.username.toLowerCase()
  const current = siteChanged ? [] : selectedUsernameList.value
  const next = !siteChanged && selectedUsernameSet.value.has(key)
    ? current.filter((u) => u.toLowerCase() !== key)
    : [...current, target.username]
  form.usernames = next.join(', ')
  syncTargetTypeForNames(next)
}

function clearTargetUsernames() {
  form.usernames = ''
  if (!isSelfCollectionTarget.value) form.target_type = 'auto'
}

const quickRailRef = ref<HTMLElement | null>(null)
const quickCanScrollLeft = ref(false)
const quickCanScrollRight = ref(false)
const quickHoverDir = ref(0) // -1 left, 0 idle, 1 right

let quickGlideRaf = 0
let quickGlideLastTs = 0
let quickGlideVelocity = 0
let quickResizeObserver: ResizeObserver | null = null
const QUICK_GLIDE_MAX_SPEED = 240 // px/sec — smooth and easy to read while gliding

function updateQuickScrollState() {
  const el = quickRailRef.value
  if (!el) {
    quickCanScrollLeft.value = false
    quickCanScrollRight.value = false
    return
  }
  const maxScroll = Math.max(0, el.scrollWidth - el.clientWidth)
  quickCanScrollLeft.value = el.scrollLeft > 4
  quickCanScrollRight.value = maxScroll > 4 && el.scrollLeft < maxScroll - 4
  if ((quickHoverDir.value < 0 && !quickCanScrollLeft.value) || (quickHoverDir.value > 0 && !quickCanScrollRight.value)) {
    quickHoverDir.value = 0
  }
}

function stopQuickGlide({ immediate = false } = {}) {
  quickHoverDir.value = 0
  if (immediate) {
    quickGlideVelocity = 0
    if (quickGlideRaf) {
      cancelAnimationFrame(quickGlideRaf)
      quickGlideRaf = 0
    }
    quickGlideLastTs = 0
  }
}

function stepQuickGlide(ts: number) {
  const el = quickRailRef.value
  if (!el) {
    stopQuickGlide({ immediate: true })
    return
  }
  if (!quickGlideLastTs) quickGlideLastTs = ts
  const dt = Math.min(0.032, Math.max(0.001, (ts - quickGlideLastTs) / 1000))
  quickGlideLastTs = ts

  const targetVelocity = quickHoverDir.value * QUICK_GLIDE_MAX_SPEED
  // Smooth spring-like acceleration on hover, silky inertia coast on leave
  const smoothing = quickHoverDir.value !== 0 ? 10 : 14
  const factor = 1 - Math.exp(-smoothing * dt)
  quickGlideVelocity += (targetVelocity - quickGlideVelocity) * factor

  if (Math.abs(quickGlideVelocity) < 4 && quickHoverDir.value === 0) {
    stopQuickGlide({ immediate: true })
    updateQuickScrollState()
    return
  }

  const maxScroll = Math.max(0, el.scrollWidth - el.clientWidth)
  const nextLeft = Math.min(maxScroll, Math.max(0, el.scrollLeft + quickGlideVelocity * dt))
  el.scrollLeft = nextLeft
  updateQuickScrollState()

  const hitLeft = nextLeft <= 0 && quickGlideVelocity < 0
  const hitRight = nextLeft >= maxScroll && quickGlideVelocity > 0
  if (hitLeft || hitRight) {
    stopQuickGlide({ immediate: true })
    return
  }

  quickGlideRaf = requestAnimationFrame(stepQuickGlide)
}

function startQuickGlide(dir: -1 | 1) {
  if (typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches) {
    return
  }
  if ((dir < 0 && !quickCanScrollLeft.value) || (dir > 0 && !quickCanScrollRight.value)) return
  quickHoverDir.value = dir
  if (!quickGlideRaf) {
    quickGlideLastTs = 0
    quickGlideRaf = requestAnimationFrame(stepQuickGlide)
  }
}

function nudgeQuickRail(dir: -1 | 1) {
  const el = quickRailRef.value
  if (!el) return
  stopQuickGlide({ immediate: true })
  const step = Math.max(180, Math.round(el.clientWidth * 0.55))
  el.scrollBy({ left: dir * step, behavior: 'smooth' })
}

function handleQuickTargetsWheel(event: WheelEvent) {
  const el = event.currentTarget as HTMLElement | null
  if (!el || el.scrollWidth <= el.clientWidth) return
  if (Math.abs(event.deltaY) > Math.abs(event.deltaX)) {
    event.preventDefault()
    stopQuickGlide({ immediate: true })
    el.scrollLeft += event.deltaY
    updateQuickScrollState()
  }
}

const quickRailMaskClass = computed(() => {
  if (quickCanScrollLeft.value && quickCanScrollRight.value) return 'quick-rail-mask-both'
  if (quickCanScrollRight.value) return 'quick-rail-mask-right'
  if (quickCanScrollLeft.value) return 'quick-rail-mask-left'
  return ''
})

watch(
  () => quickTargets.value.length,
  async () => {
    await nextTick()
    updateQuickScrollState()
  }
)

onMounted(() => {
  updateQuickScrollState()
  if (typeof ResizeObserver !== 'undefined') {
    quickResizeObserver = new ResizeObserver(() => updateQuickScrollState())
    if (quickRailRef.value) quickResizeObserver.observe(quickRailRef.value)
  }
})

watch(quickRailRef, (el, prev) => {
  if (prev && quickResizeObserver) quickResizeObserver.unobserve(prev)
  if (el && quickResizeObserver) quickResizeObserver.observe(el)
  nextTick(updateQuickScrollState)
})

onDeactivated(() => {
  isRouteActive.value = false
  stopQuickGlide({ immediate: true })
})

onBeforeUnmount(() => {
  stopQuickGlide({ immediate: true })
  quickResizeObserver?.disconnect()
  quickResizeObserver = null
})

let activatedOnce = false

onActivated(async () => {
  isRouteActive.value = true
  nextTick(updateQuickScrollState)
  if (!activatedOnce) {
    activatedOnce = true
    if (store.hydratedFromCache) return
    const tasks = []
    if (!store.overviewLoaded) tasks.push(store.refreshOverview())
    if (!store.jobsLoaded || Date.now() - store.jobsLoadedAt > 3000) {
      tasks.push(store.refreshJobs())
    }
    if (store.targetsCache === null) {
      tasks.push(store.ensureTargets())
    }
    if (!store.tempFilesLoaded || Date.now() - store.tempFilesLoadedAt > 1500) {
      tasks.push(store.refreshTempFiles())
    }
    if (tasks.length > 0) {
      await Promise.all(tasks)
      nextTick(updateQuickScrollState)
    }
    return
  }
  store.refreshJobs()
  store.refreshTempFiles()
})

function importTxtFile(file: File | undefined) {
  if (!file) return
  if (!file.name.toLowerCase().endsWith('.txt')) {
    store.notify('请选择 .txt 文件', 'warning')
    return
  }
  const reader = new FileReader()
  reader.onload = () => {
    const content = String(reader.result || '').trim()
    if (content) {
      form.urlsText = form.urlsText ? `${form.urlsText}\n${content}` : content
      form.batch_name = file.name.replace(/\.txt$/i, '') || 'url_batch'
      store.notify(`已导入 ${file.name}`, 'success')
    }
  }
  reader.onerror = () => store.notify('无法读取 TXT 文件，请重试', 'error')
  reader.readAsText(file, 'utf-8')
}

function handleTxtFileChange(evt: Event) {
  const input = evt.target as HTMLInputElement
  importTxtFile(input.files?.[0])
  input.value = ''
}

function handleTxtDrop(evt: DragEvent) {
  urlDropping.value = false
  importTxtFile(evt.dataTransfer?.files?.[0])
}

function clearUrls() {
  form.urlsText = ''
}

function handleSubsPicked(picked: SubscriptionItem[], opts: { site?: SiteKey } = {}) {
  const nextSite = opts?.site || picked?.[0]?.site || form.site
  const switchedSite = nextSite !== form.site
  if (switchedSite) {
    form.site = nextSite
    form.usernames = ''
  }
  const names = picked.map((p) => p.username)
  const existing = switchedSite ? [] : parseTargetUsernames(form.usernames)
  const merged = parseTargetUsernames([...existing, ...names].join(','))
  form.usernames = merged.join(', ')
  form.target_type = 'auto'
  sourceTab.value = 'target'
}

async function submitDownload() {
  if (cannotSubmit.value) {
    if (!submitting.value) store.notify('请粘贴或导入至少一条有效视频链接', 'warning')
    return
  }
  if (sourceTab.value === 'target' && !isSelfCollectionTarget.value) {
    const parsed = parseTargetUsernames(form.usernames)
    if (form.usernames.trim() && parsed.length === 0) {
      store.notify('未识别到有效的博主用户名（视频链接请切换到「视频链接 / TXT」标签）', 'warning')
      return
    }
  }
  if (sourceTab.value === 'target' && videoMode.value === 'specific' && !form.videos) {
    store.notify('请填写要下载的页码和页内视频序号', 'warning')
    return
  }
  submitting.value = true
  try {
    const payload: CreateJobRequest = {
      source_mode: sourceTab.value,
      quality: form.quality.trim(),
      keywords: form.keywords.join(','),
      exclude_keywords: form.exclude_keywords.join(','),
      redownload: !form.export_only && form.redownload,
      auto_upload: !form.export_only && form.auto_upload,
      export_only: form.export_only,
      add_to_auto_track: !creatorActionsDisabled.value && form.add_to_auto_track,
      subscribe_creator: !subscribeCreatorDisabled.value && form.subscribe_creator,
    }
    if (sourceTab.value === 'target') {
      payload.site = form.site
      payload.pages = form.pages.trim()
      payload.videos = form.videos.trim()
      payload.usernames = isSelfCollectionTarget.value ? [] : parseTargetUsernames(form.usernames)
      payload.target_type = form.target_type
    } else {
      payload.urls = parsedUrls.value
      payload.group_by_uploader = form.group_by_uploader
      payload.batch_name = form.batch_name.trim() || 'url_batch'
    }

    const res = await api.createJob(payload)
    store.notify(`任务 #${res.job.id} 已加入队列`, 'success')
    await store.refreshJobs()
  } catch (err: unknown) {
    store.notify(getErrorMessage(err), 'error')
  } finally {
    submitting.value = false
  }
}

async function triggerQuickAutoUpdate() {
  triggeringAuto.value = true
  try {
    const [res] = await Promise.all([
      api.runUpdate({ auto_download: true }),
      new Promise((r) => setTimeout(r, 550)),
    ])
    store.notify(`已启动全量追更任务 #${res.job.id}`, 'success')
    await store.refreshJobs()
  } catch (err: unknown) {
    store.notify(getErrorMessage(err), 'error')
  } finally {
    triggeringAuto.value = false
  }
}

async function handleResumeTemp() {
  if (tempAction.value) return
  tempAction.value = 'resume'
  try {
    const res = await api.resumeTempFiles()
    store.notify(`已创建断点续传任务 #${res.job.id}`, 'success')
    await Promise.all([store.refreshJobs(), store.refreshTempFiles()])
  } catch (err: unknown) {
    store.notify(getErrorMessage(err), 'error')
  } finally {
    tempAction.value = ''
  }
}

async function handleClearTemp() {
  if (tempAction.value) return
  tempAction.value = 'clear'
  try {
    const res = await api.clearTempFiles()
    store.notify(`已清理 ${res.removed} 个临时文件`, 'info')
    await store.refreshTempFiles()
  } catch (err: unknown) {
    store.notify(getErrorMessage(err), 'error')
  } finally {
    tempAction.value = ''
  }
}
</script>

<template>
  <div class="download-page">
    <!-- Page Heading with tinted brand icon badge -->
    <div class="download-heading flex flex-wrap items-center justify-between gap-2.5 sm:gap-3">
      <div class="flex items-center gap-3 sm:gap-3.5 min-w-0">
        <div
          class="w-9 h-9 sm:w-10 sm:h-10 rounded-xl bg-brand/10 text-brand flex items-center justify-center shrink-0 ring-1 ring-brand/15"
        >
          <Download class="w-4 h-4 sm:w-5 sm:h-5" />
        </div>
        <div class="min-w-0">
          <h1 class="text-lg sm:text-xl font-bold tracking-tight text-foreground">新建下载</h1>
          <p class="text-xs sm:text-sm text-muted-foreground mt-0.5">
            按用户名或视频链接创建任务，提交后可在“任务队列”查看实时进度
          </p>
        </div>
      </div>
      <Button variant="outline" size="sm" class="w-full sm:w-auto shrink-0" :disabled="triggeringAuto" @click="triggerQuickAutoUpdate">
        <RefreshCw :class="['w-3.5 h-3.5', triggeringAuto ? 'animate-spin text-brand' : '']" />
        立即全量追更
      </Button>
    </div>

    <!-- Elevated Semantic KPI Grid (inspired by rust-srec Dashboard) -->
    <div class="overview-grid grid grid-cols-2 sm:grid-cols-3 gap-2.5 sm:gap-4">
      <Card class="overview-card col-span-2 sm:col-span-1">
        <div class="flex items-center justify-between gap-2">
          <span class="text-xs font-semibold tracking-wide text-muted-foreground">引擎状态</span>
          <span
            class="overview-icon"
            :class="[
              'w-8 h-8 rounded-full flex items-center justify-center shrink-0',
              store.overview?.running
                ? 'bg-brand/12 text-brand'
                : 'bg-emerald-500/12 text-emerald-600 dark:text-emerald-400',
            ]"
          >
            <Activity class="w-4 h-4" />
          </span>
        </div>
        <div class="overview-value flex items-center gap-2.5">
          <span
            :class="[
              'w-2.5 h-2.5 rounded-full shrink-0',
              store.overview?.running
                ? 'bg-brand status-dot-glow-brand animate-pulse'
                : 'bg-emerald-500 status-dot-glow-emerald',
            ]"
          />
          <span class="overview-number font-bold tracking-tight text-foreground">
            {{ store.overview?.running ? '运行中' : '空闲就绪' }}
          </span>
          <Badge
            v-if="store.overview?.queued_count"
            variant="secondary"
            class="tabular-nums"
          >
            排队 {{ store.overview.queued_count }}
          </Badge>
        </div>
        <p class="text-xs text-muted-foreground mt-1.5">
          {{ store.overview?.running ? '当前有任务正在后台高速执行' : '引擎健康，随时可接收新任务' }}
        </p>
      </Card>

      <Card class="overview-card">
        <div class="flex items-center justify-between gap-2">
          <span class="text-xs font-semibold tracking-wide text-muted-foreground">下次定时追更</span>
          <span class="overview-icon w-8 h-8 rounded-full bg-secondary text-foreground flex items-center justify-center shrink-0">
            <Clock class="w-4 h-4" />
          </span>
        </div>
        <div class="overview-value font-bold tracking-tight text-foreground tabular-nums truncate">
          {{
            store.overview?.schedule?.enabled
              ? formatDateTime(store.overview?.schedule?.next_run_at).slice(5)
              : '已暂停'
          }}
        </div>
        <p class="text-xs text-muted-foreground mt-1.5 tabular-nums">
          已启用 <strong class="text-foreground font-medium">{{ store.overview?.targets_enabled ?? 0 }}</strong> / {{ store.overview?.targets_total ?? 0 }} 个监控目标
        </p>
      </Card>

      <Card class="overview-card">
        <div class="flex items-center justify-between gap-2">
          <span class="text-xs font-semibold tracking-wide text-muted-foreground">已上传云端</span>
          <span class="overview-icon w-8 h-8 rounded-full bg-emerald-500/12 text-emerald-600 dark:text-emerald-400 flex items-center justify-center shrink-0">
            <CloudUpload class="w-4 h-4" />
          </span>
        </div>
        <div class="overview-value font-bold tracking-tight text-foreground tabular-nums">
          {{ store.overview?.videos?.uploaded ?? 0 }}
        </div>
        <p class="text-xs text-muted-foreground mt-1.5 tabular-nums">
          去重索引总计 <strong class="text-foreground font-medium">{{ store.overview?.videos?.total ?? 0 }}</strong> 条记录
        </p>
      </Card>
    </div>

    <!-- Temp Resume Notice (only shown when idle, never during active download!) -->
    <div
      v-if="showTempBanner"
      class="temp-notice rounded-xl border border-amber-500/30 bg-amber-500/8 px-3.5 py-2 flex flex-wrap items-center gap-2.5"
    >
      <span
        class="w-8 h-8 rounded-full bg-amber-500/12 text-amber-600 dark:text-amber-400 flex items-center justify-center shrink-0"
        aria-hidden="true"
      >
        <TriangleAlert class="w-4 h-4" />
      </span>
      <div class="min-w-0 flex-1 text-xs leading-5 text-foreground">
        检测到 <strong class="font-semibold tabular-nums text-amber-600 dark:text-amber-400">{{ store.tempFiles.length }}</strong> 个未完成的下载。
        <span v-if="legacyTempCount > 0">{{ legacyTempCount }} 个缺少续传校验记录，重新下载时会保留原断点。</span>
        <span v-else>源文件校验一致时，将从断点继续。</span>
      </div>
      <div class="flex w-full sm:w-auto shrink-0 items-center justify-end gap-2">
        <Button size="xs" :disabled="Boolean(tempAction)" @click="handleResumeTemp">
          <Loader2 v-if="tempAction === 'resume'" class="w-3.5 h-3.5 animate-spin" />
          <RotateCcw v-else class="w-3.5 h-3.5" />
          恢复未完成下载
        </Button>
        <ConfirmPopover
          :title="`确定清空全部 ${store.tempFiles.length} 个未完成临时文件？`"
          confirm-text="清空"
          @confirm="handleClearTemp"
        >
          <Button variant="outline" size="xs" :disabled="Boolean(tempAction)">
            <Trash2 class="w-3.5 h-3.5" />
            清空
          </Button>
        </ConfirmPopover>
      </div>
    </div>

    <div class="download-workspace">
      <Card class="composer-card">
        <div class="workspace-card-header">
          <div class="min-w-0">
            <h2 class="text-[15px] font-semibold tracking-tight text-foreground">新建下载任务</h2>
            <p class="text-xs text-muted-foreground mt-0.5">按用户名批量抓取，或粘贴视频链接 / 导入 TXT</p>
          </div>
          <div class="w-full sm:w-auto">
            <Tabs v-model="sourceTab" :items="sourceTabs" class="grid grid-cols-2 w-full sm:inline-flex sm:w-auto" />
          </div>
        </div>

        <form class="composer-form" @submit.prevent="submitDownload">
          <div class="composer-body">
            <!-- Both sources remain in the same grid cell so switching never changes its size. -->
            <div class="mode-panels source-panels">
              <div
                :class="['mode-panel target-panel', { 'mode-panel-inactive': sourceTab !== 'target' }]"
                :inert="sourceTab !== 'target'"
                :aria-hidden="sourceTab !== 'target'"
              >
                <div class="site-bar">
                  <span class="site-bar-label">来源站点</span>
                  <SegmentedControl v-model="form.site" :options="SITE_OPTIONS" aria-label="目标站点" />
                </div>

                <div class="target-source">
                  <div class="username-field field">
                    <div class="field-heading">
                      <label for="download-usernames">用户名</label>
                      <span>多个以逗号或空格分隔</span>
                      <button
                        v-if="!isSelfCollectionTarget && selectedUsernameList.length > 0"
                        type="button"
                        class="ml-auto text-[11px] text-muted-foreground hover:text-foreground cursor-pointer"
                        @click="clearTargetUsernames"
                      >
                        清空 ({{ selectedUsernameList.length }})
                      </button>
                    </div>
                    <Input
                      id="download-usernames"
                      v-model="form.usernames"
                      :disabled="isSelfCollectionTarget"
                      :placeholder="
                        form.target_type === 'liked'
                          ? 'Liked 模式无需输入用户名'
                          : isSelfCollectionTarget
                            ? 'xHamster 收藏模式无需输入用户名'
                            : form.site === 'xhamster'
                              ? '例如: fragile-tempt, webtolove（支持粘贴主页链接）'
                              : '例如: blowketing, candy-love（支持粘贴主页链接）'
                      "
                    />
                  </div>
                  <div class="field">
                    <div class="field-heading"><label>目标类型</label></div>
                    <Select v-model="form.target_type" :options="targetTypeOptions" aria-label="目标类型" />
                  </div>
                  <Button
                    variant="outline"
                    class="subscription-picker text-xs"
                    :disabled="isSelfCollectionTarget"
                    @click="subsModalOpen = true"
                  >
                    <Users class="w-3.5 h-3.5" />
                    从订阅勾选
                  </Button>
                </div>

                <div v-if="quickTargets.length > 0" class="quick-targets">
                  <span class="text-[11px] text-muted-foreground shrink-0">快速填入</span>
                  <div class="quick-targets-stage">
                    <div
                      ref="quickRailRef"
                      :class="['quick-targets-rail', quickRailMaskClass]"
                      @scroll.passive="updateQuickScrollState"
                      @wheel="handleQuickTargetsWheel"
                    >
                      <Button
                        v-for="target in quickTargets"
                        :key="target.id"
                        variant="outline"
                        size="xs"
                        :aria-pressed="isQuickTargetSelected(target)"
                        :title="isQuickTargetSelected(target) ? `点击取消选择 ${targetName(target)}` : `点击加入 ${targetName(target)}（可多选）`"
                        :class="[
                          'h-[26px] shrink-0 gap-1.5 rounded-full px-2 text-[11px] shadow-none hover:shadow-none',
                          isQuickTargetSelected(target)
                            ? 'border-brand/40 bg-brand/10 text-brand hover:border-brand/40 hover:bg-brand/10'
                            : 'border-border text-muted-foreground hover:text-foreground',
                        ]"
                        @click="fillQuickTarget(target)"
                      >
                        <span class="relative w-4 h-4 rounded-full overflow-hidden bg-secondary flex items-center justify-center text-[9px] shrink-0">
                          <span>{{ targetName(target).slice(0, 1).toUpperCase() }}</span>
                          <img
                            v-if="target.avatar_url && !brokenRecentAvatars.has(target.avatar_url)"
                            :src="target.avatar_url"
                            alt=""
                            aria-hidden="true"
                            class="absolute inset-0 w-full h-full object-cover"
                            decoding="sync"
                            referrerpolicy="no-referrer"
                            @error="markRecentAvatarBroken(target.avatar_url)"
                          />
                        </span>
                        <span class="max-w-24 truncate">{{ targetName(target) }}</span>
                        <SiteBadge :site="target.site" size="xs" />
                      </Button>
                    </div>

                    <button
                      type="button"
                      :class="[
                        'quick-edge-trigger quick-edge-left',
                        quickCanScrollLeft ? 'quick-edge-visible' : 'quick-edge-hidden',
                        quickHoverDir === -1 ? 'quick-edge-gliding' : '',
                      ]"
                      :tabindex="quickCanScrollLeft ? 0 : -1"
                      aria-label="向左滚动查看前面的博主"
                      title="悬停自动向左滑动，点击快速翻页"
                      @pointerenter="startQuickGlide(-1)"
                      @pointerleave="stopQuickGlide()"
                      @click="nudgeQuickRail(-1)"
                    >
                      <span class="quick-edge-pill">
                        <ChevronLeft class="w-3.5 h-3.5 quick-edge-chevron-left" />
                      </span>
                    </button>

                    <button
                      type="button"
                      :class="[
                        'quick-edge-trigger quick-edge-right',
                        quickCanScrollRight ? 'quick-edge-visible' : 'quick-edge-hidden',
                        quickHoverDir === 1 ? 'quick-edge-gliding' : '',
                      ]"
                      :tabindex="quickCanScrollRight ? 0 : -1"
                      aria-label="向右滚动查看后面的博主"
                      title="悬停自动向右滑动，点击快速翻页"
                      @pointerenter="startQuickGlide(1)"
                      @pointerleave="stopQuickGlide()"
                      @click="nudgeQuickRail(1)"
                    >
                      <span class="quick-edge-pill">
                        <ChevronRight class="w-3.5 h-3.5 quick-edge-chevron-right" />
                      </span>
                    </button>
                  </div>
                </div>
              </div>

              <div
                :class="['mode-panel url-panel', { 'mode-panel-inactive': sourceTab !== 'urls' }]"
                :inert="sourceTab !== 'urls'"
                :aria-hidden="sourceTab !== 'urls'"
              >
                <div class="field min-w-0">
                  <div class="field-heading">
                    <label for="download-urls">视频链接</label>
                    <Badge variant="secondary" class="px-2 py-0 text-[10px] tabular-nums" aria-live="polite">已识别 {{ parsedUrls.length }}</Badge>
                    <button v-if="form.urlsText" type="button" class="ml-auto text-[11px] text-muted-foreground hover:text-foreground cursor-pointer" @click="clearUrls">清空</button>
                  </div>
                  <div class="url-source">
                    <Textarea
                      id="download-urls"
                      v-model="form.urlsText"
                      :rows="5"
                      placeholder="每行粘贴一个视频页链接（支持 PornHub 与 xHamster 混合粘贴）&#10;https://www.pornhub.com/view_video.php?viewkey=...&#10;https://xhamster.com/videos/...-xhOwQIZ"
                      class="url-textarea font-mono text-xs"
                    />
                    <Button
                      variant="outline"
                      :class="['txt-dropzone', urlDropping ? 'border-brand/50 bg-brand/5' : 'border-dashed bg-surface-subtle']"
                      @click="txtInputRef?.click()"
                      @dragover.prevent="urlDropping = true"
                      @dragleave.prevent="urlDropping = false"
                      @drop.prevent="handleTxtDrop"
                    >
                      <Upload class="w-4 h-4 text-muted-foreground" />
                      <span class="text-xs font-medium">导入 TXT 文件</span>
                      <span class="text-[10px] text-muted-foreground">拖入文件或点击选择</span>
                    </Button>
                  </div>
                  <p class="source-note">
                    {{ ignoredLinksText ? `已忽略 ${ignoredLinksText}。` : '支持 PornHub（view_video/embed/shorties）与 xHamster 视频链接，重复视频自动去重。' }}
                  </p>
                  <input ref="txtInputRef" type="file" accept=".txt" class="hidden" @change="handleTxtFileChange" />
                </div>
              </div>
            </div>

            <div class="download-options">
              <label :class="{ 'download-option-disabled': form.export_only }" :title="form.export_only ? '仅导出链接时不执行下载或上传' : undefined">
                <Switch v-model="form.auto_upload" :disabled="form.export_only" />
                <span>自动 rclone 上传并删本地</span>
              </label>
              <label :class="{ 'download-option-disabled': form.export_only }" :title="form.export_only ? '仅导出链接时不执行下载或上传' : undefined">
                <Switch v-model="form.redownload" :disabled="form.export_only" />
                <span>强制重下已记录</span>
              </label>
              <label>
                <Switch v-model="form.export_only" />
                <span>仅导出视频页链接 TXT</span>
              </label>
              <label
                :class="{ 'download-option-disabled': creatorActionsDisabled }"
                :title="creatorActionsDisabled ? '仅对博主目标或视频链接生效' : '任务解析到博主后自动加入“自动追更”列表（已存在时不覆盖原有设置）'"
              >
                <Switch v-model="form.add_to_auto_track" :disabled="creatorActionsDisabled" />
                <span>同时加入自动追更</span>
              </label>
              <label
                :class="{ 'download-option-disabled': subscribeCreatorDisabled }"
                :title="subscribeCreatorDisabled ? '仅对博主目标或视频链接生效' : '使用当前站点已登录 Cookie 自动订阅该博主（已订阅自动跳过）'"
              >
                <Switch v-model="form.subscribe_creator" :disabled="subscribeCreatorDisabled" />
                <span>同时订阅博主</span>
              </label>
            </div>

            <div v-if="showPartialTrackHint" class="track-scope-hint" role="note">
              <Info class="w-3.5 h-3.5 shrink-0 mt-0.5 text-amber-600 dark:text-amber-400" />
              <span>
                当前为局部范围或单视频下载，未下载的历史视频下次追更可能会被补扫；若只想追更之后的新片，建议先在「自动追更」页建立基线。
              </span>
            </div>

            <div
              id="download-advanced-filters"
              :class="['advanced-filters', { 'advanced-filters-collapsed': !showAdvanced }]"
              :inert="!showAdvanced"
            >
              <div class="filters-clip">
                <!-- The common fields keep the same two rows and columns for both sources. -->
                <div class="filter-grid">
                  <div class="field quality-field">
                    <div class="field-heading">
                      <label>画质偏好</label>
                      <span>默认最高，可多选</span>
                    </div>
                    <div class="quality-choices">
                      <Button
                        variant="outline"
                        size="xs"
                        class="quality-choice"
                        :aria-pressed="form.quality === ''"
                        @click="clearQuality"
                      >自动最高</Button>
                      <Button
                        v-for="quality in qualityChoices"
                        :key="quality"
                        variant="outline"
                        size="xs"
                        class="quality-choice font-mono"
                        :aria-pressed="selectedQualities.has(quality)"
                        @click="toggleQuality(quality)"
                      >{{ quality }}p</Button>
                    </div>
                  </div>

                  <div class="mode-panels scope-panels">
                    <div
                      :class="['mode-panel field', { 'mode-panel-inactive': sourceTab !== 'target' }]"
                      :inert="sourceTab !== 'target'"
                      :aria-hidden="sourceTab !== 'target'"
                    >
                      <div class="field-heading">
                        <label>下载范围</label>
                        <span>{{ downloadScope === 'specific' ? '页码 / 页内序号' : downloadScope === 'range' ? '含起止页，单页填相同页码' : '扫描全部视频列表' }}</span>
                      </div>
                      <div class="scope-controls">
                        <Select v-model="downloadScope" :options="scopeOptions" class="text-xs px-2" aria-label="下载范围" />
                        <div class="mode-panels">
                          <div :class="['mode-panel scope-all', { 'mode-panel-inactive': downloadScope !== 'all' }]" :inert="downloadScope !== 'all'" :aria-hidden="downloadScope !== 'all'">
                            从第 1 页扫描至末页
                          </div>
                          <div :class="['mode-panel scope-range', { 'mode-panel-inactive': downloadScope !== 'range' }]" :inert="downloadScope !== 'range'" :aria-hidden="downloadScope !== 'range'">
                            <Input v-model="pageStart" type="number" :min="1" class="min-w-0 px-2 text-xs" aria-label="起始页码" />
                            <span class="text-[11px] text-muted-foreground">至</span>
                            <Input v-model="pageEnd" type="number" :min="1" class="min-w-0 px-2 text-xs" aria-label="结束页码" />
                          </div>
                          <div :class="['mode-panel scope-specific', { 'mode-panel-inactive': downloadScope !== 'specific' }]" :inert="downloadScope !== 'specific'" :aria-hidden="downloadScope !== 'specific'">
                            <Input v-model="specificPage" type="number" :min="1" class="min-w-0 px-2 text-xs" title="页码" aria-label="指定视频页码" />
                            <TagsInput
                              v-model="specificPositions"
                              :commit-keys="['Enter', ';', '；']"
                              :parse-draft="parseSpecificDraft"
                              placeholder="序号，如 1,3-5"
                              class="filter-tags scope-positions py-1"
                              title="输入页内序号（如 1,3-5）回车，切换左侧页码可叠加多页"
                              aria-label="页内视频序号"
                            />
                          </div>
                        </div>
                      </div>
                    </div>

                    <div
                      :class="['mode-panel field', { 'mode-panel-inactive': sourceTab !== 'urls' }]"
                      :inert="sourceTab !== 'urls'"
                      :aria-hidden="sourceTab !== 'urls'"
                    >
                      <div class="field-heading">
                        <label>保存目录</label>
                        <span>{{ form.group_by_uploader ? '自动按上传者分组' : '统一保存到批次目录' }}</span>
                      </div>
                      <div class="scope-controls">
                        <label class="uploader-group">
                          <Switch v-model="form.group_by_uploader" aria-label="按上传者自动分组" />
                          <span>按上传者</span>
                        </label>
                        <Input v-model="form.batch_name" :disabled="form.group_by_uploader" placeholder="批次目录名" class="min-w-0 text-xs" aria-label="统一批次目录名" />
                      </div>
                    </div>
                  </div>

                  <div class="field include-field">
                    <div class="field-heading">
                      <label>标题包含关键词</label>
                      <span>任一命中才保留</span>
                    </div>
                    <TagsInput v-model="form.keywords" placeholder="输入关键词后按回车" class="filter-tags py-1" />
                  </div>

                  <div class="field exclude-field">
                    <div class="field-heading">
                      <label>标题排除关键词</label>
                      <span>任一命中即跳过</span>
                    </div>
                    <TagsInput v-model="form.exclude_keywords" placeholder="输入关键词后按回车" class="filter-tags py-1" />
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div class="composer-footer">
            <Button
              variant="ghost"
              size="sm"
              class="text-muted-foreground hover:text-foreground"
              :aria-expanded="showAdvanced"
              aria-controls="download-advanced-filters"
              @click="showAdvanced = !showAdvanced"
            >
              <SlidersHorizontal class="w-3.5 h-3.5" />
              高级过滤
              <Badge v-if="activeFilterCount > 0" variant="secondary" class="px-1.5 py-0 text-[10px] tabular-nums">{{ activeFilterCount }}</Badge>
              <ChevronDown :class="['w-3.5 h-3.5 transition-transform', showAdvanced ? 'rotate-180' : '']" />
            </Button>
            <Button type="submit" size="sm" class="px-5" :disabled="cannotSubmit">
              <Loader2 v-if="submitting" class="w-3.5 h-3.5 animate-spin" />
              <Play v-else class="w-3.5 h-3.5" />
              {{ form.export_only ? '导出页面链接' : '加入下载队列' }}
            </Button>
          </div>
        </form>
      </Card>

      <aside class="recent-tasks min-w-0 flex">
        <Card class="recent-card flex flex-col w-full overflow-hidden">
          <div class="workspace-card-header flex items-center justify-between gap-2 border-b border-border">
            <div class="flex items-center gap-2.5">
              <span class="w-7 h-7 rounded-lg bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 flex items-center justify-center">
                <ListChecks class="w-3.5 h-3.5" />
              </span>
              <h2 class="text-[15px] font-semibold tracking-tight text-foreground">最近任务</h2>
            </div>
            <RouterLink to="/jobs" class="inline-flex items-center gap-0.5 text-[11px] font-medium text-muted-foreground hover:text-foreground no-underline">
              查看全部
              <ChevronRight class="w-3 h-3" />
            </RouterLink>
          </div>

          <div v-if="recentJobCards.length > 0" class="recent-list flex-1 flex flex-col">
            <div
              v-for="(entry, index) in recentJobCards"
              :key="entry.job.id"
              :class="[
                'min-w-0',
                index >= 5 ? 'recent-extra-slot' : '',
                index >= 5 && !showAdvanced ? 'recent-extra-slot-collapsed' : '',
              ]"
              :inert="index >= 5 && !showAdvanced"
              :aria-hidden="index >= 5 && !showAdvanced"
            >
              <div :class="index >= 5 ? 'recent-extra-clip' : 'min-w-0'">
                <RouterLink
                  :to="{ name: 'jobs', query: { job: entry.job.id } }"
                  class="recent-job flex flex-col justify-center min-w-0 rounded-xl border border-border bg-card px-3 py-2.5 no-underline transition-colors hover:border-brand/30 hover:bg-accent/40"
                >
              <div class="flex items-center gap-2.5 min-w-0">
                <div class="w-8 h-8 shrink-0 relative">
                  <template v-if="entry.visual.avatars.length >= 1">
                    <div
                      v-if="entry.visual.avatars[0].isLiked"
                      class="w-8 h-8 rounded-full bg-gradient-to-br from-rose-500 to-pink-600 text-white flex items-center justify-center"
                    >
                      <Heart class="w-3.5 h-3.5 fill-white/95" />
                    </div>
                    <div
                      v-else-if="entry.visual.avatars[0].isFavorites"
                      class="w-8 h-8 rounded-full bg-gradient-to-br from-red-500 to-rose-600 text-white flex items-center justify-center"
                    >
                      <Bookmark class="w-3.5 h-3.5 fill-white/95" />
                    </div>
                    <div
                      v-else
                      :class="[
                        'relative w-8 h-8 rounded-full overflow-hidden bg-gradient-to-br text-white flex items-center justify-center text-[11px] font-semibold ring-1 ring-border/70',
                        avatarGradient(entry.visual.avatars[0].name),
                      ]"
                    >
                      <span>{{ entry.visual.avatars[0].name.slice(0, 1).toUpperCase() }}</span>
                      <img
                        v-if="isRecentAvatarValid(entry.visual.avatars[0])"
                        :src="entry.visual.avatars[0].avatarUrl"
                        :alt="entry.visual.avatars[0].name"
                        class="absolute inset-0 w-full h-full object-cover"
                        decoding="sync"
                        referrerpolicy="no-referrer"
                        @error="markRecentAvatarBroken(entry.visual.avatars[0].avatarUrl)"
                      />
                    </div>
                  </template>
                  <div
                    v-else
                    :class="['w-8 h-8 rounded-lg flex items-center justify-center', entry.visual.category.iconClass]"
                  >
                    <component :is="entry.icon" class="w-3.5 h-3.5" />
                  </div>
                </div>

                <div class="min-w-0 flex-1">
                  <div class="flex items-center gap-1.5 min-w-0">
                    <span
                      :class="[
                        'inline-flex items-center rounded-full border px-1.5 py-0 text-[10px] leading-4 font-medium shrink-0',
                        entry.visual.category.pillClass,
                      ]"
                    >
                      {{ entry.visual.category.label }}
                    </span>
                    <SiteBadge v-if="entry.visual.site" :site="entry.visual.site" size="xs" />
                    <span class="truncate text-xs font-semibold text-foreground" :title="entry.visual.title">
                      {{ entry.visual.title }}
                    </span>
                    <ChevronRight class="w-3.5 h-3.5 text-muted-foreground/60 shrink-0 ml-auto" />
                  </div>

                  <div class="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] text-muted-foreground tabular-nums">
                    <Badge :variant="jobStatusMeta(entry.job.status).variant" :dot="jobStatusMeta(entry.job.status).dot" class="text-[10px] px-1.5 py-0 shrink-0">
                      {{ jobStatusMeta(entry.job.status).label }}
                    </Badge>
                    <span class="font-mono">#{{ entry.job.id }}</span>
                    <span>{{ entry.metrics.done }}/{{ entry.metrics.total }}</span>
                    <span
                      v-if="entry.isActive && entry.metrics.speedBps > 0"
                      class="ml-auto font-mono font-semibold text-emerald-600 dark:text-emerald-400"
                    >
                      {{ formatSpeed(entry.metrics.speedBps) }}
                    </span>
                    <span
                      v-else-if="entry.isActive && entry.metrics.remuxingCount > 0"
                      class="ml-auto font-medium text-violet-600 dark:text-violet-400"
                    >
                      封装 MP4
                    </span>
                    <span v-else class="ml-auto">{{ formatDateTime(entry.job.created_at) }}</span>
                  </div>
                </div>
              </div>

                  <Progress
                    v-if="entry.isActive"
                    :model-value="entry.metrics.percent"
                    :indeterminate="entry.metrics.indeterminate"
                    class="mt-2 h-1"
                  />
                </RouterLink>
              </div>
            </div>
          </div>

          <div v-else class="recent-empty m-4 flex-1 flex items-center justify-center rounded-xl border border-dashed border-border px-4 py-6 text-center text-xs text-muted-foreground">
            暂无任务，提交后会在这里显示最近进度
          </div>
        </Card>
      </aside>
    </div>

    <SubsPickerModal
      :open="subsModalOpen"
      :site="form.site"
      confirm-label="填入用户名"
      @close="subsModalOpen = false"
      @confirm="handleSubsPicked"
    />
  </div>
</template>

<style scoped>
.download-page {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.overview-card {
  position: relative;
  min-height: 140px;
  padding: 20px 24px;
}

.overview-icon {
  position: absolute;
  top: 20px;
  right: 24px;
  width: 36px;
  height: 36px;
}

.overview-icon :deep(svg) {
  width: 18px;
  height: 18px;
}

.overview-value {
  margin-top: 12px;
  font-size: 32px;
  line-height: 40px;
}

.overview-number {
  font-size: inherit;
  line-height: inherit;
}

.overview-card > p {
  margin-top: 6px;
  font-size: 12px;
  line-height: 18px;
}

.download-workspace {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  align-items: stretch;
  gap: 20px;
}

.composer-card {
  container-type: inline-size;
  display: flex;
  min-width: 0;
  flex-direction: column;
  overflow: hidden;
}

.workspace-card-header {
  display: flex;
  min-height: 64px;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 20px;
  border-bottom: 1px solid var(--border);
}

.composer-form {
  display: flex;
  flex: 1;
  flex-direction: column;
}

.composer-body {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 16px;
  padding: 16px 20px;
}

.mode-panels {
  display: grid;
  min-width: 0;
}

.mode-panel {
  grid-area: 1 / 1;
  min-width: 0;
  align-self: start;
}

.mode-panel-inactive {
  /* Group opacity hides the whole subtree immediately; inherited visibility
     would be delayed by the controls' transition-all animations. */
  opacity: 0;
  pointer-events: none;
}

.field {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 6px;
}

.field-heading {
  display: flex;
  min-height: 16px;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 8px;
  font-size: 10px;
  line-height: 16px;
  color: var(--muted-foreground);
}

.field-heading label {
  font-size: 12px;
  font-weight: 500;
  color: var(--foreground);
}

.site-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 10px;
  margin-bottom: 12px;
}

.site-bar-label {
  font-size: 12px;
  font-weight: 500;
  color: var(--foreground);
}

.target-source {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  align-items: end;
  gap: 12px;
}

.username-field {
  grid-column: 1 / -1;
}

.quick-targets {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  margin-top: 12px;
}

.quick-targets-stage {
  position: relative;
  display: flex;
  flex: 1;
  min-width: 0;
  align-items: center;
}

.quick-targets-rail {
  display: flex;
  flex: 1;
  min-width: 0;
  flex-wrap: nowrap;
  align-items: center;
  gap: 6px;
  overflow-x: auto;
  padding: 2px 0;
  scrollbar-width: none;
  -ms-overflow-style: none;
}

.quick-targets-rail::-webkit-scrollbar {
  display: none;
}

.quick-rail-mask-right {
  -webkit-mask-image: linear-gradient(to right, #000 0, #000 calc(100% - 46px), transparent 100%);
  mask-image: linear-gradient(to right, #000 0, #000 calc(100% - 46px), transparent 100%);
}

.quick-rail-mask-left {
  -webkit-mask-image: linear-gradient(to right, transparent 0, #000 46px, #000 100%);
  mask-image: linear-gradient(to right, transparent 0, #000 46px, #000 100%);
}

.quick-rail-mask-both {
  -webkit-mask-image: linear-gradient(to right, transparent 0, #000 46px, #000 calc(100% - 46px), transparent 100%);
  mask-image: linear-gradient(to right, transparent 0, #000 46px, #000 calc(100% - 46px), transparent 100%);
}

.quick-edge-trigger {
  position: absolute;
  top: 0;
  bottom: 0;
  z-index: 5;
  display: flex;
  align-items: center;
  width: 40px;
  padding: 0;
  border: 0;
  background: transparent;
  cursor: pointer;
  transition: opacity 200ms cubic-bezier(0.22, 1, 0.36, 1), transform 200ms cubic-bezier(0.22, 1, 0.36, 1);
}

.quick-edge-left {
  left: 0;
  justify-content: flex-start;
}

.quick-edge-right {
  right: 0;
  justify-content: flex-end;
}

.quick-edge-visible {
  opacity: 1;
  pointer-events: auto;
}

.quick-edge-hidden {
  opacity: 0;
  pointer-events: none;
}

.quick-edge-pill {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border-radius: 9999px;
  border: 1px solid var(--border);
  background: color-mix(in srgb, var(--card) 86%, transparent);
  backdrop-filter: blur(8px);
  color: var(--muted-foreground);
  box-shadow: 0 2px 8px -2px rgba(9, 9, 11, 0.12);
  transition:
    transform 180ms cubic-bezier(0.22, 1, 0.36, 1),
    border-color 180ms ease,
    color 180ms ease,
    background-color 180ms ease,
    box-shadow 180ms ease;
}

.quick-edge-trigger:hover .quick-edge-pill,
.quick-edge-gliding .quick-edge-pill {
  transform: scale(1.06);
  border-color: color-mix(in srgb, var(--brand) 42%, transparent);
  background: var(--card);
  color: var(--brand);
  box-shadow: 0 4px 12px -3px rgba(9, 9, 11, 0.18);
}

.quick-edge-gliding .quick-edge-chevron-right {
  animation: quick-nudge-right 750ms ease-in-out infinite;
}

.quick-edge-gliding .quick-edge-chevron-left {
  animation: quick-nudge-left 750ms ease-in-out infinite;
}

@keyframes quick-nudge-right {
  0%, 100% { transform: translateX(0); }
  50% { transform: translateX(2.5px); }
}

@keyframes quick-nudge-left {
  0%, 100% { transform: translateX(0); }
  50% { transform: translateX(-2.5px); }
}

@media (prefers-reduced-motion: reduce) {
  .quick-edge-gliding .quick-edge-chevron-right,
  .quick-edge-gliding .quick-edge-chevron-left {
    animation: none;
  }
}

.url-source {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  align-items: stretch;
  gap: 12px;
}

.url-textarea {
  min-height: 124px;
  max-height: 320px;
  line-height: 1.65;
  overflow-y: auto;
  resize: vertical;
  transition-property: border-color, box-shadow;
}

.source-note {
  font-size: 10px;
  line-height: 15px;
  color: var(--muted-foreground);
}

.txt-dropzone {
  height: auto;
  min-height: 44px;
  flex-wrap: wrap;
  gap: 4px 8px;
  padding: 10px 12px;
}

.download-options {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 10px 12px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--surface-subtle);
}

.download-options label,
.uploader-group {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  line-height: 16px;
  cursor: pointer;
}

.download-options .download-option-disabled {
  color: var(--muted-foreground);
  cursor: not-allowed;
}

.download-option-disabled > span {
  opacity: 0.65;
}

.track-scope-hint {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 12px;
  border-radius: 10px;
  border: 1px solid color-mix(in srgb, #f59e0b 28%, var(--border));
  background: color-mix(in srgb, #f59e0b 8%, var(--surface-subtle));
  font-size: 11px;
  line-height: 16px;
  color: var(--muted-foreground);
}

.advanced-filters {
  display: grid;
  grid-template-rows: 1fr;
  transition: grid-template-rows 180ms, opacity 180ms;
}

.advanced-filters-collapsed {
  grid-template-rows: 0fr;
  opacity: 0;
}

.filters-clip {
  overflow: hidden;
}

.filter-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  grid-template-areas: "quality" "scope" "include" "exclude";
  align-items: start;
  gap: 14px 20px;
  padding: 3px;
}

.quality-field { grid-area: quality; }
.scope-panels { grid-area: scope; }
.include-field { grid-area: include; }
.exclude-field { grid-area: exclude; }

.quality-choices {
  display: flex;
  min-height: 36px;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
}

.quality-choice {
  padding: 0 8px;
  border-radius: 999px;
  background: var(--card);
  color: var(--muted-foreground);
  font-size: 11px;
  box-shadow: none;
}

.quality-choice:hover {
  color: var(--foreground);
  border-color: var(--ring);
}

.quality-choice[aria-pressed='true'] {
  border-color: color-mix(in srgb, var(--brand) 40%, transparent);
  background: color-mix(in srgb, var(--brand) 10%, transparent);
  color: var(--brand);
}

.scope-controls {
  display: grid;
  grid-template-columns: 104px minmax(0, 1fr);
  align-items: center;
  gap: 8px;
}

.scope-all {
  display: flex;
  min-height: 36px;
  align-items: center;
  font-size: 11px;
  line-height: 16px;
  color: var(--muted-foreground);
}

.scope-range {
  display: flex;
  align-items: center;
  gap: 6px;
}

.scope-specific {
  display: grid;
  grid-template-columns: 52px minmax(0, 1fr);
  gap: 6px;
}

.scope-positions :deep(input) {
  min-width: 40px;
  font-size: 12px;
}

.composer-footer {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: auto;
  padding: 10px 20px;
  border-top: 1px solid var(--border);
  background: var(--surface-subtle);
}

.recent-list {
  --recent-gap: 8px;
  gap: var(--recent-gap);
  padding: 14px;
}

.recent-extra-slot {
  display: grid;
  grid-template-rows: 1fr;
  opacity: 1;
  transition: grid-template-rows 180ms, opacity 180ms, margin-top 180ms;
}

.recent-extra-slot-collapsed {
  grid-template-rows: 0fr;
  opacity: 0;
  margin-top: calc(var(--recent-gap) * -1);
  pointer-events: none;
}

.recent-extra-clip {
  overflow: hidden;
  min-height: 0;
  min-width: 0;
}

.recent-job {
  min-height: 64px;
  padding: 8px 12px;
}

@container (min-width: 600px) {
  .target-source {
    grid-template-columns: minmax(0, 2fr) repeat(2, minmax(0, 1fr));
  }

  .username-field {
    grid-column: auto;
  }

  .url-source {
    grid-template-columns: minmax(0, 2.5fr) minmax(160px, 1fr);
  }

  .txt-dropzone {
    flex-direction: column;
    align-self: stretch;
    justify-content: center;
  }

  .download-options {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .filter-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    grid-template-areas: "quality include" "scope exclude";
  }
}

@media (min-width: 1280px) {
  .download-workspace {
    grid-template-columns: minmax(0, 2.3fr) minmax(280px, 1fr);
  }
}

@media (min-width: 1280px) and (max-height: 800px) {
  .overview-card {
    min-height: 0;
    padding: 8px 12px;
  }

  .overview-icon {
    top: 12px;
    right: 12px;
    width: 28px;
    height: 28px;
  }

  .overview-icon :deep(svg) {
    width: 16px;
    height: 16px;
  }

  .overview-value {
    margin-top: 0;
    font-size: 20px;
    line-height: 22px;
  }

  .overview-card > p {
    margin-top: 0;
    font-size: 10px;
    line-height: 14px;
  }

  .temp-notice {
    padding-top: 6px;
    padding-bottom: 6px;
  }

  .temp-notice > span {
    width: 28px;
    height: 28px;
  }

  .workspace-card-header {
    min-height: 56px;
    padding: 8px 16px;
  }

  .composer-body {
    gap: 12px;
    padding: 12px 16px;
  }

  .site-bar {
    margin-bottom: 10px;
  }

  .url-textarea {
    min-height: 108px;
    padding-top: 8px;
    padding-bottom: 8px;
  }

  .quick-targets {
    margin-top: 8px;
  }

  .download-options {
    padding-top: 8px;
    padding-bottom: 8px;
  }

  .filter-grid {
    row-gap: 10px;
  }

  .composer-footer {
    padding: 8px 16px;
  }

  .recent-list {
    --recent-gap: 6px;
    padding: 10px;
  }

  .recent-job {
    min-height: 60px;
    padding: 6px 10px;
  }
}

@media (max-width: 639px) {
  .download-page {
    gap: 14px;
  }

  .overview-card {
    min-height: 0;
    padding: 12px 14px;
  }

  .overview-icon {
    top: 10px;
    right: 12px;
    width: 28px;
    height: 28px;
  }

  .overview-icon :deep(svg) {
    width: 15px;
    height: 15px;
  }

  .overview-value {
    margin-top: 6px;
    font-size: 20px;
    line-height: 26px;
  }

  .overview-card > p {
    margin-top: 4px;
    font-size: 11px;
    line-height: 15px;
  }

  .download-workspace {
    gap: 14px;
  }

  .workspace-card-header {
    min-height: 0;
    padding: 12px 14px;
    gap: 10px;
  }

  .composer-body {
    gap: 12px;
    padding: 12px 14px;
  }

  .preview-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .scope-controls {
    grid-template-columns: 96px minmax(0, 1fr);
  }

  .composer-footer {
    padding: 10px 14px;
  }

  .recent-list {
    padding: 10px;
  }
}

@media (hover: none), (pointer: coarse) {
  .quick-edge-trigger {
    display: none;
  }

  .quick-rail-mask-left,
  .quick-rail-mask-right,
  .quick-rail-mask-both {
    -webkit-mask-image: none;
    mask-image: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .advanced-filters,
  .recent-extra-slot {
    transition: none;
  }
}
</style>
