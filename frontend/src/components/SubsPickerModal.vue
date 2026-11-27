<script setup lang="ts">
import { getErrorMessage } from '../utils/errors'
import { computed, nextTick, ref, watch } from 'vue'
import { Search, Check, RefreshCw, UserRound, Camera } from 'lucide-vue-next'
import { useAppStore } from '../stores/app'
import type { SiteKey, SubscriptionItem, TargetRecord } from '../types/api'
import { targetTypeMeta } from '../utils/format'
import { prepareAvatarDataUrl } from '../utils/avatarImage'
import Dialog from './ui/Dialog.vue'
import Input from './ui/Input.vue'
import Select from './ui/Select.vue'
import Button from './ui/Button.vue'
import Badge from './ui/Badge.vue'
import Switch from './ui/Switch.vue'
import Spinner from './ui/Spinner.vue'
import SegmentedControl from './ui/SegmentedControl.vue'
import BaselineScopeFields from './BaselineScopeFields.vue'

const props = withDefaults(defineProps<{
  open?: boolean
  site?: SiteKey
  confirmLabel?: string
  showBaselineOption?: boolean
}>(), {
  open: false,
  site: 'pornhub',
  confirmLabel: '确认选择',
  showBaselineOption: false,
})
const emit = defineEmits<{
  close: []
  confirm: [items: SubscriptionItem[], options: { site: SiteKey; initBaseline: boolean; baselinePages: string }]
}>()
const store = useAppStore()
const activeSite = ref<SiteKey>('pornhub')
const loading = ref(false)
const error = ref('')
const sourceUser = ref('')
const searchInput = ref<{ focus: (options?: FocusOptions) => void } | null>(null)
const avatarInput = ref<HTMLInputElement | null>(null)
const uploadingAvatar = ref(false)
const items = ref<SubscriptionItem[]>([])
const query = ref('')
const typeFilter = ref('all')
const trackFilter = ref('all')
const selected = ref(new Set<string>())
const initBaseline = ref(true)
const baselinePages = ref('all')
const baselinePagesValid = ref(true)
const fetchedAt = ref(0)
const fromCache = ref(false)
const failedAvatarUrls = ref(new Set<string>())
let requestVersion = 0

const SITE_OPTIONS = [
  { value: 'pornhub', label: 'PornHub', dot: 'bg-[#ff9000]' },
  { value: 'xhamster', label: 'xHamster', dot: 'bg-red-500' },
] as const

