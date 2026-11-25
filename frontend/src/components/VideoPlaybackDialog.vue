<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import { CircleAlert, ExternalLink, RefreshCw } from 'lucide-vue-next'
import { api } from '../api/client'
import Button from './ui/Button.vue'
import Dialog from './ui/Dialog.vue'
import Select from './ui/Select.vue'
import Spinner from './ui/Spinner.vue'

const props = defineProps({ video: { type: Object, required: true } })
const emit = defineEmits(['close'])

const mountPoint = ref(null)
const fullscreenTarget = shallowRef(null)
const sources = shallowRef([])
const activeIndex = ref(0)
const phase = ref('resolving')
const error = ref('')
const busy = computed(() => phase.value === 'resolving' || phase.value === 'connecting')
const buffering = ref(false)
const showLoading = computed(() => busy.value || buffering.value)
const qualityOptions = computed(() =>
  sources.value.map((source, index) => ({ label: source.quality || '原始画质', value: index }))
)
const byline = computed(() => props.video.uploader_display_name || props.video.uploader || '')
const pageUrl = computed(() => {
  try {
    const url = new URL(props.video.video_page_url)
    return ['https:', 'http:'].includes(url.protocol) ? url.href : ''
  } catch {
    return ''
  }
})

let player = null
let Player = null
let controller = null
let timer = null
let bufferTimer = null
let attempt = 0
let closed = false
let resume = { time: 0, playing: true, rate: 1, muted: false, volume: 0.8 }

function snapshot() {
  const media = player?.video
  if (phase.value !== 'ready' || !media) return resume
  return {
    time: media.currentTime || 0,
    playing: !media.paused,
    rate: media.playbackRate,
    muted: media.muted,
    volume: media.volume,
  }
}

function releasePlayer() {
  clearTimeout(timer)
  timer = null
  stopBuffering()
  fullscreenTarget.value = null
  const instance = player
  player = null
  instance?.destroy()
}

function stopBuffering() {
  clearTimeout(bufferTimer)
  bufferTimer = null
  buffering.value = false
}

function showError(message) {
  attempt++
  releasePlayer()
  error.value = message
  phase.value = 'error'
}

function connect(index, previous) {
  const source = sources.value[index]
  if (!source || !mountPoint.value) return

  const id = ++attempt
  resume = previous
  releasePlayer()
  activeIndex.value = index
  phase.value = 'connecting'
  error.value = ''

  const isCurrent = () => id === attempt && !closed
  function failed() {
    if (!isCurrent()) return
    resume = snapshot()
    showError('视频暂时无法加载，请重试。')
  }

  timer = setTimeout(failed, 35_000)
  try {
    const instance = new Player({
      container: mountPoint.value,
      url: source.relay_url,
      type: 'mp4',
      lang: 'zh-cn',
      theme: '#e4e4e7',
      volume: previous.volume,
      muted: previous.muted,
      playbackRate: true,
      setting: true,
      fullscreen: true,
      pip: true,
      hotkey: false,
      cssVar: { '--art-progress-height': '5px', '--art-control-height': '42px' },
      moreVideoAttr: { playsInline: true, preload: 'auto', 'aria-label': props.video.title || '视频播放器' },
    })
    player = instance
    const media = instance.video
    function onBuffering() {
      if (!isCurrent() || phase.value !== 'ready' || bufferTimer !== null || buffering.value) return
      bufferTimer = setTimeout(() => {
        bufferTimer = null
        if (isCurrent() && (media.seeking || (!media.paused && media.readyState < HTMLMediaElement.HAVE_FUTURE_DATA))) {
          buffering.value = true
        }
      }, 150)
    }
    function onPlayable() {
      if (isCurrent() && !media.seeking) stopBuffering()
    }
    instance.once('video:loadedmetadata', () => {
      if (!isCurrent()) return
      if (previous.time > 0 && Number.isFinite(media.duration)) {
        media.currentTime = Math.min(previous.time, Math.max(0, media.duration - 0.1))
      }
      media.playbackRate = previous.rate
    })
    instance.once('video:canplay', () => {
      if (!isCurrent()) return
      clearTimeout(timer)
      timer = null
      phase.value = 'ready'
      // The browser may require another gesture after the source lookup.
      if (previous.playing) instance.play().catch(() => {})
    })
    instance.on('video:waiting', onBuffering)
    instance.on('video:seeking', onBuffering)
    for (const event of ['canplay', 'playing', 'seeked', 'pause', 'ended']) {
      instance.on(`video:${event}`, onPlayable)
    }
    instance.on('video:error', () => queueMicrotask(failed))
  } catch {
    if (isCurrent()) showError('播放器初始化失败，请重试。')
  }
}

function syncFullscreen() {
  const element = document.fullscreenElement || document.webkitFullscreenElement
  fullscreenTarget.value = element && mountPoint.value?.contains(element) && element.tagName !== 'VIDEO' ? element : null
}

async function loadSources() {
  const previous = snapshot()
  const previousQuality = sources.value[activeIndex.value]?.quality
  resume = previous
  attempt++
  releasePlayer()
  controller?.abort()
  const request = new AbortController()
  controller = request
  phase.value = 'resolving'
  error.value = ''
  try {
    const [response, module] = await Promise.all([
      api.videoPlayback(props.video.viewkey, request.signal),
      import('artplayer'),
    ])
    if (closed || request.signal.aborted) return
    Player = module.default
    // The dialog handles retry by getting a fresh playback URL.
    Player.RECONNECT_TIME_MAX = 0
    Player.LOG_VERSION = false
    sources.value = (response?.sources || []).filter((source) => source.relay_url)
    if (!sources.value.length) {
      showError('当前没有可播放的视频。')
      return
    }
    const selected = sources.value.findIndex((source) => source.quality === previousQuality)
    await nextTick()
    if (!closed && !request.signal.aborted) connect(Math.max(0, selected), previous)
  } catch (err) {
    if (!closed && !request.signal.aborted) showError(err.message || '获取播放地址失败，请重试。')
  }
}

