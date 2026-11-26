<script setup>
import { computed, onActivated, ref, watch } from 'vue'
import {
  Save,
  Cookie,
  Sliders,
  CloudUpload,
  Bell,
  CheckCircle2,
  RefreshCw,
  PlugZap,
  Send,
  Loader2,
  Settings,
  Code2,
  RotateCcw,
  ShieldCheck,
  ChevronDown,
  Camera,
  UserRound,
  Trash2,
  KeyRound,
} from 'lucide-vue-next'
import { api } from '../api/client'
import { useAppStore } from '../stores/app'
import { prepareAvatarDataUrl } from '../utils/avatarImage'
import Card from '../components/ui/Card.vue'
import Button from '../components/ui/Button.vue'
import Badge from '../components/ui/Badge.vue'
import Input from '../components/ui/Input.vue'
import Textarea from '../components/ui/Textarea.vue'
import Switch from '../components/ui/Switch.vue'
import Tabs from '../components/ui/Tabs.vue'
import Select from '../components/ui/Select.vue'
import SiteBadge from '../components/SiteBadge.vue'
import SegmentedControl from '../components/ui/SegmentedControl.vue'

const DEFAULT_SELECTORS = Object.freeze({
  list_page_main_containers:
    'ul.videos#videoSearchResult, div#videoCategory ul.videos, ul#searchResults.videos.video-listing.clear, ul.videos[data-type="pornstar"], ul.videos.row-5-thumbs#mostRecentVideosSection, ul#mostRecentVideosSection, ul.videos.section-listing, ul#showAllChanelVideos, ul.videos.video-listing.clear.profileContent, ul.videos.video-listing.clear.playlistContent, div.container > ul.videos, ul#moreData.videos, ul#likedVideosSection.videos',
  list_page_li_in_container:
    'li.videoBox[data-video-vkey], li.pcVideoListItem[data-video-vkey], li.js-pop.videoblock.videoBox, li.pcVideoListItem.videoBox, li.videoBox, li.pcVideoListItem',
  list_page_li_fallback:
    'li[data-video-vkey], ul.videos > li[data-video-vkey], div.videoUList > ul > li.videoBox[data-video-vkey], ul.videos > li.videoBox, ul.videos > li.pcVideoListItem.videoblock, li.videoPreviewBg',
  list_page_video_link_tag_primary:
    'a[href*="view_video.php?viewkey="], a[href*="/video/show?viewkey="]',
  list_page_video_link_tag_fallback_href: 'a[href*="viewkey="]',
  list_page_video_image_for_title: 'img[alt]',
  list_page_video_span_div_for_title: 'span.title, div.title, var.video_title',
  detail_page_h1_titles:
    'h1.title span.inlineFree, h1.video-title span.inlineFree, h1.title.straightTitle, h1.title, h1.video-title, span.inlineFree',
  detail_page_og_title_meta: 'meta[property="og:title"]',
  detail_page_title_tag: 'title',
  detail_page_uploader_container: 'div.userInfo, div.video-detailed-info, div.p_cnt',
  detail_page_uploader_link_in_container:
    "a[href*='/model/'], a[href*='/pornstar/'], a[href*='/users/'], a[href*='/user/']",
  subs_page_main_container: 'ul#moreData, ul.userWidgetWrapperGrid',
  subs_page_user_link_in_container: 'div.user-flag a.userLink[href]',
  pornstar_page_video_container_check: 'div.profileVids',
})

const SELECTOR_FIELDS = [
  { key: 'list_page_main_containers', label: '列表页主视频容器' },
  { key: 'list_page_li_in_container', label: '容器内视频卡片元素 (li)' },
  { key: 'list_page_li_fallback', label: '列表页兜底视频卡片元素' },
  { key: 'list_page_video_link_tag_primary', label: '列表卡片视频主链接' },
  { key: 'list_page_video_link_tag_fallback_href', label: '列表卡片兜底链接' },
  { key: 'list_page_video_image_for_title', label: '列表卡片封面图标题属性' },
  { key: 'list_page_video_span_div_for_title', label: '列表卡片文本标题节点' },
  { key: 'detail_page_h1_titles', label: '详情页 H1 标题选择器' },
  { key: 'detail_page_og_title_meta', label: '详情页 OG Meta 标题' },
  { key: 'detail_page_title_tag', label: '详情页 Title 标签兜底' },
  { key: 'detail_page_uploader_container', label: '详情页上传者区域容器' },
  { key: 'detail_page_uploader_link_in_container', label: '详情页上传者主页链接' },
  { key: 'subs_page_main_container', label: '订阅页列表主容器' },
  { key: 'subs_page_user_link_in_container', label: '订阅页用户链接' },
  { key: 'pornstar_page_video_container_check', label: 'Pornstar 上传容器校验' },
]

const YESCAPTCHA_SITE_OPTIONS = [
  { label: 'PornHub', value: 'pornhub' },
  { label: 'xHamster', value: 'xhamster' },
]

const YESCAPTCHA_TASK_TYPES = {
  pornhub: [
    { label: 'reCAPTCHA v2 企业版（PornHub 使用 enterprise.js）', value: 'RecaptchaV2EnterpriseTaskProxyless' },
  ],
  xhamster: [
    { label: '标准 v2 · 低价线路（15 点/次）', value: 'NoCaptchaTaskProxyless' },
    { label: '标准 v2 · 标准线路（20 点/次）', value: 'RecaptchaV2TaskProxyless' },
  ],
}

const UPDATE_SCAN_MODE_OPTIONS = [
  { label: '智能衔接（快 · 衔接已知视频即止）', value: 'catch_up' },
  { label: '全量翻页（全 · 扫完所有分页不漏老片）', value: 'full' },
]

const XH_CODEC_OPTIONS = [
  { label: 'H.264（默认 · 兼容性最好）', value: 'h264' },
  { label: 'AV1（同画质体积更小，需播放器支持）', value: 'av1' },
]

function cloneSettings(src) {
  if (!src) return null
  return {
    ...src,
    yescaptcha_ph_task_type:
      src.yescaptcha_ph_task_type || 'RecaptchaV2EnterpriseTaskProxyless',
    yescaptcha_xh_task_type: src.yescaptcha_xh_task_type || 'RecaptchaV2TaskProxyless',
    selectors: { ...DEFAULT_SELECTORS, ...(src.selectors || {}) },
  }
}

const AUTH_MODES = [
  { value: 'account', label: '账号登录', icon: KeyRound },
  { value: 'cookie', label: '粘贴 Cookie', icon: Cookie },
]