const defaultSource = computed(() =>
  activeSite.value === 'xhamster'
    ? (store.xhAccountUsername || '')
    : (store.accountUsername || store.subscriptionsLastSource || '')
)
const sourceAvatar = computed(() => {
  if (canShowAvatar(store.accountAvatarUrl)) {
    return store.accountAvatarUrl
  }
  return store.targetsCache?.find(
    (target) =>
      String(target.site || 'pornhub') === activeSite.value &&
      target.username.toLowerCase() === sourceUser.value.toLowerCase() &&
      canShowAvatar(target.avatar_url)
  )?.avatar_url || ''
})
const trackedAvatars = computed(() => new Map(
  (store.targetsCache || [])
    .filter((target) => target.avatar_url && String(target.site || 'pornhub') === activeSite.value)
    .map((target) => [avatarKey(target.target_type, target.username), target.avatar_url])
))
const availableItems = computed(() =>
  activeSite.value === 'xhamster'
    ? items.value
    : items.value.filter((sub) => sub.type !== 'channel' && sub.type !== 'users' && sub.type !== 'user')
)
const typeOptions = computed(() =>
  activeSite.value === 'xhamster'
    ? [
        { label: '全部类型', value: 'all' },
        { label: 'Creator', value: 'creator' },
        { label: 'Pornstar', value: 'pornstar' },
        { label: 'Channel', value: 'channel' },
        { label: 'User', value: 'users' },
      ]
    : [
        { label: '全部类型', value: 'all' },
        { label: 'Model', value: 'model' },
        { label: 'Pornstar', value: 'pornstar' },
      ]
)
const trackedTargetsMap = computed(() => {
  const byPair = new Map<string, TargetRecord>()
  const byUser = new Map<string, TargetRecord>()
  for (const target of store.targetsCache || []) {
    if (String(target?.site || 'pornhub') !== activeSite.value) continue
    const tType = String(target?.target_type || '').toLowerCase()
    const uLow = String(target?.username || '').trim().toLowerCase()
    if (!uLow || tType === 'liked' || tType === 'favorites') continue
    byPair.set(`${tType}:${uLow}`, target)
    if (!byUser.has(uLow)) byUser.set(uLow, target)
  }
  return { byPair, byUser }
})
function trackedTargetFor(sub: SubscriptionItem) {
  const uLow = String(sub?.username || '').trim().toLowerCase()
  const tType = String(sub?.type || '').toLowerCase()
  const boundLow = String(sub?.bound_username || '').trim().toLowerCase()
  if (uLow) {
    const direct = trackedTargetsMap.value.byPair.get(`${tType}:${uLow}`) || trackedTargetsMap.value.byUser.get(uLow)
    if (direct) return direct
  }
  if (boundLow) {
    return trackedTargetsMap.value.byPair.get(`users:${boundLow}`) || trackedTargetsMap.value.byUser.get(boundLow) || null
  }
  return null
}
function isSubTracked(sub: SubscriptionItem) {
  return Boolean(trackedTargetFor(sub))
}
function boundUserLabel(sub: SubscriptionItem) {
  return String(sub?.bound_display_name || sub?.bound_username || '').trim()
}
function matchedBoundUserLabel(sub: SubscriptionItem) {
  const q = query.value.trim().toLowerCase()
  const boundLabel = boundUserLabel(sub)
  if (!q || !boundLabel) return ''
  const matchedPrimary = [sub?.display_name, sub?.username].some((name) => String(name || '').toLowerCase().includes(q))
  if (matchedPrimary) return ''
  const matchedBound = [sub?.bound_username, sub?.bound_display_name].some((name) =>
    String(name || '').toLowerCase().includes(q)
  )
  return matchedBound ? boundLabel : ''
}
const trackedSubCount = computed(() => availableItems.value.filter((sub) => isSubTracked(sub)).length)
const untrackedSubCount = computed(() => availableItems.value.length - trackedSubCount.value)
const trackFilterPills = computed(() => [
  { value: 'all', label: '全部', count: availableItems.value.length },
  { value: 'untracked', label: '未追更', count: untrackedSubCount.value },
  { value: 'tracked', label: '已追更', count: trackedSubCount.value },
])
const filtered = computed(() => {
  const q = query.value.trim().toLowerCase()
  return availableItems.value.filter((sub) => {
    if (typeFilter.value !== 'all' && sub.type !== typeFilter.value) return false
    if (trackFilter.value === 'tracked' && !isSubTracked(sub)) return false
    if (trackFilter.value === 'untracked' && isSubTracked(sub)) return false
    return (
      !q ||
      [sub.display_name, sub.username, sub.bound_username, sub.bound_display_name].some((name) =>
        String(name || '').toLowerCase().includes(q)
      )
    )
  })
})
const updatedTime = computed(() => fetchedAt.value
  ? new Date(fetchedAt.value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
  : '')

async function focusSearch() {
  await nextTick()
  if (props.open) searchInput.value?.focus({ preventScroll: true })
}

function handleOpenAutoFocus(event: Event) {
  event.preventDefault()
  focusSearch()
}

async function loadSubs(force = false) {
  const version = ++requestVersion
  const targetSite = activeSite.value
  error.value = ''
  const cached = !force && store.getCachedSubscriptions(sourceUser.value, targetSite)
  if (cached) {
    items.value = cached.subscriptions
    sourceUser.value = cached.username
    fetchedAt.value = cached.fetchedAt
    fromCache.value = true
    loading.value = false
    await focusSearch()
    return
  }
  loading.value = true
  try {
    const res = await store.loadSubscriptions(sourceUser.value, force, targetSite)
    if (version !== requestVersion || !props.open || activeSite.value !== targetSite) return
    items.value = res.subscriptions
    const availableKeys = new Set(res.subscriptions.map(subKey))
    selected.value = new Set([...selected.value].filter((key) => availableKeys.has(key)))
    sourceUser.value = res.username
    fetchedAt.value = res.fetchedAt
    fromCache.value = res.fromCache
    failedAvatarUrls.value.clear()
    await focusSearch()
  } catch (err: unknown) {
    if (version === requestVersion) error.value = getErrorMessage(err, '获取订阅列表失败')
  } finally {
    if (version === requestVersion) loading.value = false
  }
}

function switchSite(nextSite: SiteKey) {
  if (activeSite.value === nextSite) return
  activeSite.value = nextSite
  items.value = []
  selected.value = new Set()
  query.value = ''
  typeFilter.value = 'all'
  trackFilter.value = 'all'
  fetchedAt.value = 0
  sourceUser.value = defaultSource.value
  loadSubs()
}

watch(() => props.open, (isOpen) => {
  if (isOpen) {
    activeSite.value = props.site === 'xhamster' ? 'xhamster' : 'pornhub'
    failedAvatarUrls.value.clear()
    selected.value = new Set()
    items.value = []
    query.value = ''
    typeFilter.value = 'all'
    trackFilter.value = 'all'
    initBaseline.value = true
    baselinePages.value = 'all'
    baselinePagesValid.value = true
    sourceUser.value = defaultSource.value
    if (store.targetsCache === null) store.ensureTargets()
    loadSubs()
  } else {
    requestVersion++
    loading.value = false
  }
})

async function handleAvatarPicked(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file || uploadingAvatar.value) return
  uploadingAvatar.value = true
  try {
    const dataUrl = await prepareAvatarDataUrl(file)
    await store.uploadAccountAvatar(dataUrl)
    failedAvatarUrls.value.clear()
    store.notify('本地账号头像已更新', 'success')
  } catch (err: unknown) {
    store.notify(getErrorMessage(err, '头像更新失败'), 'error')
  } finally {
    uploadingAvatar.value = false
  }
}