function changeQuality(value) {
  const index = Number(value)
  if (index !== activeIndex.value) connect(index, snapshot())
}

function handlePlayerKey(event) {
  const instance = player
  if (phase.value !== 'ready' || !instance || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return

  const media = instance.video
  switch (event.code) {
    case 'Space':
      if (media.paused) instance.play().catch(() => {})
      else instance.pause()
      break
    case 'ArrowLeft':
      media.currentTime = Math.max(0, media.currentTime - 5)
      break
    case 'ArrowRight':
      if (Number.isFinite(media.duration)) media.currentTime = Math.min(media.duration, media.currentTime + 5)
      break
    default:
      return
  }
  event.preventDefault()
}

onMounted(() => {
  document.addEventListener('fullscreenchange', syncFullscreen)
  document.addEventListener('webkitfullscreenchange', syncFullscreen)
  loadSources()
})
onBeforeUnmount(() => {
  document.removeEventListener('fullscreenchange', syncFullscreen)
  document.removeEventListener('webkitfullscreenchange', syncFullscreen)
  closed = true
  attempt++
  controller?.abort()
  releasePlayer()
})
</script>

<template>
  <Dialog
    :open="true"
    :title="video.title || video.viewkey"
    :description="byline ? `在线播放 · ${byline}` : '在线播放'"
    class="max-w-5xl max-h-[calc(100dvh-1.5rem)] overflow-y-auto p-4 sm:p-5"
    @close="emit('close')"
  >
    <div class="playback-frame">
      <div class="playback-stage relative overflow-hidden bg-black">
        <div
          ref="mountPoint"
          class="absolute inset-0 focus-visible:outline-2 focus-visible:-outline-offset-4 focus-visible:outline-zinc-200"
          :inert="phase !== 'ready'"
          tabindex="0"
          role="group"
          aria-label="视频播放器"
          aria-keyshortcuts="Space ArrowLeft ArrowRight"
          @keydown.self="handlePlayerKey"
        />
        <Teleport :to="fullscreenTarget || 'body'" :disabled="!fullscreenTarget">
          <div
            v-if="showLoading || error"
            :role="error ? 'alert' : 'status'"
            class="absolute inset-0 z-[71] grid place-items-center p-4 text-center text-zinc-100"
            :class="error || busy ? 'bg-zinc-950/90' : 'pointer-events-none'"
          >
            <div
              :class="[
                'flex max-w-sm flex-col items-center gap-2.5',
                !error && !busy
                  ? 'rounded-xl bg-zinc-950/75 px-4 py-3 backdrop-blur-xs shadow-lg ring-1 ring-white/10'
                  : '',
              ]"
            >
              <CircleAlert v-if="error" class="h-6 w-6 shrink-0 text-zinc-400" />
              <Spinner v-else class="h-6 w-6 shrink-0 text-zinc-200" />
              <p class="text-sm leading-5">{{ error || '正在加载视频…' }}</p>
              <Button v-if="error" size="sm" variant="secondary" @click="loadSources()">
                <RefreshCw class="h-3.5 w-3.5" />
                重试播放
              </Button>
            </div>
          </div>
        </Teleport>
      </div>
    </div>

    <div class="mt-3 flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
      <div class="inline-flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
        <Select
          v-if="sources.length > 1"
          :model-value="activeIndex"
          :options="qualityOptions"
          :disabled="phase !== 'ready'"
          aria-label="播放画质"
          class="h-8 w-24 text-xs tabular-nums"
          @update:model-value="changeQuality"
        />
        <span
          v-else-if="phase === 'ready' && sources[0]?.quality"
          class="rounded-full bg-secondary px-2 py-0.5 text-[11px] font-medium tabular-nums"
        >
          {{ sources[0].quality }}
        </span>
      </div>

      <div class="flex flex-wrap items-center gap-1.5">
        <Button
          v-if="!error"
          variant="ghost" size="sm" :disabled="busy" class="text-muted-foreground"
          @click="loadSources()"
        >
          <RefreshCw class="h-3.5 w-3.5" />
          刷新地址
        </Button>
        <Button
          v-if="pageUrl"
          as="a" :href="pageUrl" target="_blank" rel="noopener noreferrer"
          variant="ghost" size="sm" class="text-muted-foreground"
        >
          <ExternalLink class="h-3.5 w-3.5" />
          原网页
        </Button>
      </div>
    </div>
  </Dialog>
</template>

<style scoped>
.playback-frame {
  position: relative;
  width: 100%;
  max-width: calc(66dvh * 16 / 9);
  margin-inline: auto;
  border-radius: 12px;
}

.playback-frame::after {
  content: '';
  position: absolute;
  inset: -2px;
  border-radius: 16px;
  border: 4px solid var(--card);
  box-shadow: inset 0 0 0 1px rgba(9, 9, 11, 0.08);
  pointer-events: none;
  z-index: 72;
}

:global(.dark) .playback-frame::after {
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.1);
}

.playback-frame:has(.art-fullscreen-web, .art-fullscreen, :fullscreen)::after {
  display: none;
}

.playback-stage {
  width: 100%;
  aspect-ratio: 16 / 9;
  border-radius: 13px;
}

.playback-stage :deep(.art-video-player .art-loading) {
  /* The dialog owns the loading indicator, including while ArtPlayer buffers. */
  display: none;
}
</style>
