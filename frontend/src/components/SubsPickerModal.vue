<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { Search, Check, RefreshCw, UserRound, Camera } from 'lucide-vue-next'
import { useAppStore } from '../stores/app'
import { targetTypeMeta } from '../utils/format'
import { prepareAvatarDataUrl } from '../utils/avatarImage'
import Dialog from './ui/Dialog.vue'
import Input from './ui/Input.vue'
import Select from './ui/Select.vue'
import Button from './ui/Button.vue'
import Badge from './ui/Badge.vue'
import Switch from './ui/Switch.vue'
import Spinner from './ui/Spinner.vue'
import BaselineScopeFields from './BaselineScopeFields.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  confirmLabel: { type: String, default: '确认选择' },
  showBaselineOption: { type: Boolean, default: false },
  showAutoTrackOption: { type: Boolean, default: false },
})
const emit = defineEmits(['close', 'confirm'])
const store = useAppStore()
const loading = ref(false)
const error = ref('')
const sourceUser = ref('')
const sourceDraft = ref('')
const manualSource = ref(false)
const editingSource = ref(false)
const sourceInput = ref(null)
const searchInput = ref(null)
const avatarInput = ref(null)
const uploadingAvatar = ref(false)
const items = ref([])
const query = ref('')
const typeFilter = ref('all')
const selected = ref(new Set())
const initBaseline = ref(true)
const addToAutoTrack = ref(false)
const baselinePages = ref('1')
const baselinePagesValid = ref(true)
const fetchedAt = ref(0)
const fromCache = ref(false)
const failedAvatarUrls = ref(new Set())
let requestVersion = 0

const defaultSource = computed(() => store.accountUsername || store.subscriptionsLastSource || '')
const isOwnAccount = computed(() => {
  const cur = sourceUser.value.trim().toLowerCase()
  const own = defaultSource.value.trim().toLowerCase()
  return !cur || (own && cur === own) || !manualSource.value
})
const sourceAvatar = computed(() => {
  if (isOwnAccount.value && canShowAvatar(store.accountAvatarUrl)) {
    return store.accountAvatarUrl
  }
  return store.targetsCache?.find(
    (target) => target.username.toLowerCase() === sourceUser.value.toLowerCase() && canShowAvatar(target.avatar_url)
  )?.avatar_url || ''
})
const trackedAvatars = computed(() => new Map(
  (store.targetsCache || [])
    .filter((target) => target.avatar_url)
    .map((target) => [avatarKey(target.target_type, target.username), target.avatar_url])
))
const availableItems = computed(() =>
  items.value.filter((sub) => sub.type !== 'channel' && sub.type !== 'users' && sub.type !== 'user')
)
const typeOptions = [
  { label: '全部类型', value: 'all' },
  { label: 'Model', value: 'model' },
  { label: 'Pornstar', value: 'pornstar' },
]
const filtered = computed(() => {
  const q = query.value.trim().toLowerCase()
  return availableItems.value.filter((sub) => {
    if (typeFilter.value !== 'all' && sub.type !== typeFilter.value) return false
    return !q || [sub.display_name, sub.username].some((name) => String(name || '').toLowerCase().includes(q))
  })
})
const updatedTime = computed(() => fetchedAt.value
  ? new Date(fetchedAt.value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
  : '')

async function focusSearch() {
  await nextTick()
  if (props.open && !editingSource.value) searchInput.value?.focus({ preventScroll: true })
}

function handleOpenAutoFocus(event) {
  event.preventDefault()
  focusSearch()
}

async function loadSubs(force = false) {
  const version = ++requestVersion
  error.value = ''
  const cached = !force && store.getCachedSubscriptions(sourceUser.value)
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
    const res = await store.loadSubscriptions(sourceUser.value, force)
    if (version !== requestVersion || !props.open) return
    items.value = res.subscriptions
    const availableKeys = new Set(res.subscriptions.map(subKey))
    selected.value = new Set([...selected.value].filter((key) => availableKeys.has(key)))
    sourceUser.value = res.username
    fetchedAt.value = res.fetchedAt
    fromCache.value = res.fromCache
    failedAvatarUrls.value.clear()
    await focusSearch()
  } catch (err) {
    if (version === requestVersion) error.value = err.message || '获取订阅列表失败'
  } finally {
    if (version === requestVersion) loading.value = false
  }
}