function subKey(sub: SubscriptionItem) { return sub.type + ':' + sub.username }
function avatarKey(type: string, username: string) { return `${type}:${username.toLowerCase()}` }
function canShowAvatar(url: string | undefined): url is string { return Boolean(url && !failedAvatarUrls.value.has(url)) }
function avatarFor(sub: SubscriptionItem) {
  if (canShowAvatar(sub.avatar_url)) return sub.avatar_url
  const trackedUrl = trackedAvatars.value.get(avatarKey(sub.type, sub.username))
  return canShowAvatar(trackedUrl) ? trackedUrl : ''
}
function markAvatarFailed(event: Event) {
  const url = (event.currentTarget as HTMLImageElement).getAttribute('src')
  if (url) failedAvatarUrls.value.add(url)
}
function toggle(sub: SubscriptionItem) {
  const next = new Set(selected.value)
  const key = subKey(sub)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  selected.value = next
}
function selectAllFiltered() {
  selected.value = new Set([...selected.value, ...filtered.value.map(subKey)])
}
function clearSelection() { selected.value = new Set() }
function confirm() {
  if (props.showBaselineOption && initBaseline.value && !baselinePagesValid.value) return
  const picked = availableItems.value
    .filter((sub) => selected.value.has(subKey(sub)))
    .map((sub) => ({ ...sub, site: activeSite.value, avatar_url: avatarFor(sub) }))
  emit('confirm', picked, {
    site: activeSite.value,
    initBaseline: initBaseline.value,
    baselinePages: baselinePages.value,
  })
  emit('close')
}
</script>

