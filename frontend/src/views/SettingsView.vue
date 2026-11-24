<script setup>
import { onActivated, ref, watch } from 'vue'
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
} from 'lucide-vue-next'
import { api } from '../api/client'
import { useAppStore } from '../stores/app'
import Card from '../components/ui/Card.vue'
import Button from '../components/ui/Button.vue'
import Badge from '../components/ui/Badge.vue'
import Input from '../components/ui/Input.vue'
import Textarea from '../components/ui/Textarea.vue'
import Switch from '../components/ui/Switch.vue'
import Tabs from '../components/ui/Tabs.vue'

const DEFAULT_SELECTORS = Object.freeze({
  list_page_main_containers:
    'ul.videos#videoSearchResult, div#videoCategory ul.videos, ul#searchResults.videos.video-listing.clear, ul.videos[data-type="pornstar"], ul.videos.row-5-thumbs#mostRecentVideosSection, ul.videos.section-listing, ul.videos.video-listing.clear.profileContent, ul.videos.video-listing.clear.playlistContent, div.container > ul.videos, ul#moreData.videos, ul#likedVideosSection.videos',
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

function cloneSettings(src) {
  if (!src) return null
  return {
    ...src,
    selectors: { ...DEFAULT_SELECTORS, ...(src.selectors || {}) },
  }
}

const store = useAppStore()
const activeTab = ref('cookie')
const loading = ref(!store.settingsCache)
const saving = ref(false)
const busyTool = ref('')
const form = ref(cloneSettings(store.settingsCache))
const pasteCookie = ref('')

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

async function loadSettings() {
  if (!form.value) loading.value = true
  try {
    const settings = await store.refreshSettings()
    if (settings && !saving.value) {
      form.value = cloneSettings(settings)
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
  busyTool.value = 'cookie_refresh'
  try {
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
  <div class="space-y-6">
    <!-- Page Header -->
    <div class="flex flex-wrap items-center justify-between gap-4">
      <div class="flex items-center gap-3.5">
        <div
          class="w-10 h-10 rounded-xl bg-brand/10 text-brand flex items-center justify-center shrink-0 ring-1 ring-brand/15"
        >
          <Settings class="w-5 h-5" />
        </div>
        <div>
          <h1 class="text-xl font-bold tracking-tight text-foreground">系统设置</h1>
          <p class="text-xs sm:text-sm text-muted-foreground mt-0.5">
            管理登录 Cookie、下载并发与磁盘背压、rclone 云端上传及通知策略
          </p>
        </div>
      </div>
      <Button size="sm" :disabled="saving || !form" @click="saveAll">
        <Loader2 v-if="saving" class="w-3.5 h-3.5 animate-spin" />
        <Save v-else class="w-3.5 h-3.5" />
        {{ saving ? '保存中...' : '保存全部设置' }}
      </Button>
    </div>

    <div v-if="loading || !form" class="py-16 flex items-center justify-center gap-2 text-xs text-muted-foreground">
      <Loader2 class="w-4 h-4 animate-spin" />
      <span>加载配置中...</span>
    </div>

    <template v-else>
      <div class="overflow-x-auto pb-1">
        <Tabs v-model="activeTab" :items="tabs" />
      </div>

      <!-- Tab 1: Cookie & Account -->
      <Card v-if="activeTab === 'cookie'" class="divide-y divide-border">
        <div class="p-6 space-y-4">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div class="space-y-1">
              <div class="flex items-center gap-2">
                <h3 class="text-sm font-semibold text-foreground">浏览器 Cookie</h3>
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
                推荐在 VPS 上直接粘贴从浏览器复制的 Cookie 请求头，稳定且免验证码
              </p>
            </div>
            <div class="flex flex-wrap gap-2">
              <Button variant="outline" size="xs" :disabled="busyTool !== ''" @click="handleTestCookie">
                <CheckCircle2 class="w-3.5 h-3.5" />
                检测有效性
              </Button>
              <Button variant="outline" size="xs" :disabled="busyTool !== ''" @click="handleRefreshCookie">
                <RefreshCw
                  :class="['w-3.5 h-3.5', busyTool === 'cookie_refresh' ? 'animate-spin' : '']"
                />
                自动登录刷新
              </Button>
            </div>
          </div>

          <Textarea
            v-model="pasteCookie"
            :rows="3"
            placeholder="粘贴新的 Cookie 字符串..."
            class="font-mono text-xs"
          />
          <div class="flex justify-end">
            <Button
              size="sm"
              :disabled="!pasteCookie.trim() || busyTool !== ''"
              @click="handleImportCookie"
            >
              保存粘贴的 Cookie 并测活
            </Button>
          </div>
        </div>

        <div class="p-6 space-y-4">
          <div>
            <h3 class="text-sm font-semibold text-foreground">账号凭据（可选）</h3>
            <p class="text-xs text-muted-foreground mt-0.5">
              仅用于拉取订阅/收藏或尝试自动刷新 Cookie，敏感字段已脱敏保护
            </p>
          </div>
          <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div class="space-y-1.5">
              <label class="text-xs font-medium text-foreground">账号 / 邮箱</label>
              <Input v-model="form.ph_username" placeholder="可选" />
            </div>
            <div class="space-y-1.5">
              <label class="text-xs font-medium text-foreground">密码（已脱敏）</label>
              <Input v-model="form.ph_password" type="password" />
            </div>
            <div class="space-y-1.5">
              <label class="text-xs font-medium text-foreground">已登录用户名</label>
              <Input v-model="form.ph_logged_in_username" placeholder="拉取订阅列表用" />
            </div>
          </div>
        </div>
      </Card>

      <!-- Tab 2: Download & Backpressure -->
      <Card v-if="activeTab === 'download'" class="p-6 space-y-5">
        <div>
          <h3 class="text-sm font-semibold text-foreground">并发与磁盘背压控制</h3>
          <p class="text-xs text-muted-foreground mt-0.5">
            当待上传文件堆积或剩余磁盘空间低于阈值时，引擎会自动暂停下载等待 rclone 释放空间
          </p>
        </div>

        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
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
            <label class="text-xs font-medium text-foreground">单批触发上传文件数</label>
            <Input v-model="form.upload_batch_size" type="number" :min="1" :max="100" />
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
        <div class="p-6 flex flex-wrap items-center justify-between gap-4">
          <div class="flex items-center gap-3">
            <Switch v-model="form.uploader_enabled" />
            <div>
              <div class="text-sm font-semibold text-foreground">启用 rclone 自动上传并删除本地文件</div>
              <div class="text-xs text-muted-foreground mt-0.5">
                边下边传：确认网盘文件与本地内容一致后，再清理本地副本
              </div>
            </div>
          </div>

          <Button variant="outline" size="sm" :disabled="busyTool !== ''" @click="handleTestRclone">
            <PlugZap class="w-3.5 h-3.5" />
            测试 rclone 连接
          </Button>
        </div>

        <div class="p-6 space-y-5">
          <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            <div class="space-y-1.5">
              <label class="text-xs font-medium text-foreground">Remote 名称</label>
              <Input v-model="form.rclone_remote" placeholder="pikpak" />
            </div>
            <div class="space-y-1.5">
              <label class="text-xs font-medium text-foreground">远端基础目录</label>
              <Input v-model="form.rclone_remote_base_dir" placeholder="Pornhub" />
            </div>
            <div class="space-y-1.5">
              <label class="text-xs font-medium text-foreground">rclone 命令路径</label>
              <Input v-model="form.rclone_bin" />
            </div>
            <div class="space-y-1.5">
              <label class="text-xs font-medium text-foreground">上传并发 (transfers)</label>
              <Input v-model="form.rclone_transfers" type="number" :min="1" :max="16" />
            </div>
            <div class="space-y-1.5">
              <label class="text-xs font-medium text-foreground">检查并发 (checkers)</label>
              <Input v-model="form.rclone_checkers" type="number" :min="1" :max="32" />
            </div>
            <div class="space-y-1.5">
              <label class="text-xs font-medium text-foreground">PikPak 分块并发（仅 PikPak 生效）</label>
              <Input v-model="form.rclone_pikpak_upload_concurrency" type="number" :min="1" :max="32" />
            </div>
            <div class="space-y-1.5">
              <label class="text-xs font-medium text-foreground">PikPak 分块大小（仅 PikPak 生效）</label>
              <Input v-model="form.rclone_pikpak_chunk_size" placeholder="32Mi" />
            </div>
            <div class="space-y-1.5">
              <label class="text-xs font-medium text-foreground">rclone 单次执行超时（秒）</label>
              <Input v-model="form.rclone_timeout_seconds" type="number" :min="30" :max="86400" />
            </div>
            <div class="space-y-1.5">
              <label class="text-xs font-medium text-foreground">rclone 日志级别</label>
              <Input v-model="form.rclone_log_level" placeholder="ERROR" />
            </div>
            <div class="space-y-1.5">
              <label class="text-xs font-medium text-foreground">rclone TPS 限制（0 不限制）</label>
              <div class="grid grid-cols-2 gap-2">
                <Input v-model="form.rclone_tpslimit" type="number" :min="0" :max="100" placeholder="0" />
                <Input v-model="form.rclone_tpslimit_burst" type="number" :min="0" :max="100" placeholder="突发" />
              </div>
            </div>
          </div>

          <div class="flex flex-wrap gap-6 pt-1">
            <label class="inline-flex items-center gap-2 text-xs text-foreground/85 cursor-pointer">
              <Switch v-model="form.rclone_ignore_checksum" />
              <span>上传时跳过 rclone 自带的校验和检查</span>
            </label>
            <label class="inline-flex items-center gap-2 text-xs text-foreground/85 cursor-pointer">
              <Switch v-model="form.rclone_ignore_size" />
              <span>上传时跳过 rclone 自带的大小检查</span>
            </label>
          </div>
        </div>
      </Card>

      <!-- Tab 4: Schedule, Proxy & ServerChan -->
      <Card v-if="activeTab === 'notify'" class="divide-y divide-border">
        <div class="p-6 space-y-5">
          <div>
            <h3 class="text-sm font-semibold text-foreground">追更调度与网络代理</h3>
            <p class="text-xs text-muted-foreground mt-0.5">
              控制每日定时追更时间与出口代理；追更内置智能增量补扫（从首页检查，未衔接历史记录时自动向后翻页，异常页下次自动补齐）
            </p>
          </div>

          <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div class="space-y-1.5">
              <label class="text-xs font-medium text-foreground">每日定时追更时间</label>
              <Input v-model="form.schedule_time" type="time" />
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

        <div class="p-6 space-y-4">
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
        <div class="p-6 flex flex-wrap items-center justify-between gap-4">
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

        <div class="p-6 grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div v-for="field in SELECTOR_FIELDS" :key="field.key" class="space-y-1.5">
            <div class="flex items-center justify-between gap-2">
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
