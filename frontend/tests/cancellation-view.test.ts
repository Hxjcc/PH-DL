import assert from 'node:assert/strict'
import { afterEach, test, vi } from 'vitest'
import { createSSRApp, ref } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import JobCard from '../src/components/JobCard.vue'
import DownloadView from '../src/views/DownloadView.vue'
import downloadViewSource from '../src/views/DownloadView.vue?raw'
import { useQuickScrollReady } from '../src/composables/useQuickScrollReady'
import { useAppStore } from '../src/stores/app'
import { jobStatusMeta } from '../src/utils/format'
import { jobItemErrorClass } from '../src/utils/jobMeta'
import { makeJob, makeJobItem } from './fixtures'

afterEach(() => vi.unstubAllGlobals())

test('a cancelling job renders an amber stopping pill, disabled action and quiet telemetry', async () => {
  const pinia = createPinia()
  const store = useAppStore(pinia)
  const job = makeJob(19, 'running', {})
  store.markJobCancelling(job.id)
  store.liveProgress[1] = {
    job_id: job.id, item_id: 1, bytes_downloaded: 100, bytes_total: 500, speed_bps: 4000, phase: 'downloading',
  }
  const html = await renderToString(createSSRApp(JobCard, { job }).use(pinia))
  assert.match(html, /bg-amber-500 status-dot-glow-amber animate-pulse/)
  assert.match(html, />\s*停止中<\/span>/)
  assert.match(html, /<button[^>]*\bdisabled[^>]*>(?:(?!<\/button>)[\s\S])*停止中\.\.\.(?:<!--[\s\S]*?-->)?<\/button>/)
  assert.match(html, /正在安全停止任务并保留断点与本地文件/)
  assert.doesNotMatch(html, /实时总下载速率|实时总上传速率/)
  assert.equal(jobStatusMeta('cancelling').label, '停止中')
})

test('job detail renders routine cancellation notes neutrally and real errors in warning colors', async () => {
  const pinia = createPinia()
  const store = useAppStore(pinia)
  const job = { ...makeJob(19, 'cancelled', {}), total_items: 4 }
  const items = [
    makeJobItem(1, job.id, { status: 'cancelled', error_message: '已取消（断点已保留）' }),
    makeJobItem(2, job.id, { status: 'cancelled', error_message: '上传已取消，保留本地文件' }),
    makeJobItem(3, job.id, { status: 'failed', error_message: '文件校验失败' }),
    makeJobItem(4, job.id, { status: 'cancelled', error_message: '因触发网盘限额已暂停，保留本地文件' }),
  ]
  store.jobItems[job.id] = items
  const html = await renderToString(createSSRApp(JobCard, { job, defaultExpanded: true }).use(pinia))
  assert.match(html, /<div class="[^"]*text-muted-foreground[^"]*"[^>]*>已取消（断点已保留）<\/div>/)
  assert.match(html, /<div class="[^"]*text-muted-foreground[^"]*"[^>]*>上传已取消，保留本地文件<\/div>/)
  assert.match(html, /<div class="[^"]*text-red-600[^"]*"[^>]*>文件校验失败<\/div>/)
  assert.match(html, /<div class="[^"]*text-amber-700[^"]*"[^>]*>因触发网盘限额已暂停，保留本地文件<\/div>/)
  assert.match(jobItemErrorClass({ status: 'failed', error_message: '上传已取消，保留本地文件' }), /text-red/)
})

test('the quick-fill arrows initially render without a transition class', async () => {
  const pinia = createPinia()
  const store = useAppStore(pinia)
  store.targetsCache = [{
    id: 1, site: 'pornhub', username: 'fixture', display_name: 'Fixture', target_type: 'model',
    avatar_url: '', enabled: true, preferred_quality: '', created_at: '', last_checked_at: null,
    last_new_count: 0, last_error: '',
  }]
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/', component: DownloadView }, { path: '/jobs', name: 'jobs', component: JobCard }],
  })
  await router.push('/')
  const html = await renderToString(createSSRApp(DownloadView).use(pinia).use(router))
  assert.match(html, /quick-edge-trigger quick-edge-right quick-edge-hidden/)
  assert.doesNotMatch(html, /quick-edge-animated/)
  assert.doesNotMatch(downloadViewSource.match(/\.quick-edge-trigger\s*\{[^}]*\}/)?.[0] || '', /transition:/)
  assert.match(downloadViewSource, /\.quick-edge-trigger\.quick-edge-animated\s*\{\s*transition: opacity/)
})

test('quick-fill measures immediately, waits for fonts and two frames, and disarms on deactivation', async () => {
  let resolveFonts!: () => void
  const fontsReady = new Promise<void>((resolve) => { resolveFonts = resolve })
  const frames = new Map<number, FrameRequestCallback>()
  let lastId = 0
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    frames.set(++lastId, callback)
    return lastId
  })
  vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id))
  const fonts = { status: 'loading', ready: fontsReady }
  vi.stubGlobal('document', { fonts })
  const measure = vi.fn()
  const { quickScrollReady, measureQuickScroll, resetQuickScrollReady } = useQuickScrollReady(
    ref({} as HTMLElement), measure,
  )
  function frame() {
    const callbacks = [...frames.values()]
    frames.clear()
    for (const callback of callbacks) callback(0)
  }

  measureQuickScroll()
  assert.equal(measure.mock.calls.length, 1, 'initial layout is measured synchronously')
  assert.equal(quickScrollReady.value, false)
  assert.equal(frames.size, 0)
  resetQuickScrollReady()
  resolveFonts()
  await fontsReady
  assert.equal(frames.size, 0, 'old font callbacks cannot arm the inactive view')

  fonts.status = 'loaded'
  measureQuickScroll()
  frame()
  assert.equal(measure.mock.calls.length, 3)
  assert.equal(quickScrollReady.value, false)
  frame()
  assert.equal(quickScrollReady.value, true)
  measureQuickScroll()
  frame()
  resetQuickScrollReady()
  assert.equal(frames.size, 0)
  assert.equal(quickScrollReady.value, false)
})
