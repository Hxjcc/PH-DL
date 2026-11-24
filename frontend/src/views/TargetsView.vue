<script setup>
import { computed, onActivated, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
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
  AlertCircle,
  CheckCircle2,
  PauseCircle,
  Sparkles,
  Pencil,
} from 'lucide-vue-next'
import { api } from '../api/client'
import { useAppStore } from '../stores/app'
import { avatarGradient, formatDateTime, targetTypeMeta } from '../utils/format'
import Card from '../components/ui/Card.vue'
import Button from '../components/ui/Button.vue'
import Badge from '../components/ui/Badge.vue'
import Input from '../components/ui/Input.vue'
import Select from '../components/ui/Select.vue'
import Switch from '../components/ui/Switch.vue'
import Dialog from '../components/ui/Dialog.vue'
import ConfirmPopover from '../components/ui/ConfirmPopover.vue'
import SubsPickerModal from '../components/SubsPickerModal.vue'
import BaselineScopeFields from '../components/BaselineScopeFields.vue'

const store = useAppStore()
const router = useRouter()

const VIEW_MODE_KEY = 'phdl-targets-view'
function loadInitialViewMode() {
  try {
    const saved = localStorage.getItem(VIEW_MODE_KEY)
    if (saved === 'table' || saved === 'grid') return saved
  } catch {}
  return 'grid'
}

const targets = ref(store.targetsCache ? [...store.targetsCache] : [])
const loading = ref(false)
const search = ref('')
const statusFilter = ref('all')
const viewMode = ref(loadInitialViewMode())
const subsModalOpen = ref(false)
const addModalOpen = ref(false)
const adding = ref(false)
const updatingTargetId = ref(null)
const newBaselineValid = ref(true)
const baselineModalOpen = ref(false)
const baselineTarget = ref(null)
const baselinePages = ref('1')
const baselinePagesValid = ref(true)
const baselineSubmitting = ref(false)
const editModalOpen = ref(false)
const editingSubmitting = ref(false)
const editForm = reactive({
  id: null,
  username: '',
  display_name: '',
  target_type: 'model',
  preferred_quality: '',
})
const editTargetTypeOptions = [
  { label: 'Model', value: 'model' },
  { label: 'Pornstar', value: 'pornstar' },
  { label: 'User (普通用户)', value: 'users' },
  { label: 'Favorites (收藏夹)', value: 'favorites' },
]
const qualityPresetChoices = ['2160', '1440', '1080', '720', '480']

const schedule = reactive({
  enabled:
    store.settingsCache !== null
      ? Boolean(store.settingsCache.schedule_enabled)
      : Boolean(store.overview?.schedule?.enabled ?? true),
  time:
    store.settingsCache?.schedule_time ||
    store.overview?.schedule?.time ||
    '04:00',
})

const newTarget = reactive({
  username: '',
  target_type: 'auto',
  preferred_quality: '',
  init_baseline: true,
  baseline_pages: '1',
})

const targetTypeOptions = [
  { label: '自动识别', value: 'auto' },
  { label: 'Model', value: 'model' },
  { label: 'Pornstar', value: 'pornstar' },
  { label: 'User (普通用户)', value: 'users' },
  { label: 'Favorites (收藏夹)', value: 'favorites' },
  { label: 'Liked (我的点赞)', value: 'liked' },
]

const enabledCount = computed(() => targets.value.filter((t) => t.enabled).length)
const disabledCount = computed(() => targets.value.filter((t) => !t.enabled).length)
const errorCount = computed(() => targets.value.filter((t) => Boolean(t.last_error)).length)

const statusPills = computed(() => [
  { value: 'all', label: '全部', count: targets.value.length, icon: Users },
  { value: 'enabled', label: '已启用', count: enabledCount.value, icon: CheckCircle2 },
  { value: 'disabled', label: '已暂停', count: disabledCount.value, icon: PauseCircle },
  ...(errorCount.value > 0
    ? [{ value: 'error', label: '有异常', count: errorCount.value, icon: AlertCircle }]
    : []),
])

const filteredTargets = computed(() => {
  const q = search.value.trim().toLowerCase()
  return targets.value.filter((t) => {
    if (statusFilter.value === 'enabled' && !t.enabled) return false
    if (statusFilter.value === 'disabled' && t.enabled) return false
    if (statusFilter.value === 'error' && !t.last_error) return false
    if (q) {
      const names = [targetDisplayName(t), t.username, t.display_name]
        .map((value) => String(value || '').toLowerCase())
      const typeLabel = String(targetTypeMeta(t.target_type) || '').toLowerCase()
      if (!names.some((name) => name.includes(q)) && !typeLabel.includes(q)) return false
    }
    return true
  })
})

function setViewMode(mode) {
  viewMode.value = mode
  try {
    localStorage.setItem(VIEW_MODE_KEY, mode)
  } catch {}
}