async function editSource() {
  sourceDraft.value = sourceUser.value || defaultSource.value
  editingSource.value = true
  await nextTick()
  sourceInput.value?.focus({ preventScroll: true })
}

async function applySource() {
  const nextSource = sourceDraft.value.trim()
  if (nextSource.toLowerCase() !== sourceUser.value.toLowerCase()) {
    items.value = []
    fetchedAt.value = 0
    selected.value = new Set()
  }
  sourceUser.value = nextSource
  manualSource.value = Boolean(nextSource)
  editingSource.value = false
  query.value = ''
  await loadSubs()
}

watch(() => props.open, (isOpen) => {
  if (isOpen) {
    failedAvatarUrls.value.clear()
    selected.value = new Set()
    query.value = ''
    typeFilter.value = 'all'
    addToAutoTrack.value = false
    editingSource.value = false
    if (!manualSource.value) sourceUser.value = defaultSource.value
    loadSubs()
  } else {
    requestVersion++
    loading.value = false
  }
})

async function handleAvatarPicked(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file || uploadingAvatar.value) return
  uploadingAvatar.value = true
  try {
    const dataUrl = await prepareAvatarDataUrl(file)
    await store.uploadAccountAvatar(dataUrl)
    failedAvatarUrls.value.clear()
    store.notify('本地账号头像已更新', 'success')
  } catch (err) {
    store.notify(err.message || '头像更新失败', 'error')
  } finally {
    uploadingAvatar.value = false
  }
}

function subKey(sub) { return sub.type + ':' + sub.username }
function avatarKey(type, username) { return `${type}:${username.toLowerCase()}` }
function canShowAvatar(url) { return url && !failedAvatarUrls.value.has(url) }
function avatarFor(sub) {
  if (canShowAvatar(sub.avatar_url)) return sub.avatar_url
  const trackedUrl = trackedAvatars.value.get(avatarKey(sub.type, sub.username))
  return canShowAvatar(trackedUrl) ? trackedUrl : ''
}
function markAvatarFailed(event) {
  const url = event.currentTarget.getAttribute('src')
  if (url) failedAvatarUrls.value.add(url)
}
function toggle(sub) {
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
    .map((sub) => ({ ...sub, avatar_url: avatarFor(sub) }))
  emit('confirm', picked, {
    initBaseline: initBaseline.value,
    baselinePages: baselinePages.value,
    addToAutoTrack: props.showAutoTrackOption && addToAutoTrack.value,
  })
  emit('close')
}
</script>