const store = useAppStore()
const activeTab = ref('cookie')
const loading = ref(!store.settingsCache)
const saving = ref(false)
const busyTool = ref('')
const form = ref(cloneSettings(store.settingsCache))
const pasteCookie = ref('')
const pasteXhCookie = ref('')
const phAuthMode = ref(
  store.settingsCache?.has_cookie && !String(store.settingsCache?.ph_username || '').trim()
    ? 'cookie'
    : 'account'
)
const xhAuthMode = ref(
  store.settingsCache?.has_xh_cookie && !String(store.settingsCache?.xh_username || '').trim()
    ? 'cookie'
    : 'account'
)
const yescaptchaTaskSite = ref('pornhub')
const yescaptchaTaskTypeOptions = computed(
  () => YESCAPTCHA_TASK_TYPES[yescaptchaTaskSite.value] || []
)
const currentYescaptchaTaskType = computed({
  get: () => {
    if (!form.value) return ''
    return yescaptchaTaskSite.value === 'pornhub'
      ? form.value.yescaptcha_ph_task_type
      : form.value.yescaptcha_xh_task_type
  },
  set: (value) => {
    if (!form.value) return
    if (yescaptchaTaskSite.value === 'pornhub') form.value.yescaptcha_ph_task_type = value
    else form.value.yescaptcha_xh_task_type = value
  },
})
let authModesSynced = Boolean(store.settingsCache)
const avatarInputRef = ref(null)
const avatarLoadError = ref(false)
const rcloneRemotes = ref([])
const loadingRemotes = ref(false)

watch(
  () => store.accountAvatarUrl,
  () => {
    avatarLoadError.value = false
  }
)
const remotesLoadedOnce = ref(false)
const providerTab = ref('pikpak')
const showRcloneAdvanced = ref(false)

const REMOTE_TYPE_LABELS = {
  pikpak: 'PikPak',
  drive: 'Google Drive',
  onedrive: 'OneDrive',
  s3: 'S3 / R2',
  webdav: 'WebDAV',
  dropbox: 'Dropbox',
  sftp: 'SFTP',
}

const PROVIDER_TABS = [
  { value: 'pikpak', label: 'PikPak' },
  { value: 'drive', label: 'Google Drive' },
  { value: 'custom', label: '其他网盘 / 自定义参数' },
]

function formatRemoteType(type) {
  const key = String(type || '').trim().toLowerCase()
  return REMOTE_TYPE_LABELS[key] || (key ? key.toUpperCase() : '未知类型')
}

const activeRemoteType = computed(() => {
  const name = String(form.value?.rclone_remote || '').trim().replace(/:+$/, '')
  if (!name) return ''
  const matched = rcloneRemotes.value.find((r) => r.name.toLowerCase() === name.toLowerCase())
  if (matched?.type) return matched.type.toLowerCase()
  const lower = name.toLowerCase()
  if (lower === 'pikpak') return 'pikpak'
  if (['gd', 'gdrive', 'drive', 'google', 'googledrive'].includes(lower)) return 'drive'
  return ''
})

function syncProviderTabFromType(rtype) {
  if (rtype === 'pikpak') providerTab.value = 'pikpak'
  else if (rtype === 'drive') providerTab.value = 'drive'
  else if (rtype) providerTab.value = 'custom'
}

async function loadRcloneRemotes({ force = false } = {}) {
  if (loadingRemotes.value || (remotesLoadedOnce.value && !force)) return
  loadingRemotes.value = true
  try {
    const res = await api.listRcloneRemotes()
    rcloneRemotes.value = Array.isArray(res?.remotes) ? res.remotes : []
    remotesLoadedOnce.value = true
    syncProviderTabFromType(activeRemoteType.value)
  } catch {
    // Keep manual remote input usable if rclone binary is not present locally
  } finally {
    loadingRemotes.value = false
  }
}

function selectRcloneRemote(remote) {
  if (!form.value || !remote?.name) return
  form.value.rclone_remote = remote.name
  syncProviderTabFromType(String(remote.type || '').toLowerCase() || activeRemoteType.value)
}

watch(activeTab, (tab) => {
  if (tab === 'rclone') {
    loadRcloneRemotes()
  }
})

watch(activeRemoteType, (rtype) => {
  syncProviderTabFromType(rtype)
}, { immediate: true })

const tabs = [
  { value: 'cookie', label: 'Cookie 与账号', icon: Cookie },
  { value: 'download', label: '下载与背压', icon: Sliders },
  { value: 'rclone', label: 'rclone 上传', icon: CloudUpload },
  { value: 'notify', label: '追更与通知', icon: Bell },
  { value: 'selectors', label: '解析选择器', icon: Code2 },
]

function resetSelectorsToDefault() {
  if (!form.value) return
  form.value.selectors = { ...DEFAULT_SELECTORS }
  store.notify('已填入默认 CSS 选择器，点击右上角“保存全部设置”生效', 'info')
}

let activatedOnce = false

function syncInitialAuthModes(settings) {
  if (authModesSynced || !settings) return
  authModesSynced = true
  phAuthMode.value = settings.has_cookie && !String(settings.ph_username || '').trim() ? 'cookie' : 'account'
  xhAuthMode.value = settings.has_xh_cookie && !String(settings.xh_username || '').trim() ? 'cookie' : 'account'
}

async function loadSettings() {
  if (!form.value) loading.value = true
  try {
    const settings = await store.refreshSettings()
    if (settings && !saving.value) {
      form.value = cloneSettings(settings)
      syncInitialAuthModes(settings)
    }
  } catch (err) {
    store.notify(err.message, 'error')
  } finally {
    loading.value = false
  }
}

watch(
  () => store.settingsCache,
  (val) => {
    if (val && !saving.value) {
      form.value = cloneSettings(val)
      syncInitialAuthModes(val)
      loading.value = false
    }
  }
)

onActivated(() => {
  if (!activatedOnce) {
    activatedOnce = true
    if (store.hydratedFromCache) return
    if (!form.value) {
      loadSettings()
    } else if (Date.now() - store.settingsLoadedAt > 1500) {
      loadSettings()
    }
    return
  }
  loadSettings()
})

async function saveAll() {
  if (!form.value) return
  saving.value = true
  try {
    const res = await api.saveSettings(form.value)
    form.value = cloneSettings(res.settings)
    store.setSettingsCache(res.settings)
    store.notify('系统设置已保存', 'success')
    await store.refreshOverview()
  } catch (err) {
    store.notify(err.message, 'error')
  } finally {
    saving.value = false
  }
}

async function handleAvatarChange(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file || busyTool.value !== '') return
  busyTool.value = 'avatar_upload'
  try {
    const dataUrl = await prepareAvatarDataUrl(file)
    const res = await store.uploadAccountAvatar(dataUrl)
    if (res?.settings && form.value) {
      form.value.account_avatar_version = res.settings.account_avatar_version || ''
    }
    avatarLoadError.value = false
    store.notify('本地账号头像已更新', 'success')
  } catch (err) {
    store.notify(err.message || '头像上传失败', 'error')
  } finally {
    busyTool.value = ''
  }
}

async function handleRemoveAvatar() {
  if (busyTool.value !== '') return
  busyTool.value = 'avatar_remove'
  try {
    const res = await store.removeAccountAvatar()
    if (res?.settings && form.value) {
      form.value.account_avatar_version = ''
    }
    avatarLoadError.value = false
    store.notify('已恢复默认头像', 'info')
  } catch (err) {
    store.notify(err.message || '移除头像失败', 'error')
  } finally {
    busyTool.value = ''
  }
}