<template>
  <Dialog
    :open="open"
    title="从账号订阅选择"
    description="选择要导入的订阅，使用当前已登录账号"
    class="max-w-3xl max-h-[calc(100dvh-2rem)] overflow-y-auto"
    @open-auto-focus="handleOpenAutoFocus"
    @close="emit('close')"
  >
    <div class="space-y-3.5">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <SegmentedControl
          :model-value="activeSite"
          :options="SITE_OPTIONS"
          aria-label="订阅站点"
          @change="switchSite"
        />
        <span v-if="activeSite === 'xhamster'" class="text-[11px] text-muted-foreground">
          支持导入创作者、明星、频道与已关注用户
        </span>
      </div>

      <div class="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border bg-surface-subtle px-3 py-2.5">
        <div class="flex min-w-0 items-center gap-2.5">
          <button
            type="button"
            class="group relative flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden rounded-full border border-border bg-card text-muted-foreground cursor-pointer transition-colors hover:border-brand/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/30"
            :disabled="uploadingAvatar"
            title="点击更换本地账号头像（仅本地显示，不修改站点账号信息）"
            aria-label="更换本地账号头像"
            @click="avatarInput?.click()"
          >
            <img v-if="sourceAvatar" :key="sourceAvatar" :src="sourceAvatar" alt="" class="h-full w-full object-cover" referrerpolicy="no-referrer" @error="markAvatarFailed" />
            <UserRound v-else class="w-4 h-4" />
            <span class="absolute inset-0 flex items-center justify-center bg-black/45 text-white opacity-0 transition-opacity group-hover:opacity-100">
              <Spinner v-if="uploadingAvatar" class="w-3.5 h-3.5" />
              <Camera v-else class="w-3.5 h-3.5" />
            </span>
          </button>
          <input
            ref="avatarInput"
            type="file"
            accept="image/png,image/jpeg,image/webp,image/gif"
            class="hidden"
            @change="handleAvatarPicked"
          />
          <div class="min-w-0">
            <div class="text-[10px] text-muted-foreground">{{ activeSite === 'xhamster' ? 'xHamster 订阅账号' : 'PornHub 订阅账号' }}</div>
            <div class="max-w-44 truncate text-sm font-medium" :title="sourceUser">{{ sourceUser || (activeSite === 'xhamster' ? '当前 xHamster 账号' : '当前 PornHub 账号') }}</div>
          </div>
        </div>
        <Button variant="outline" size="sm" :disabled="loading" @click="loadSubs(true)">
          <Spinner v-if="loading" class="w-4 h-4" />
          <RefreshCw v-else class="w-3.5 h-3.5" />
          {{ loading ? '获取中' : '刷新订阅' }}
        </Button>
      </div>

      <div class="flex flex-col sm:flex-row sm:flex-wrap sm:items-center gap-2">
        <div class="relative w-full sm:min-w-[180px] sm:flex-1">
          <Search class="absolute left-3 top-1/2 w-3.5 h-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            ref="searchInput"
            v-model="query"
            :placeholder="activeSite === 'xhamster' ? '搜索用户名、显示名或绑定上传号...' : '搜索用户名或显示名...'"
            aria-label="搜索订阅"
            class="pl-8"
          />
        </div>
        <div class="flex items-center gap-2 sm:contents">
          <Select v-model="typeFilter" :options="typeOptions" class="flex-1 sm:flex-none sm:w-28" aria-label="订阅类型" />
          <Button variant="outline" size="sm" @click="selectAllFiltered">全选</Button>
          <Button variant="ghost" size="sm" @click="clearSelection">清空</Button>
        </div>
      </div>

      <div class="flex flex-wrap items-center justify-between gap-2 text-[11px] text-muted-foreground">
        <div class="flex flex-wrap items-center gap-1.5" role="group" aria-label="按追更状态筛选">
          <button
            v-for="pill in trackFilterPills"
            :key="pill.value"
            type="button"
            :aria-pressed="trackFilter === pill.value"
            :class="[
              'inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] font-medium transition-colors cursor-pointer select-none tabular-nums',
              trackFilter === pill.value
                ? 'border-brand/40 bg-brand/10 dark:bg-brand/15 text-brand'
                : 'border-border bg-card text-muted-foreground hover:text-foreground',
            ]"
            @click="trackFilter = pill.value"
          >
            <span>{{ pill.label }}</span>
            <span class="font-mono text-[10px] opacity-80">{{ pill.count }}</span>
          </button>
          <span v-if="filtered.length !== availableItems.length" class="ml-1 tabular-nums">
            当前显示 {{ filtered.length }} 个
          </span>
        </div>
        <span v-if="fetchedAt" :title="new Date(fetchedAt).toLocaleString()">
          {{ fromCache ? '已缓存 · ' : '' }}更新于 {{ updatedTime }}
        </span>
      </div>
      <div v-if="error" role="alert" class="rounded-xl border border-amber-500/25 bg-amber-500/8 px-3 py-2 text-xs text-amber-700 dark:text-amber-300">
        {{ error }}<span v-if="items.length">，当前保留上次获取的订阅。</span>
      </div>

      <div :class="['min-h-[180px] overflow-y-auto pr-1', showBaselineOption && initBaseline ? 'max-h-[min(28vh,240px)]' : 'max-h-[min(42vh,340px)]']">
        <div v-if="loading && items.length === 0" class="flex min-h-[180px] flex-col items-center justify-center gap-3 text-xs text-muted-foreground" role="status">
          <Spinner class="w-7 h-7 text-foreground" />
          <span>正在获取订阅列表...</span>
        </div>
        <div v-else-if="filtered.length === 0" class="flex min-h-[180px] items-center justify-center px-4 text-center text-xs text-muted-foreground">
          {{ error ? '获取失败，可点击“刷新订阅”重试' : availableItems.length ? '没有符合筛选条件的订阅' : '该账号暂无可导入的订阅' }}
        </div>
        <div v-else class="grid grid-cols-1 sm:grid-cols-2 gap-2">
          <button
            v-for="sub in filtered"
            :key="subKey(sub)"
            type="button"
            :aria-pressed="selected.has(subKey(sub))"
            :class="[
              'flex min-w-0 items-center gap-2.5 sm:gap-3 rounded-xl border px-2.5 sm:px-3 py-2.5 sm:py-3 text-left transition-colors cursor-pointer',
              selected.has(subKey(sub)) ? 'border-brand/45 bg-brand/5 text-foreground' : 'border-border bg-card text-foreground hover:bg-accent',
            ]"
            @click="toggle(sub)"
          >
            <span class="relative shrink-0">
              <img v-if="avatarFor(sub)" :key="avatarFor(sub)" :src="avatarFor(sub)" alt="" aria-hidden="true" class="w-9 h-9 sm:w-10 sm:h-10 rounded-full object-cover bg-secondary" loading="lazy" decoding="async" referrerpolicy="no-referrer" @error="markAvatarFailed" />
              <span v-else class="flex w-9 h-9 sm:w-10 sm:h-10 items-center justify-center rounded-full bg-secondary text-sm font-semibold text-muted-foreground">{{ (sub.display_name || sub.username).slice(0, 1).toUpperCase() }}</span>
              <span v-if="selected.has(subKey(sub))" class="absolute -bottom-0.5 -right-0.5 flex w-4 h-4 items-center justify-center rounded-full bg-primary text-primary-foreground ring-2 ring-card" aria-hidden="true">
                <Check class="w-2.5 h-2.5" />
              </span>
            </span>
            <span class="min-w-0 flex-1">
              <span class="block truncate text-sm font-medium" :title="sub.display_name || sub.username">{{ sub.display_name || sub.username }}</span>
              <span
                v-if="matchedBoundUserLabel(sub)"
                class="mt-0.5 block truncate text-[11px] font-medium text-foreground"
                :title="`已自动合并绑定上传号 ${matchedBoundUserLabel(sub)}`"
              >
                匹配上传号 · {{ matchedBoundUserLabel(sub) }}
              </span>
              <span
                v-else-if="sub.display_name && sub.display_name !== sub.username"
                class="mt-0.5 block truncate text-[11px] text-muted-foreground"
                :title="boundUserLabel(sub) ? `${sub.username}（已自动合并绑定上传号 ${boundUserLabel(sub)}）` : sub.username"
              >
                {{ sub.username }}
              </span>
            </span>
            <span class="flex shrink-0 items-center gap-1 sm:gap-1.5">
              <span
                v-if="trackedTargetFor(sub)"
                :class="[
                  'inline-flex items-center gap-1 rounded-full border px-1.5 sm:px-2 py-0.5 text-[10px] font-medium whitespace-nowrap',
                  trackedTargetFor(sub)?.enabled
                    ? 'border-emerald-500/25 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
                    : 'border-border bg-secondary text-muted-foreground',
                ]"
              >
                <span
                  :class="[
                    'h-1.5 w-1.5 rounded-full',
                    trackedTargetFor(sub)?.enabled ? 'bg-emerald-500' : 'bg-zinc-400',
                  ]"
                />
                {{ trackedTargetFor(sub)?.enabled ? '已追更' : '已暂停' }}
              </span>
              <Badge
                variant="outline"
                class="h-6 shrink-0 rounded-md px-1.5 sm:px-2 text-[11px] sm:text-xs"
                :title="boundUserLabel(sub) ? `已自动合并绑定上传号 ${boundUserLabel(sub)}，下载与追更自动同步两边视频` : undefined"
              >
                {{ targetTypeMeta(sub.type) }}<span v-if="boundUserLabel(sub)" class="ml-0.5 opacity-70">+1</span>
              </Badge>
            </span>
          </button>
        </div>
      </div>

      <BaselineScopeFields
        v-if="showBaselineOption && initBaseline"
        v-model="baselinePages"
        class="border-t border-border pt-3.5"
        @validity-change="baselinePagesValid = $event"
      />
    </div>

    <template #footer>
      <div class="flex w-full flex-wrap items-center justify-between gap-3">
        <div class="flex flex-wrap items-center gap-3">
          <span class="text-xs text-muted-foreground">已选择 <strong class="font-semibold text-foreground tabular-nums">{{ selected.size }}</strong> 项</span>
          <label v-if="showBaselineOption" class="inline-flex items-center gap-1.5 text-xs text-foreground/85 cursor-pointer select-none">
            <Switch v-model="initBaseline" />
            <span>同时初始化基线</span>
          </label>
        </div>
        <div class="flex items-center justify-end gap-2 w-full sm:w-auto">
          <Button variant="outline" size="sm" @click="emit('close')">取消</Button>
          <Button size="sm" :disabled="selected.size === 0 || (showBaselineOption && initBaseline && !baselinePagesValid)" @click="confirm">{{ confirmLabel }}</Button>
        </div>
      </div>
    </template>
  </Dialog>
</template>