<template>
  <Dialog
    :open="open"
    title="从账号订阅选择"
    description="选择要导入的订阅，默认使用已登录账号"
    class="max-w-3xl max-h-[calc(100dvh-2rem)] overflow-y-auto"
    @open-auto-focus="handleOpenAutoFocus"
    @close="emit('close')"
  >
    <div class="space-y-3.5">
      <div class="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border bg-surface-subtle px-3 py-2.5">
        <div v-if="editingSource" class="flex w-full items-center gap-2">
          <Input ref="sourceInput" v-model="sourceDraft" class="min-w-0 flex-1" placeholder="源账号，留空使用已登录账号" aria-label="订阅源账号" @keydown.enter.prevent="applySource" />
          <Button size="sm" @click="applySource">获取</Button>
          <Button size="sm" variant="ghost" @click="editingSource = false; focusSearch()">取消</Button>
        </div>
        <template v-else>
          <div class="flex min-w-0 items-center gap-2.5">
            <button
              v-if="isOwnAccount"
              type="button"
              class="group relative flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden rounded-full border border-border bg-card text-muted-foreground cursor-pointer transition-colors hover:border-brand/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/30"
              :disabled="uploadingAvatar"
              title="点击更换本地账号头像（仅本地显示，不修改 PornHub 账号）"
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
            <span
              v-else
              class="flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden rounded-full border border-border bg-card text-muted-foreground"
            >
              <img v-if="sourceAvatar" :key="sourceAvatar" :src="sourceAvatar" alt="" class="h-full w-full object-cover" referrerpolicy="no-referrer" @error="markAvatarFailed" />
              <UserRound v-else class="w-4 h-4" />
            </span>
            <input
              ref="avatarInput"
              type="file"
              accept="image/png,image/jpeg,image/webp,image/gif"
              class="hidden"
              @change="handleAvatarPicked"
            />
            <div class="min-w-0">
              <div class="text-[10px] text-muted-foreground">订阅账号</div>
              <div class="max-w-44 truncate text-sm font-medium" :title="sourceUser">{{ sourceUser || '当前登录账号' }}</div>
            </div>
            <Button variant="ghost" size="xs" :disabled="loading" class="text-muted-foreground" @click="editSource">更换</Button>
          </div>
          <Button variant="outline" size="sm" :disabled="loading" @click="loadSubs(true)">
            <Spinner v-if="loading" class="w-4 h-4" />
            <RefreshCw v-else class="w-3.5 h-3.5" />
            {{ loading ? '获取中' : '刷新订阅' }}
          </Button>
        </template>
      </div>

      <div class="flex flex-wrap items-center gap-2">
        <div class="relative min-w-[180px] flex-1">
          <Search class="absolute left-3 top-1/2 w-3.5 h-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input ref="searchInput" v-model="query" placeholder="搜索用户名或显示名..." aria-label="搜索订阅" class="pl-8" />
        </div>
        <Select v-model="typeFilter" :options="typeOptions" class="w-28" aria-label="订阅类型" />
        <Button variant="outline" size="sm" @click="selectAllFiltered">全选</Button>
        <Button variant="ghost" size="sm" @click="clearSelection">清空</Button>
      </div>

      <div class="flex flex-wrap items-center justify-between gap-2 text-[11px] text-muted-foreground">
        <span>{{ filtered.length }} / {{ availableItems.length }} 个订阅</span>
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
              'flex min-w-0 items-center gap-3 rounded-xl border px-3 py-3 text-left transition-colors cursor-pointer',
              selected.has(subKey(sub)) ? 'border-brand/45 bg-brand/5 text-foreground' : 'border-border bg-card text-foreground hover:bg-accent',
            ]"
            @click="toggle(sub)"
          >
            <span class="relative shrink-0">
              <img v-if="avatarFor(sub)" :key="avatarFor(sub)" :src="avatarFor(sub)" alt="" aria-hidden="true" class="w-10 h-10 rounded-full object-cover bg-secondary" loading="lazy" decoding="async" referrerpolicy="no-referrer" @error="markAvatarFailed" />
              <span v-else class="flex w-10 h-10 items-center justify-center rounded-full bg-secondary text-sm font-semibold text-muted-foreground">{{ (sub.display_name || sub.username).slice(0, 1).toUpperCase() }}</span>
              <span v-if="selected.has(subKey(sub))" class="absolute -bottom-0.5 -right-0.5 flex w-4 h-4 items-center justify-center rounded-full bg-primary text-primary-foreground ring-2 ring-card" aria-hidden="true">
                <Check class="w-2.5 h-2.5" />
              </span>
            </span>
            <span class="min-w-0 flex-1">
              <span class="block truncate text-sm font-medium" :title="sub.display_name || sub.username">{{ sub.display_name || sub.username }}</span>
              <span v-if="sub.display_name && sub.display_name !== sub.username" class="mt-0.5 block truncate text-[11px] text-muted-foreground">{{ sub.username }}</span>
            </span>
            <Badge variant="outline" class="h-6 shrink-0 rounded-md px-2 text-xs">{{ targetTypeMeta(sub.type) }}</Badge>
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
          <label v-if="showAutoTrackOption" class="inline-flex items-center gap-1.5 text-xs text-foreground/85 cursor-pointer select-none">
            <Switch v-model="addToAutoTrack" />
            <span>同时加入自动追更</span>
          </label>
        </div>
        <div class="flex items-center gap-2">
          <Button variant="outline" size="sm" @click="emit('close')">取消</Button>
          <Button size="sm" :disabled="selected.size === 0 || (showBaselineOption && initBaseline && !baselinePagesValid)" @click="confirm">{{ confirmLabel }}</Button>
        </div>
      </div>
    </template>
  </Dialog>
</template>