async function handleTestCookie() {
  busyTool.value = 'cookie_test'
  try {
    const res = await api.testCookie()
    store.notify(res.message, res.valid ? 'success' : 'warning')
    await store.refreshOverview()
  } catch (err) {
    store.notify(err.message, 'error')
  } finally {
    busyTool.value = ''
  }
}

async function handleImportCookie() {
  if (!pasteCookie.value.trim()) return
  busyTool.value = 'cookie_import'
  try {
    if (form.value && String(form.value.ph_logged_in_username || '').trim()) {
      await api.saveSettings({ ph_logged_in_username: form.value.ph_logged_in_username.trim() })
    }
    const res = await api.importCookie(pasteCookie.value.trim())
    store.clearSubscriptionsCache()
    pasteCookie.value = ''
    store.notify(`Cookie 已保存：${res.message}`, res.valid ? 'success' : 'warning')
    await Promise.all([loadSettings(), store.refreshOverview()])
  } catch (err) {
    store.notify(err.message, 'error')
  } finally {
    busyTool.value = ''
  }
}

async function handleRefreshCookie() {
  if (!form.value) return
  if (!String(form.value.ph_username || '').trim() || !String(form.value.ph_password || '').trim()) {
    store.notify('请先填写 PornHub 账号与密码', 'warning')
    return
  }
  busyTool.value = 'cookie_refresh'
  try {
    const saved = await api.saveSettings(form.value)
    if (saved?.settings) {
      form.value = cloneSettings(saved.settings)
      store.setSettingsCache(saved.settings)
    }
    const res = await api.refreshCookie()
    if (res.success) store.clearSubscriptionsCache()
    store.notify(res.message, res.success ? 'success' : 'error')
    await Promise.all([loadSettings(), store.refreshOverview()])
  } catch (err) {
    store.notify(err.message, 'error')
  } finally {
    busyTool.value = ''
  }
}

async function handleTestXhCookie() {
  busyTool.value = 'xh_cookie_test'
  try {
    const res = await api.testXhCookie()
    store.notify(res.message, res.valid ? 'success' : 'warning')
    await Promise.all([loadSettings(), store.refreshOverview()])
  } catch (err) {
    store.notify(err.message, 'error')
  } finally {
    busyTool.value = ''
  }
}

async function handleImportXhCookie() {
  if (!pasteXhCookie.value.trim()) return
  busyTool.value = 'xh_cookie_import'
  try {
    const res = await api.importXhCookie(pasteXhCookie.value.trim())
    store.clearSubscriptionsCache()
    pasteXhCookie.value = ''
    store.notify(`xHamster Cookie 已保存：${res.message}`, res.valid ? 'success' : 'warning')
    await Promise.all([loadSettings(), store.refreshOverview()])
  } catch (err) {
    store.notify(err.message, 'error')
  } finally {
    busyTool.value = ''
  }
}

async function handleRefreshXhCookie() {
  if (!form.value) return
  if (!String(form.value.xh_username || '').trim() || !String(form.value.xh_password || '').trim()) {
    store.notify('请先填写 xHamster 账号与密码', 'warning')
    return
  }
  busyTool.value = 'xh_cookie_refresh'
  try {
    const saved = await api.saveSettings(form.value)
    if (saved?.settings) {
      form.value = cloneSettings(saved.settings)
      store.setSettingsCache(saved.settings)
    }
    const res = await api.refreshXhCookie()
    if (res.success) store.clearSubscriptionsCache()
    store.notify(res.message, res.success ? 'success' : 'error')
    await Promise.all([loadSettings(), store.refreshOverview()])
  } catch (err) {
    store.notify(err.message, 'error')
  } finally {
    busyTool.value = ''
  }
}

async function handleTestYescaptcha() {
  busyTool.value = 'yescaptcha'
  try {
    const saved = await api.saveSettings(form.value)
    if (saved?.settings) {
      form.value = cloneSettings(saved.settings)
      store.setSettingsCache(saved.settings)
    }
    const res = await api.testYescaptcha()
    store.notify(res.message, res.ok ? 'success' : 'error')
  } catch (err) {
    store.notify(err.message, 'error')
  } finally {
    busyTool.value = ''
  }
}

async function handleTestRclone() {
  busyTool.value = 'rclone'
  try {
    const saved = await api.saveSettings(form.value)
    if (saved?.settings) {
      form.value = cloneSettings(saved.settings)
      store.setSettingsCache(saved.settings)
    }
    const res = await api.testRclone()
    store.notify(res.message, res.ok ? 'success' : 'error')
    loadRcloneRemotes({ force: true })
  } catch (err) {
    store.notify(err.message, 'error')
  } finally {
    busyTool.value = ''
  }
}

async function handleTestNotify() {
  busyTool.value = 'notify'
  try {
    const saved = await api.saveSettings(form.value)
    if (saved?.settings) {
      form.value = cloneSettings(saved.settings)
      store.setSettingsCache(saved.settings)
    }
    const res = await api.testNotify()
    store.notify(res.message, res.ok ? 'success' : 'error')
  } catch (err) {
    store.notify(err.message, 'error')
  } finally {
    busyTool.value = ''
  }
}
</script>