function targetDisplayName(t) {
  if (t.target_type === 'liked') return 'Liked Videos'
  return (t.display_name || '').trim() || t.username
}

function targetInitial(t) {
  const name = targetDisplayName(t).trim()
  return (name[0] || 'P').toUpperCase()
}

function handleAvatarError(t) {
  t.avatar_url = ''
}

let loadingTimer = null
let activatedOnce = false

async function loadData({ silent = false } = {}) {
  clearTimeout(loadingTimer)
  if (!silent && store.targetsCache === null && targets.value.length === 0) {
    loadingTimer = setTimeout(() => {
      loading.value = true
    }, 120)
  }
  try {
    const [items, settings] = await Promise.all([store.refreshTargets(), store.refreshSettings()])
    targets.value = [...items]
    if (settings) {
      schedule.enabled = Boolean(settings.schedule_enabled)
      schedule.time = settings.schedule_time || '04:00'
    }
  } catch (err) {
    store.notify(err.message, 'error')
  } finally {
    clearTimeout(loadingTimer)
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
  (val) => {
    if (val) {
      schedule.enabled = Boolean(val.schedule_enabled)
      schedule.time = val.schedule_time || '04:00'
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
  loadData({ silent: true })
})

onUnmounted(() => {
  clearTimeout(loadingTimer)
})

async function saveSchedule() {
  if (!schedule.time) return
  try {
    const res = await api.saveSettings({
      schedule_enabled: schedule.enabled,
      schedule_time: schedule.time,
    })
    if (res?.settings) {
      store.setSettingsCache(res.settings)
    }
    store.notify('定时追更策略已保存', 'success')
    await store.refreshOverview()
  } catch (err) {
    store.notify(err.message, 'error')
  }
}

async function handleAddTarget() {
  if (newTarget.init_baseline && !newBaselineValid.value) return
  adding.value = true
  try {
    const res = await api.createTarget({ ...newTarget })
    const addedName = res.target.display_name || res.target.username
    store.notify(
      res.baseline_job
        ? `已添加 '${addedName}' 并启动基线任务 #${res.baseline_job.id}`
        : `已添加追更目标 '${addedName}'`,
      'success'
    )
    addModalOpen.value = false
    newTarget.username = ''
    newTarget.preferred_quality = ''
    await loadData({ silent: true })
  } catch (err) {
    store.notify(err.message, 'error')
  } finally {
    adding.value = false
  }
}

async function handleImportSubs(picked, opts = {}) {
  const initBaseline = Boolean(opts?.initBaseline)
  try {
    const res = await api.batchCreateTargets({
      items: picked.map((p) => ({
        username: p.username,
        target_type: p.type,
        display_name: p.display_name || p.username,
        avatar_url: p.avatar_url || '',
      })),
      init_baseline: initBaseline,
      baseline_pages: opts?.baselinePages || '1',
    })
    const baselineCount = (res.baseline_jobs || []).length
    store.notify(
      baselineCount > 0
        ? `已批量导入 ${res.created_count} 个追更目标，并创建 ${baselineCount} 个基线任务`
        : `已批量导入 ${res.created_count} 个追更目标`,
      'success'
    )
    await Promise.all([loadData({ silent: true }), store.refreshJobs()])
  } catch (err) {
    store.notify(err.message, 'error')
  }
}

async function toggleEnabled(t, val) {
  const prev = t.enabled
  t.enabled = val
  try {
    const res = await api.updateTarget(t.id, { enabled: val })
    t.enabled = res.target.enabled
    store.setTargetsCache(targets.value)
  } catch (err) {
    t.enabled = prev
    store.notify(err.message, 'error')
  }
}

async function triggerUpdate(targetIds, autoDownload) {
  if (Array.isArray(targetIds) && targetIds.length === 1) {
    updatingTargetId.value = targetIds[0]
  }
  try {
    const res = await api.runUpdate({ target_ids: targetIds, auto_download: autoDownload })
    store.notify(`已创建任务 #${res.job.id}`, 'success')
    await store.refreshJobs()
    router.push({ name: 'jobs' })
  } catch (err) {
    store.notify(err.message, 'error')
  } finally {
    updatingTargetId.value = null
  }
}

function openBaselineDialog(target) {
  baselineTarget.value = target
  baselinePages.value = '1'
  baselinePagesValid.value = true
  baselineModalOpen.value = true
}

function openEditDialog(target) {
  editForm.id = target.id
  editForm.username = target.username
  editForm.display_name = target.display_name || ''
  editForm.target_type = target.target_type
  editForm.preferred_quality = target.preferred_quality || ''
  editModalOpen.value = true
}

function toggleEditQuality(q) {
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

function isEditQualitySelected(q) {
  return String(editForm.preferred_quality || '')
    .split(',')
    .map((s) => s.trim().replace(/p$/i, ''))
    .includes(q)
}

async function handleSaveTargetEdit() {
  if (!editForm.id || editingSubmitting.value) return
  editingSubmitting.value = true
  try {
    const payload = {
      display_name: editForm.display_name.trim(),
      preferred_quality: editForm.preferred_quality.trim(),
    }
    if (editForm.target_type !== 'liked') {
      payload.target_type = editForm.target_type
    }
    const res = await api.updateTarget(editForm.id, payload)
    const idx = targets.value.findIndex((x) => x.id === editForm.id)
    if (idx >= 0 && res?.target) {
      targets.value[idx] = { ...targets.value[idx], ...res.target }
      store.setTargetsCache(targets.value)
    } else {
      await loadData({ silent: true })
    }
    editModalOpen.value = false
    store.notify('监控目标配置已更新', 'success')
  } catch (err) {
    store.notify(err.message, 'error')
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
  } catch (err) {
    store.notify(err.message, 'error')
  } finally {
    baselineSubmitting.value = false
  }
}

async function handleDelete(t) {
  try {
    await api.deleteTarget(t.id)
    targets.value = targets.value.filter((x) => x.id !== t.id)
    store.setTargetsCache(targets.value)
    store.notify(`已移除 '${targetDisplayName(t)}'`, 'info')
  } catch (err) {
    store.notify(err.message, 'error')
  }
}
</script>

<template>
  <div class="space-y-6">
    <!-- Page Header with tinted section icon -->
    <div class="flex flex-wrap items-center justify-between gap-4">
      <div class="flex items-center gap-3.5">
        <div
          class="w-10 h-10 rounded-xl bg-brand/10 text-brand flex items-center justify-center shrink-0 ring-1 ring-brand/15"
        >
          <RefreshCw class="w-5 h-5" />
        </div>
        <div>
          <h1 class="text-xl font-bold tracking-tight text-foreground">自动追更</h1>
          <p class="text-xs sm:text-sm text-muted-foreground mt-0.5">
            每日定时检查已启用目标的新发布视频，自动下载并上传云端
          </p>
        </div>
      </div>

      <div class="flex flex-wrap items-center gap-2">
        <Button variant="outline" size="sm" @click="triggerUpdate(null, false)">
          <Eye class="w-3.5 h-3.5" />
          仅检查更新
        </Button>
        <Button size="sm" @click="triggerUpdate(null, true)">
          <Play class="w-3.5 h-3.5" />
          立即全量追更
        </Button>
      </div>
    </div>

    <!-- Compact Schedule Strip -->
    <Card class="px-5 py-4 flex flex-wrap items-center justify-between gap-4">
      <div class="flex flex-wrap items-center gap-3.5">
        <div class="inline-flex items-center gap-2 text-sm font-medium text-foreground">
          <span class="w-7 h-7 rounded-lg bg-brand/10 text-brand flex items-center justify-center">
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
      </div>

      <div class="flex items-center gap-2">
        <Button variant="outline" size="sm" @click="subsModalOpen = true">
          <Users class="w-3.5 h-3.5" />
          从订阅导入
        </Button>
        <Button variant="outline" size="sm" @click="addModalOpen = true">
          <Plus class="w-3.5 h-3.5" />
          添加目标
        </Button>
      </div>
    </Card>

    <!-- Quick Filter Pills + Search + View Switcher (inspired by rust-srec Streamers toolbar) -->
    <div class="space-y-4">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <!-- Pill Filter Strip -->
        <div class="flex flex-wrap items-center gap-1.5" role="group" aria-label="按目标状态筛选">
          <Button
            v-for="pill in statusPills"
            :key="pill.value"
            variant="outline"
            size="sm"
            :aria-pressed="statusFilter === pill.value"
            :class="[
              'gap-1.5 rounded-full shadow-none hover:shadow-none',
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
                  : 'bg-secondary text-muted-foreground',
              ]"
            >
              {{ pill.count }}
            </span>
          </Button>
        </div>

        <!-- Search + View Mode Switcher -->
        <div class="flex items-center gap-2 w-full sm:w-auto">
          <div class="relative flex-1 sm:w-64">
            <Search class="w-3.5 h-3.5 text-muted-foreground absolute left-3 top-1/2 -translate-y-1/2" />
            <Input v-model="search" placeholder="搜索监控目标..." class="pl-8 rounded-full" />
          </div>

          <div class="inline-flex items-center rounded-lg border border-border bg-card p-0.5 shrink-0" role="group" aria-label="目标视图">
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

      <!-- Loading / Empty State -->
      <Card v-if="loading && targets.length === 0" class="py-16 text-center text-xs text-muted-foreground">
        <Loader2 class="w-4 h-4 animate-spin inline mr-1.5 text-brand" />
        加载中...
      </Card>

      <Card v-else-if="filteredTargets.length === 0" class="py-16 px-6 flex flex-col items-center text-center">
        <div class="w-11 h-11 rounded-2xl bg-brand/10 text-brand flex items-center justify-center mb-3">
          <Sparkles class="w-5 h-5" />
        </div>
        <div class="text-sm font-medium text-foreground">
          {{ targets.length === 0 ? '暂无追更目标' : '没有匹配的目标' }}
        </div>
        <p class="text-xs text-muted-foreground mt-1 max-w-sm">
          {{
            targets.length === 0
              ? '点击上方“添加目标”或“从订阅导入”即可开启每日自动追更'
              : '尝试调整筛选条件或清空搜索关键词'
          }}
        </p>
      </Card>

      <!-- Card Grid View (Default, inspired by rust-srec) -->
      <div
        v-else-if="viewMode === 'grid'"
        class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4"
      >
        <Card
          v-for="t in filteredTargets"
          :key="t.id"
          class="group p-5 flex flex-col justify-between gap-4 transition-all duration-200 hover:border-brand/35"
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
                <span v-else>{{ targetInitial(t) }}</span>
              </div>

              <div class="min-w-0 flex-1">
                <div class="font-semibold text-[15px] text-foreground truncate" :title="targetDisplayName(t)">
                  {{ targetDisplayName(t) }}
                </div>
                <div class="mt-1 flex flex-wrap items-center gap-1.5">
                  <Badge variant="secondary" class="text-[11px] px-2 py-0">
                    {{ targetTypeMeta(t.target_type) }}
                  </Badge>
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
          <div class="pt-3 border-t border-border/80 flex items-center justify-between gap-2">
            <div class="flex items-center gap-2.5 text-xs text-muted-foreground tabular-nums min-w-0">
              <span class="truncate" :title="`上次检查: ${formatDateTime(t.last_checked_at)}`">
                {{ formatDateTime(t.last_checked_at) }}
              </span>
              <span
                v-if="t.last_new_count > 0"
                class="inline-flex items-center rounded-full bg-emerald-500/12 px-2 py-0.5 text-[11px] font-semibold text-emerald-600 dark:text-emerald-400 shrink-0"
              >
                +{{ t.last_new_count }} 新
              </span>
            </div>

            <div class="inline-flex items-center gap-1 shrink-0">
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
                <th class="h-11 px-5 w-32">类型</th>
                <th class="h-11 px-5 w-48">上次检查</th>
                <th class="h-11 px-5 w-28">上次新增</th>
                <th class="h-11 px-5 w-36 text-right">操作</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-border">
              <tr
                v-for="t in filteredTargets"
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
                  <Badge variant="secondary">{{ targetTypeMeta(t.target_type) }}</Badge>
                </td>
                <td class="px-5 py-3.5 text-xs text-muted-foreground tabular-nums whitespace-nowrap">
                  {{ formatDateTime(t.last_checked_at) }}
                </td>
                <td class="px-5 py-3.5 text-xs tabular-nums whitespace-nowrap">
                  <Badge v-if="t.last_new_count > 0" variant="outline" dot="bg-emerald-500">
                    +{{ t.last_new_count }}
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
          <label class="text-xs font-medium text-foreground">用户名</label>
          <Input
            v-model="newTarget.username"
            :disabled="newTarget.target_type === 'liked'"
            placeholder="例如: candy-love"
          />
        </div>

        <div class="grid grid-cols-2 gap-3">
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
        <Button type="submit" form="add-target-form" size="sm" :disabled="adding || (newTarget.init_baseline && !newBaselineValid)">
          <Loader2 v-if="adding" class="w-3.5 h-3.5 animate-spin" />
          {{ adding ? '保存中...' : '确认添加' }}
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
      description="自定义目标显示名称、专属画质偏好或修正目标类型"
      @close="editModalOpen = false"
    >
      <form id="edit-target-form" class="space-y-4" @submit.prevent="handleSaveTargetEdit">
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div class="space-y-1.5">
            <label class="text-xs font-medium text-foreground">目标标识（只读）</label>
            <Input :model-value="editForm.target_type === 'liked' ? 'Liked Videos' : editForm.username" disabled />
          </div>
          <div class="space-y-1.5">
            <label class="text-xs font-medium text-foreground">目标类型</label>
            <Input v-if="editForm.target_type === 'liked'" model-value="我的点赞 (Liked)" disabled />
            <Select v-else v-model="editForm.target_type" :options="editTargetTypeOptions" />
          </div>
        </div>

        <div class="space-y-1.5">
          <label class="text-xs font-medium text-foreground">自定义显示名称（可选）</label>
          <Input
            v-model="editForm.display_name"
            :disabled="editForm.target_type === 'liked'"
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