<template>
  <div class="space-y-4 sm:space-y-6">
    <!-- Page Header -->
    <div class="flex flex-wrap items-center justify-between gap-3 sm:gap-4">
      <div class="flex items-center gap-3 sm:gap-3.5 min-w-0">
        <div
          class="w-9 h-9 sm:w-10 sm:h-10 rounded-xl bg-brand/10 text-brand flex items-center justify-center shrink-0 ring-1 ring-brand/15"
        >
          <Settings class="w-4 h-4 sm:w-5 sm:h-5" />
        </div>
        <div class="min-w-0">
          <h1 class="text-lg sm:text-xl font-bold tracking-tight text-foreground">系统设置</h1>
          <p class="text-xs sm:text-sm text-muted-foreground mt-0.5">
            管理登录 Cookie、下载并发与磁盘背压、rclone 云端上传及通知策略
          </p>
        </div>
      </div>
      <Button size="sm" class="w-full sm:w-auto" :disabled="saving || !form" @click="saveAll">
        <Loader2 v-if="saving" class="w-3.5 h-3.5 animate-spin" />
        <Save v-else class="w-3.5 h-3.5" />
        {{ saving ? '保存中...' : '保存全部设置' }}
      </Button>
    </div>

    <div v-if="loading || !form" class="py-16 flex items-center justify-center gap-2 text-xs text-muted-foreground leading-none">
      <Loader2 class="w-4 h-4 animate-spin shrink-0 text-brand" />
      <span>加载配置中...</span>
    </div>

    <template v-else>
      <div class="overflow-x-auto no-scrollbar -mx-3.5 px-3.5 sm:mx-0 sm:px-0 pb-0.5">
        <Tabs v-model="activeTab" :items="tabs" />
      </div>

      <!-- Tab 1: Cookie & Account -->
      <Card v-if="activeTab === 'cookie'" class="divide-y divide-border">
        <!-- PornHub Auth -->
        <div class="p-4 sm:p-6 space-y-4">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div class="space-y-1">
              <div class="flex flex-wrap items-center gap-2">
                <SiteBadge site="pornhub" size="sm" />
                <h3 class="text-sm font-semibold text-foreground">PornHub 账号与 Cookie</h3>
                <Badge
                  variant="outline"
                  :dot="
                    store.overview?.cookie?.valid === true
                      ? 'bg-emerald-500'
                      : store.overview?.cookie?.valid === false
                        ? 'bg-red-500'
                        : 'bg-zinc-400'
                  "
                >
                  {{ store.overview?.cookie?.message || (form.has_cookie ? '已配置' : '未配置') }}
                </Badge>
              </div>
              <p class="text-xs text-muted-foreground">
                可在「账号登录」自动获取/续期 Cookie，或切换至「粘贴 Cookie」直接导入浏览器请求头
              </p>
            </div>
            <div class="flex flex-wrap items-center gap-2">
              <SegmentedControl
                v-model="phAuthMode"
                :options="AUTH_MODES"
                aria-label="PornHub 认证方式"
                size="sm"
              />
              <Button variant="outline" size="xs" :disabled="busyTool !== ''" @click="handleTestCookie">
                <Loader2 v-if="busyTool === 'cookie_test'" class="w-3.5 h-3.5 animate-spin" />
                <CheckCircle2 v-else class="w-3.5 h-3.5" />
                检测有效性
              </Button>
            </div>
          </div>

          <div v-if="phAuthMode === 'account'" class="space-y-3.5">
            <div class="grid grid-cols-1 md:grid-cols-3 gap-3.5">
              <div class="space-y-1.5">
                <label class="text-xs font-medium text-foreground">账号 / 邮箱</label>
                <Input v-model="form.ph_username" placeholder="PornHub 账号或邮箱" />
              </div>
              <div class="space-y-1.5">
                <label class="text-xs font-medium text-foreground">密码（已脱敏）</label>
                <Input v-model="form.ph_password" type="password" placeholder="输入密码" />
              </div>
              <div class="space-y-1.5">
                <label class="text-xs font-medium text-foreground">已登录用户名</label>
                <Input v-model="form.ph_logged_in_username" placeholder="拉取订阅列表用（登录后自动填）" />
              </div>
            </div>
            <div class="flex flex-wrap items-center justify-between gap-3">
              <p class="text-xs text-muted-foreground">
                点击登录会自动保存凭据并刷新 Cookie；遇 reCAPTCHA 时自动调用下方 YesCaptcha
              </p>
              <Button
                size="sm"
                :disabled="!String(form.ph_username || '').trim() || !String(form.ph_password || '').trim() || busyTool !== ''"
                @click="handleRefreshCookie"
              >
                <RefreshCw
                  :class="['w-3.5 h-3.5', busyTool === 'cookie_refresh' ? 'animate-spin' : '']"
                />
                {{ busyTool === 'cookie_refresh' ? '正在登录...' : '登录并获取 Cookie' }}
              </Button>
            </div>
          </div>

          <div v-else class="space-y-3.5">
            <Textarea
              v-model="pasteCookie"
              :rows="3"
              placeholder="粘贴从 pornhub.com 浏览器请求头复制的 Cookie 字符串..."
              class="font-mono text-xs"
            />
            <div class="flex flex-wrap items-center justify-between gap-3">
              <div class="flex items-center gap-2 min-w-0">
                <label class="text-xs text-muted-foreground shrink-0">已登录用户名</label>
                <Input
                  v-model="form.ph_logged_in_username"
                  placeholder="拉取订阅用（可选）"
                  class="h-8 w-44 text-xs"
                />
              </div>
              <Button
                size="sm"
                :disabled="!pasteCookie.trim() || busyTool !== ''"
                @click="handleImportCookie"
              >
                <Loader2 v-if="busyTool === 'cookie_import'" class="w-3.5 h-3.5 animate-spin" />
                保存粘贴的 Cookie 并测活
              </Button>
            </div>
          </div>
        </div>

        <!-- xHamster Auth -->
        <div class="p-4 sm:p-6 space-y-4">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div class="space-y-1">
              <div class="flex flex-wrap items-center gap-2">
                <SiteBadge site="xhamster" size="sm" />
                <h3 class="text-sm font-semibold text-foreground">xHamster 账号与 Cookie（可选）</h3>
                <Badge
                  variant="outline"
                  :dot="
                    store.overview?.xh_cookie?.valid === true
                      ? 'bg-emerald-500'
                      : store.overview?.xh_cookie?.valid === false
                        ? 'bg-red-500'
                        : 'bg-zinc-400'
                  "
                >
                  {{ store.overview?.xh_cookie?.message || (form.has_xh_cookie ? '已配置' : '未配置（公开下载无需）') }}
                </Badge>
              </div>
              <p class="text-xs text-muted-foreground">
                公开博主、频道与单视频下载均无需登录；仅「从 xHamster 订阅导入」与「xHamster 收藏夹」需要认证
              </p>
            </div>
            <div class="flex flex-wrap items-center gap-2">
              <SegmentedControl
                v-model="xhAuthMode"
                :options="AUTH_MODES"
                aria-label="xHamster 认证方式"
                size="sm"
              />
              <Button variant="outline" size="xs" :disabled="busyTool !== ''" @click="handleTestXhCookie">
                <Loader2 v-if="busyTool === 'xh_cookie_test'" class="w-3.5 h-3.5 animate-spin" />
                <CheckCircle2 v-else class="w-3.5 h-3.5" />
                检测有效性
              </Button>
            </div>
          </div>

          <div v-if="xhAuthMode === 'account'" class="space-y-3.5">
            <div class="grid grid-cols-1 md:grid-cols-3 gap-3.5">
              <div class="space-y-1.5">
                <label class="text-xs font-medium text-foreground">账号 / 邮箱</label>
                <Input v-model="form.xh_username" placeholder="xHamster 账号或邮箱" />
              </div>
              <div class="space-y-1.5">
                <label class="text-xs font-medium text-foreground">密码（已脱敏）</label>
                <Input v-model="form.xh_password" type="password" placeholder="输入密码" />
              </div>
              <div class="space-y-1.5">
                <label class="text-xs font-medium text-foreground">已登录用户名</label>
                <Input v-model="form.xh_logged_in_username" placeholder="登录后自动识别" />
              </div>
            </div>
            <div class="flex flex-wrap items-center justify-between gap-3">
              <p class="text-xs text-muted-foreground">
                点击登录会自动保存凭据并获取长期 Cookie，Cookie 过期时后台任务也会自动续期
              </p>
              <Button
                size="sm"
                :disabled="!String(form.xh_username || '').trim() || !String(form.xh_password || '').trim() || busyTool !== ''"
                @click="handleRefreshXhCookie"
              >
                <RefreshCw
                  :class="['w-3.5 h-3.5', busyTool === 'xh_cookie_refresh' ? 'animate-spin' : '']"
                />
                {{ busyTool === 'xh_cookie_refresh' ? '正在登录...' : '登录并获取 Cookie' }}
              </Button>
            </div>
          </div>

          <div v-else class="space-y-3.5">
            <Textarea
              v-model="pasteXhCookie"
              :rows="3"
              placeholder="粘贴从 xhamster.com 浏览器请求头复制的 Cookie 字符串..."
              class="font-mono text-xs"
            />
            <div class="flex flex-wrap items-center justify-between gap-3">
              <span v-if="form.xh_logged_in_username" class="text-xs text-muted-foreground">
                当前已识别 xHamster 账号：<strong class="text-foreground">{{ form.xh_logged_in_username }}</strong>
              </span>
              <span v-else class="text-xs text-muted-foreground">
                保存后将自动测活并识别已登录用户名
              </span>
              <Button
                size="sm"
                :disabled="!pasteXhCookie.trim() || busyTool !== ''"
                @click="handleImportXhCookie"
              >
                <Loader2 v-if="busyTool === 'xh_cookie_import'" class="w-3.5 h-3.5 animate-spin" />
                保存 xHamster Cookie 并测活
              </Button>
            </div>
          </div>
        </div>

        <!-- Local Panel Avatar -->
        <div class="p-4 sm:p-6">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h3 class="text-sm font-semibold text-foreground">本地面板头像（可选）</h3>
              <p class="text-xs text-muted-foreground mt-0.5">
                仅保存在本地控制面板用于订阅弹窗与侧栏显示，不会修改站点账号信息
              </p>
            </div>
            <div class="flex items-center gap-2.5">
              <button
                type="button"
                class="group relative flex h-10 w-10 shrink-0 items-center justify-center overflow-hidden rounded-full border border-border bg-surface-subtle text-muted-foreground cursor-pointer transition-colors hover:border-brand/50"
                :disabled="busyTool !== ''"
                title="点击更换本地账号头像"
                @click="avatarInputRef?.click()"
              >
                <img
                  v-if="store.accountAvatarUrl && !avatarLoadError"
                  :key="store.accountAvatarUrl"
                  :src="store.accountAvatarUrl"
                  alt=""
                  class="h-full w-full object-cover"
                  @error="avatarLoadError = true"
                />
                <UserRound v-else class="w-4 h-4" />
                <span class="absolute inset-0 flex items-center justify-center bg-black/45 text-white opacity-0 transition-opacity group-hover:opacity-100">
                  <Loader2 v-if="busyTool === 'avatar_upload'" class="w-3.5 h-3.5 animate-spin" />
                  <Camera v-else class="w-3.5 h-3.5" />
                </span>
              </button>
              <input
                ref="avatarInputRef"
                type="file"
                accept="image/png,image/jpeg,image/webp,image/gif"
                class="hidden"
                @change="handleAvatarChange"
              />
              <Button
                variant="outline"
                size="xs"
                :disabled="busyTool !== ''"
                @click="avatarInputRef?.click()"
              >
                <Loader2 v-if="busyTool === 'avatar_upload'" class="w-3.5 h-3.5 animate-spin" />
                <Camera v-else class="w-3.5 h-3.5" />
                更换头像
              </Button>
              <Button
                v-if="store.accountAvatarUrl"
                variant="ghost"
                size="xs"
                class="text-muted-foreground hover:text-foreground"
                :disabled="busyTool !== ''"
                @click="handleRemoveAvatar"
              >
                <Trash2 class="w-3.5 h-3.5" />
                恢复默认
              </Button>
            </div>
          </div>
        </div>

        <div class="p-4 sm:p-6 space-y-4">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div class="flex items-center gap-3">
              <div
                class="w-9 h-9 rounded-xl bg-brand/10 text-brand flex items-center justify-center shrink-0 ring-1 ring-brand/15"
              >
                <ShieldCheck class="w-4 h-4" />
              </div>
              <div>
                <div class="flex items-center gap-2">
                  <h3 class="text-sm font-semibold text-foreground">YesCaptcha 自动过验证码</h3>
                  <Switch v-model="form.yescaptcha_enabled" />
                </div>
                <p class="text-xs text-muted-foreground mt-0.5">
                  自动登录遇到 reCAPTCHA 时调用 YesCaptcha 获取 token；仅在触发验证码时消耗点数
                </p>
              </div>
            </div>
            <Button
              variant="outline"
              size="xs"
              :disabled="busyTool !== ''"
              @click="handleTestYescaptcha"
            >
              <PlugZap class="w-3.5 h-3.5" />
              测试 YesCaptcha
            </Button>
          </div>

          <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
            <div class="space-y-1.5">
              <label class="text-xs font-medium text-foreground">ClientKey（已脱敏）</label>
              <Input
                v-model="form.yescaptcha_client_key"
                type="password"
                placeholder="从 yescaptcha.com 后台复制"
              />
            </div>
            <div class="space-y-1.5">
              <div class="flex flex-wrap items-center justify-between gap-2">
                <label class="text-xs font-medium text-foreground">识别任务类型</label>
                <SegmentedControl
                  v-model="yescaptchaTaskSite"
                  :options="YESCAPTCHA_SITE_OPTIONS"
                  aria-label="验证码任务站点"
                  size="sm"
                />
              </div>
              <Select v-model="currentYescaptchaTaskType" :options="yescaptchaTaskTypeOptions" />
              <p class="text-[11px] text-muted-foreground">
                {{
                  yescaptchaTaskSite === 'pornhub'
                    ? 'PornHub 使用企业版 v2 线路。'
                    : '两个标准 v2 线路参数相同，可互相切换。'
                }}
              </p>
            </div>
            <div class="space-y-1.5">
              <label class="text-xs font-medium text-foreground">解题超时（秒）</label>
              <Input v-model="form.yescaptcha_timeout_seconds" type="number" :min="30" :max="600" />
              <p class="text-[11px] text-muted-foreground">两个站点共用此超时。</p>
            </div>
          </div>

          <p class="text-xs text-muted-foreground">
            默认使用国际节点 api.yescaptcha.com。PornHub 固定企业版 v2；xHamster 可选两个标准 v2 线路。
            若 token 提交后仍要求验证码，可切换线路或粘贴浏览器 Cookie。
          </p>
        </div>
      </Card>

      <!-- Tab 2: Download & Backpressure -->
      <Card v-if="activeTab === 'download'" class="p-4 sm:p-5 space-y-4">
        <div>
          <h3 class="text-sm font-semibold text-foreground">并发与磁盘背压控制</h3>
          <p class="text-xs text-muted-foreground mt-0.5">
            当待上传文件堆积或剩余磁盘空间低于阈值时，引擎会自动降档并发并等待 rclone 释放空间
          </p>
        </div>

        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3.5">
          <div class="space-y-1.5">
            <label class="text-xs font-medium text-foreground">最大视频下载并发数</label>
            <Input v-model="form.max_concurrent_downloads" type="number" :min="1" :max="16" />
          </div>
          <div class="space-y-1.5">
            <label class="text-xs font-medium text-foreground">详情页并发解析数</label>
            <Input v-model="form.max_concurrent_video_details" type="number" :min="1" :max="40" />
          </div>
          <div class="space-y-1.5">
            <label class="text-xs font-medium text-foreground">列表页预取并发数</label>
            <Input v-model="form.max_concurrent_list_pages" type="number" :min="1" :max="10" />
          </div>
          <div class="space-y-1.5">
            <label class="text-xs font-medium text-foreground">默认偏好画质（留空最高）</label>
            <Input v-model="form.preferred_quality" placeholder="如 2160,1440,1080" />
          </div>
          <div class="space-y-1.5">
            <label
              class="text-xs font-medium text-foreground"
              title="下载 xHamster 高清/4K 视频时优先选择的视频编码格式；所选编码不存在时会自动回退到另一编码"
            >
              xHamster 编码偏好
            </label>
            <Select v-model="form.xh_preferred_codec" :options="XH_CODEC_OPTIONS" />
          </div>
          <div class="space-y-1.5">
            <label class="text-xs font-medium text-foreground">待上传最大本地堆积数</label>
            <Input v-model="form.max_pending_upload_files" type="number" :min="1" :max="200" />
          </div>
          <div class="space-y-1.5">
            <label class="text-xs font-medium text-foreground">VPS 最低保留磁盘空间 (GB)</label>
            <Input v-model="form.min_free_disk_gb" type="number" :min="0" :step="0.5" />
          </div>
          <div class="space-y-1.5">
            <label class="text-xs font-medium text-foreground">单文件下载重试次数</label>
            <Input v-model="form.download_attempts" type="number" :min="1" :max="10" />
          </div>
          <div class="space-y-1.5">
            <label class="text-xs font-medium text-foreground">翻页最小 / 最大间隔 (秒)</label>
            <div class="grid grid-cols-2 gap-2">
              <Input v-model="form.page_delay_min" type="number" :step="0.1" />
              <Input v-model="form.page_delay_max" type="number" :step="0.1" />
            </div>
          </div>
          <div class="space-y-1.5">
            <label class="text-xs font-medium text-foreground">页面请求超时 (秒)</label>
            <Input v-model="form.request_timeout" type="number" :min="5" :max="180" :step="1" />
          </div>
          <div class="space-y-1.5">
            <label class="text-xs font-medium text-foreground">页面请求最大重试次数</label>
            <Input v-model="form.max_retries_page_fetch" type="number" :min="0" :max="10" />
          </div>
          <div class="space-y-1.5">
            <label class="text-xs font-medium text-foreground">重试延迟基数 / 上限 (秒)</label>
            <div class="grid grid-cols-2 gap-2">
              <Input v-model="form.retry_delay_base" type="number" :min="0" :step="0.5" />
              <Input v-model="form.retry_delay_max" type="number" :min="0" :step="0.5" />
            </div>
          </div>
          <div class="space-y-1.5">
            <label class="text-xs font-medium text-foreground">最小有效视频体积 (字节)</label>
            <Input v-model="form.min_file_size_bytes" type="number" :min="1" />
          </div>
        </div>
      </Card>

      <!-- Tab 3: rclone Uploader -->
      <Card v-if="activeTab === 'rclone'" class="divide-y divide-border">
        <div class="px-4 sm:px-5 py-3.5 flex flex-wrap items-center justify-between gap-3">
          <div class="flex items-center gap-3">
            <Switch v-model="form.uploader_enabled" />
            <div>
              <div class="text-sm font-semibold text-foreground">启用 rclone 自动上传并删除本地文件</div>
              <div class="text-[11px] text-muted-foreground">
                边下边传：校验网盘文件无误后即刻清理本地副本
              </div>
            </div>
          </div>

          <div class="flex flex-wrap items-center gap-2 sm:ml-auto">
            <div v-if="rcloneRemotes.length > 0" class="flex flex-wrap items-center gap-1.5">
              <Button
                v-for="r in rcloneRemotes"
                :key="r.name"
                variant="outline"
                size="xs"
                :aria-pressed="String(form.rclone_remote || '').trim().toLowerCase() === r.name.toLowerCase()"
                :class="[
                  'h-7 gap-1.5 px-2.5 rounded-full text-xs font-medium shadow-none hover:shadow-none',
                  String(form.rclone_remote || '').trim().toLowerCase() === r.name.toLowerCase()
                    ? 'border-brand/40 bg-brand/10 dark:bg-brand/15 text-brand hover:border-brand/40 hover:bg-brand/10 dark:hover:bg-brand/15 hover:text-brand'
                    : 'border-border bg-card text-muted-foreground hover:text-foreground hover:border-foreground/20',
                ]"
                @click="selectRcloneRemote(r)"
              >
                <span class="font-mono">{{ r.name }}</span>
                <span class="text-[10px] opacity-75">{{ formatRemoteType(r.type) }}</span>
              </Button>
            </div>
            <Button
              variant="ghost"
              size="icon"
              class="h-7 w-7 text-muted-foreground"
              title="重新扫描 rclone.conf"
              :disabled="loadingRemotes"
              @click="loadRcloneRemotes({ force: true })"
            >
              <RefreshCw :class="['w-3.5 h-3.5', loadingRemotes ? 'animate-spin' : '']" />
            </Button>
            <Button variant="outline" size="xs" class="h-8" :disabled="busyTool !== ''" @click="handleTestRclone">
              <PlugZap class="w-3.5 h-3.5" />
              测试连接
            </Button>
          </div>
        </div>

        <div class="p-4 sm:p-5 space-y-4">
          <!-- Section 1: Core Common Settings (4x2 Compact Grid) -->
          <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3.5">
            <div class="space-y-1">
              <label class="text-xs font-medium text-foreground">Remote 名称</label>
              <Input v-model="form.rclone_remote" placeholder="pikpak 或 gd" />
            </div>
            <div class="space-y-1">
              <label class="text-xs font-medium text-foreground">PornHub 远端基础目录</label>
              <Input v-model="form.rclone_remote_base_dir" placeholder="Pornhub" />
            </div>
            <div class="space-y-1">
              <label class="text-xs font-medium text-foreground">xHamster 远端基础目录</label>
              <Input v-model="form.rclone_xh_remote_base_dir" placeholder="xHamster" />
            </div>
            <div class="space-y-1">
              <label class="text-xs font-medium text-foreground">并行上传 Worker 数</label>
              <Input v-model="form.upload_workers" type="number" :min="1" :max="8" />
            </div>
            <div class="space-y-1">
              <label class="text-xs font-medium text-foreground">单批触发上传数（1=即传）</label>
              <Input v-model="form.upload_batch_size" type="number" :min="1" :max="100" />
            </div>
            <div class="space-y-1">
              <label class="text-xs font-medium text-foreground">单 Worker transfers（建议 1）</label>
              <Input v-model="form.rclone_transfers" type="number" :min="1" :max="16" />
            </div>
            <div class="space-y-1">
              <label class="text-xs font-medium text-foreground">检查并发 (checkers)</label>
              <Input v-model="form.rclone_checkers" type="number" :min="1" :max="32" />
            </div>
            <div class="space-y-1">
              <label class="text-xs font-medium text-foreground">单次执行超时（秒）</label>
              <Input v-model="form.rclone_timeout_seconds" type="number" :min="30" :max="86400" />
            </div>
            <div class="space-y-1">
              <label class="text-xs font-medium text-foreground">TPS 限制 / 突发（0不限）</label>
              <div class="grid grid-cols-2 gap-1.5">
                <Input v-model="form.rclone_tpslimit" type="number" :min="0" :max="100" placeholder="0" />
                <Input v-model="form.rclone_tpslimit_burst" type="number" :min="0" :max="100" placeholder="0" />
              </div>
            </div>
          </div>

          <!-- Switches + Collapsible Advanced (bin / log level) -->
          <div class="flex flex-wrap items-center justify-between gap-3 pt-0.5">
            <div class="flex flex-wrap items-center gap-x-5 gap-y-2">
              <label class="inline-flex items-center gap-2 text-xs text-foreground/85 cursor-pointer">
                <Switch v-model="form.rclone_ignore_checksum" />
                <span>跳过 rclone 校验和检查</span>
              </label>
              <label class="inline-flex items-center gap-2 text-xs text-foreground/85 cursor-pointer">
                <Switch v-model="form.rclone_ignore_size" />
                <span>跳过 rclone 大小检查</span>
              </label>
              <label
                class="inline-flex items-center gap-2 text-xs text-foreground/85 cursor-pointer"
                title="部分网盘（如 PikPak）不返回 MD5；开启后会在上传完成时重新从网盘下载整部视频做逐字节校验，耗时翻倍，通常应保持关闭"
              >
                <Switch v-model="form.rclone_verify_download" />
                <span>无校验和时回下比对</span>
              </label>
            </div>

            <button
              type="button"
              class="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors cursor-pointer select-none"
              @click="showRcloneAdvanced = !showRcloneAdvanced"
            >
              <span>高级（命令路径 / 日志）</span>
              <ChevronDown :class="['w-3.5 h-3.5 transition-transform duration-200', showRcloneAdvanced ? 'rotate-180' : '']" />
            </button>
          </div>

          <div
            v-if="showRcloneAdvanced"
            class="grid grid-cols-1 sm:grid-cols-2 gap-3.5 rounded-xl border border-border/75 bg-surface-subtle/50 p-3.5"
          >
            <div class="space-y-1">
              <label class="text-xs font-medium text-foreground">rclone 命令路径</label>
              <Input v-model="form.rclone_bin" />
            </div>
            <div class="space-y-1">
              <label class="text-xs font-medium text-foreground">rclone 日志级别</label>
              <Input v-model="form.rclone_log_level" placeholder="ERROR" />
            </div>
          </div>

          <!-- Section 2: Provider-Specific Tuning (Compact Single-Row Cards) -->
          <div class="rounded-xl border border-border bg-surface-subtle/40 overflow-hidden">
            <div class="px-4 py-2.5 border-b border-border/80 flex flex-wrap items-center justify-between gap-2 bg-surface-subtle/70">
              <div class="flex items-center gap-2">
                <h4 class="text-xs font-semibold text-foreground">网盘专属调优参数</h4>
                <span class="hidden sm:inline text-[11px] text-muted-foreground">按当前 Remote 类型自动匹配，独立保存</span>
              </div>
              <div class="flex flex-wrap items-center gap-1.5">
                <Button
                  v-for="pt in PROVIDER_TABS"
                  :key="pt.value"
                  variant="outline"
                  size="xs"
                  :aria-pressed="providerTab === pt.value"
                  :class="[
                    'h-6 gap-1.5 px-2.5 rounded-full text-[11px] font-medium shadow-none hover:shadow-none',
                    providerTab === pt.value
                      ? 'border-brand/40 bg-brand/10 dark:bg-brand/15 text-brand hover:border-brand/40 hover:bg-brand/10 dark:hover:bg-brand/15 hover:text-brand'
                      : 'border-border bg-card text-muted-foreground hover:text-foreground',
                  ]"
                  @click="providerTab = pt.value"
                >
                  <span>{{ pt.label }}</span>
                  <span
                    v-if="(pt.value === 'pikpak' && activeRemoteType === 'pikpak') || (pt.value === 'drive' && activeRemoteType === 'drive')"
                    class="rounded-full bg-emerald-500/15 px-1.5 text-[10px] text-emerald-700 dark:text-emerald-300"
                  >
                    当前生效
                  </span>
                </Button>
              </div>
            </div>

            <!-- PikPak -->
            <div v-if="providerTab === 'pikpak'" class="p-3.5 space-y-2.5">
              <div class="grid grid-cols-1 md:grid-cols-2 gap-3.5 items-end">
                <div class="space-y-1">
                  <label class="text-xs font-medium text-foreground">PikPak 分块并发（--pikpak-upload-concurrency）</label>
                  <Input v-model="form.rclone_pikpak_upload_concurrency" type="number" :min="1" :max="32" />
                </div>
                <div class="flex flex-col sm:flex-row sm:items-end gap-2">
                  <div class="space-y-1 flex-1 min-w-0">
                    <label class="text-xs font-medium text-foreground">PikPak 分块大小（--pikpak-chunk-size）</label>
                    <Input v-model="form.rclone_pikpak_chunk_size" placeholder="16Mi" />
                  </div>
                  <div class="flex items-center gap-1 pb-0.5 shrink-0">
                    <Button
                      v-for="sz in ['16Mi', '32Mi', '64Mi']"
                      :key="sz"
                      variant="outline"
                      size="xs"
                      :aria-pressed="form.rclone_pikpak_chunk_size === sz"
                      :class="[
                        'h-9 px-2.5 rounded-lg font-mono text-[11px] shadow-none hover:shadow-none',
                        form.rclone_pikpak_chunk_size === sz
                          ? 'border-brand/40 bg-brand/10 dark:bg-brand/15 text-brand hover:border-brand/40 hover:bg-brand/10 dark:hover:bg-brand/15 hover:text-brand'
                          : 'border-border bg-card text-muted-foreground hover:text-foreground',
                      ]"
                      @click="form.rclone_pikpak_chunk_size = sz"
                    >
                      {{ sz }}
                    </Button>
                  </div>
                </div>
              </div>
              <p class="text-[11px] text-muted-foreground">
                建议策略：多 Worker + 小分块（千兆 / 4G 内存推荐 <span class="font-mono text-foreground">Worker = 6</span>、<span class="font-mono text-foreground">分块并发 = 3</span>、<span class="font-mono text-foreground">16Mi</span>）。
              </p>
            </div>

            <!-- Google Drive -->
            <div v-else-if="providerTab === 'drive'" class="p-3.5 space-y-2.5">
              <div class="grid grid-cols-1 md:grid-cols-2 gap-3.5 items-end">
                <div class="flex flex-col sm:flex-row sm:items-end gap-2">
                  <div class="space-y-1 flex-1 min-w-0">
                    <label class="text-xs font-medium text-foreground">GD 分块大小（--drive-chunk-size，2 的幂）</label>
                    <Input v-model="form.rclone_drive_chunk_size" placeholder="64Mi" />
                  </div>
                  <div class="flex items-center gap-1 pb-0.5 shrink-0">
                    <Button
                      v-for="sz in ['32Mi', '64Mi', '128Mi']"
                      :key="sz"
                      variant="outline"
                      size="xs"
                      :aria-pressed="form.rclone_drive_chunk_size === sz"
                      :class="[
                        'h-9 px-2.5 rounded-lg font-mono text-[11px] shadow-none hover:shadow-none',
                        form.rclone_drive_chunk_size === sz
                          ? 'border-brand/40 bg-brand/10 dark:bg-brand/15 text-brand hover:border-brand/40 hover:bg-brand/10 dark:hover:bg-brand/15 hover:text-brand'
                          : 'border-border bg-card text-muted-foreground hover:text-foreground',
                      ]"
                      @click="form.rclone_drive_chunk_size = sz"
                    >
                      {{ sz }}
                    </Button>
                  </div>
                </div>
                <label
                  class="inline-flex items-center gap-2 h-9 text-xs text-foreground/85 cursor-pointer"
                  title="Google Drive 单号每日 750GB 上传上限触发时立即终止本次上传并保留本地文件，避免干等超时"
                >
                  <Switch v-model="form.rclone_drive_stop_on_upload_limit" />
                  <span>达每日 750GB 上限时立即停止重试</span>
                </label>
              </div>
              <p class="text-[11px] text-muted-foreground">
                建议策略：少 Worker + 大分块（切到 GD 时建议上方 <span class="font-mono text-foreground">Worker = 2~3</span>、分块 <span class="font-mono text-foreground">64Mi / 128Mi</span>，建议自建 OAuth Client ID）。
              </p>
            </div>

            <!-- Other / Custom -->
            <div v-else class="p-3.5 space-y-2">
              <div class="space-y-1">
                <label class="text-xs font-medium text-foreground">附加 rclone copy 调优参数（任意网盘生效）</label>
                <Input
                  v-model="form.rclone_extra_flags"
                  placeholder="如 --onedrive-chunk-size=60M 或 --s3-chunk-size=32Mi --s3-upload-concurrency=4"
                  class="font-mono text-xs"
                />
              </div>
              <p class="text-[11px] text-muted-foreground">
                接入 OneDrive / S3 / R2 / WebDAV / Alist 等网盘时可直接填写官方调优参数（仅允许安全 <span class="font-mono">--*</span> 选项，自动过滤危险参数）。
              </p>
            </div>
          </div>
        </div>
      </Card>

      <!-- Tab 4: Schedule, Proxy & ServerChan -->
      <Card v-if="activeTab === 'notify'" class="divide-y divide-border">
        <div class="p-4 sm:p-6 space-y-5">
          <div>
            <h3 class="text-sm font-semibold text-foreground">追更调度与网络代理</h3>
            <p class="text-xs text-muted-foreground mt-0.5">
              控制每日定时追更时间、扫描深度与出口代理；「智能衔接」在接上历史记录后即停，「全量翻页」会扫描全部列表分页以捕获重新公开的旧视频
            </p>
          </div>

          <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div class="space-y-1.5">
              <label class="text-xs font-medium text-foreground">每日定时追更时间</label>
              <Input v-model="form.schedule_time" type="time" />
            </div>
            <div class="space-y-1.5">
              <label
                class="text-xs font-medium text-foreground"
                title="智能衔接：默认从首页扫描并在衔接已知记录后停止；全量翻页：每次翻完目标全部列表页，彻底不漏重新上架/公开的老视频（已知视频会自动跳过，不重复解析详情）"
              >
                追更扫描模式
              </label>
              <Select v-model="form.update_scan_mode" :options="UPDATE_SCAN_MODE_OPTIONS" />
            </div>
            <div class="space-y-1.5">
              <label
                class="text-xs font-medium text-foreground"
                title="全量追更与仅检查更新时同时扫描的博主数，建议 1~3，过大易触发 403 限流"
              >
                追更扫描目标并发（建议 1~3）
              </label>
              <Input v-model="form.update_target_concurrency" type="number" :min="1" :max="6" />
            </div>
            <div class="space-y-1.5">
              <label class="text-xs font-medium text-foreground">网络代理 URL（可选）</label>
              <Input v-model="form.proxy_url" placeholder="http://127.0.0.1:7890" />
            </div>
          </div>

          <div class="flex flex-wrap gap-6">
            <label class="inline-flex items-center gap-2 text-xs text-foreground/85 cursor-pointer">
              <Switch v-model="form.schedule_enabled" />
              <span>启用每日定时追更</span>
            </label>
            <label class="inline-flex items-center gap-2 text-xs text-foreground/85 cursor-pointer">
              <Switch v-model="form.notify_if_no_updates" />
              <span>无更新时也发通知</span>
            </label>
          </div>
        </div>

        <div class="p-4 sm:p-6 space-y-4">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div class="flex items-center gap-3">
              <Switch v-model="form.serverchan_enabled" />
              <div>
                <div class="text-sm font-semibold text-foreground">ServerChan 微信推送通知</div>
                <div class="text-xs text-muted-foreground mt-0.5">每次自动追更结束后推送新增与上传汇总</div>
              </div>
            </div>
            <Button variant="outline" size="sm" :disabled="busyTool !== ''" @click="handleTestNotify">
              <Send class="w-3.5 h-3.5" />
              发送测试通知
            </Button>
          </div>

          <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div class="space-y-1.5">
              <label class="text-xs font-medium text-foreground">ServerChan SendKey（已脱敏）</label>
              <Input v-model="form.serverchan_send_key" type="password" placeholder="SCT..." />
            </div>
            <div class="space-y-1.5">
              <label class="text-xs font-medium text-foreground">ServerChan API URL</label>
              <Input v-model="form.serverchan_api_url" />
            </div>
          </div>
        </div>
      </Card>

      <!-- Tab 5: Hot-updatable CSS Selectors -->
      <Card v-if="activeTab === 'selectors' && form.selectors" class="divide-y divide-border">
        <div class="p-4 sm:p-6 flex flex-wrap items-center justify-between gap-3 sm:gap-4">
          <div>
            <h3 class="text-sm font-semibold text-foreground">页面解析 CSS 选择器</h3>
            <p class="text-xs text-muted-foreground mt-0.5">
              当目标站点改版 DOM 结构时，可在此热更新选择器规则而无需重新构建镜像；多项以英文逗号分隔
            </p>
          </div>
          <Button variant="outline" size="sm" @click="resetSelectorsToDefault">
            <RotateCcw class="w-3.5 h-3.5" />
            恢复默认选择器
          </Button>
        </div>

        <div class="p-4 sm:p-6 grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div v-for="field in SELECTOR_FIELDS" :key="field.key" class="space-y-1.5">
            <div class="flex flex-wrap items-center justify-between gap-x-2 gap-y-0.5">
              <label class="text-xs font-medium text-foreground">{{ field.label }}</label>
              <span class="font-mono text-[10px] text-muted-foreground">{{ field.key }}</span>
            </div>
            <Input v-model="form.selectors[field.key]" class="font-mono text-xs" />
          </div>
        </div>
      </Card>
    </template>
  </div>
</template>
