import assert from 'node:assert/strict'
import { afterEach, test, vi } from 'vitest'
import { createPinia } from 'pinia'
import { api } from '../src/api/client'
import { EventStreamClient } from '../src/api/ws'
import { useAppStore } from '../src/stores/app'
import { SESSION_CACHE_KEY, SESSION_CACHE_MAX_CHARS, SESSION_LOGOUT_KEY } from '../src/stores/sessionCache'
import { resolveJobVisual } from '../src/utils/jobMeta'
import type { JobDetailResponse, JobStatus, OverviewResponse, RuntimeSettings, TargetRecord, VideoListResponse, WsMessage } from '../src/types/api'
import { makeJob, makeJobItem } from './fixtures'

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function job(id: number, status: JobStatus, revision: number, resumeCount = 0) {
  return { ...makeJob(id, status, { resume_count: resumeCount }), revision }
}

function mockSessionStorage(pathname = '/', initial: string | null = null, local = new Map<string, string>()) {
  let raw = initial
  let writes = 0
  const browser = Object.assign(new EventTarget(), {
    location: { pathname },
    sessionStorage: {
      getItem: (key: string) => key === SESSION_CACHE_KEY ? raw : null,
      removeItem: (key: string) => { if (key === SESSION_CACHE_KEY) raw = null },
      setItem: (key: string, value: string) => {
        assert.equal(key, SESSION_CACHE_KEY)
        raw = value
        writes++
      },
    },
    localStorage: {
      getItem: (key: string) => local.get(key) ?? null,
      setItem: (key: string, value: string) => local.set(key, value),
    },
  })
  const listener = vi.spyOn(browser, 'addEventListener')
  vi.stubGlobal('window', browser)
  return {
    get raw() { return raw },
    get writes() { return writes },
    pagehide: () => browser.dispatchEvent(new Event('pagehide')),
    listener,
    local,
  }
}

const stats = { total: 1, uploaded: 1, downloaded: 0, baseline: 0, published_7d: 0, baseline_missing_metadata: 0 }
const accountCookie = { configured: true, username: 'member', valid: true, message: 'Cookie valid' }
const overview: OverviewResponse = {
  running: false, current_job: null, queued_count: 0,
  account: { username: 'member', avatar_url: '/api/settings/avatar?v=version1' },
  cookie: accountCookie, xh_cookie: accountCookie,
  schedule: { enabled: true, time: '04:00', scan_mode: 'catch_up', next_run_at: null },
  uploader_enabled: true,
  event_loop: { current_ms: 0, avg_ms: 0, max_ms: 0, warning_count: 0, warning_threshold_ms: 250 },
  targets_total: 1, targets_enabled: 1, videos: stats, temp_files_count: 1,
  disk: { free_bytes: 1024, total_bytes: 2048 },
}
const target: TargetRecord = {
  id: 42, site: 'xhamster', username: 'fragile-tempt', target_type: 'creator', display_name: 'Fragile Tempt',
  avatar_url: 'https://cdn.example/fragile-tempt.jpg', enabled: true, preferred_quality: '1080', created_at: '',
  last_checked_at: null, last_new_count: 0, last_error: 'target-error-secret',
  today_new_date: '2026-11-27', today_new_count: 2, video_total_count: 7, video_saved_count: 5, video_baseline_count: 2,
}
const history: VideoListResponse = {
  items: [{
    viewkey: 'ph123', site: 'pornhub', title: 'Saved video', uploader: 'member', target_type: 'model',
    video_page_url: 'https://www.pornhub.com/view_video.php?viewkey=ph123', quality: '1080', status: 'uploaded',
    file_size: 1024, duration_seconds: 5, published_at: '', created_at: '', downloaded_at: null, uploaded_at: null,
    local_path: 'private-local-path', remote_path: 'private-remote-path', upload_requested: 1,
  }],
  total: 1, page: 1, page_size: 30, stats,
  facets: { sites: ['pornhub'], uploaders: ['member'], uploader_labels: { member: 'Member' }, qualities: ['1080'] },
}
const settings = {
  ph_logged_in_username: 'member', ph_cookie: '********', has_cookie: true, account_avatar_version: 'version1',
  proxy_url: 'http://localhost:8080', serverchan_enabled: true, serverchan_send_key: '********',
  serverchan_api_url: 'https://sctapi.ftqq.com', rclone_remote: 'backup', rclone_extra_flags: '--bwlimit=5Mi',
  schedule_enabled: true, schedule_time: '04:00', update_scan_mode: 'catch_up',
} as RuntimeSettings
const tempFile = { name: 'clip.mp4.tmp', folder: 'creator', site: 'xhamster', viewkey: 'xh123', size_bytes: 2048, verified_checkpoint: true } as const
const remotes = [{ name: 'backup', type: 'drive' }]
const log = { id: 0, ts: '2026-11-27T12:00:00Z', level: 'INFO', job_id: null, message: 'Download complete' }

function seedRouteStore() {
  const store = useAppStore(createPinia())
  store.authenticated = true
  store.overview = overview
  store.overviewLoaded = true
  store.jobsLoaded = true
  store.jobsFetchedLimit = store.jobsLimit
  store.tempFilesLoaded = true
  store.tempFiles = [tempFile]
  store.setTargetsCache([target])
  store.setSettingsCache(settings)
  store.setRcloneRemotesCache(remotes)
  store.setHistoryCache(history)
  store._applyLogsSnapshot([log])
  return store
}

test('job revisions order repeated resumes and reject stale terminal snapshots', () => {
  const store = useAppStore(createPinia())
  try {
    for (const [status, revision, run] of [
      ['failed', 10, 0], ['queued', 11, 1], ['scanning', 12, 1], ['running', 13, 1],
      ['scanning', 14, 1], ['cancelled', 15, 1], ['queued', 16, 2], ['running', 17, 2],
    ] as const) {
      store._upsertJob(job(1, status, revision, run))
      assert.equal(store.jobs[0].status, status)
    }
    assert.equal(store._upsertJob(job(1, 'cancelled', 15, 1)), false)
    assert.equal(store.jobs[0].revision, 17)
  } finally {
    store._resetSessionState()
  }
})

test('job detail requests merge item events received while the snapshot was in flight', async () => {
  const store = useAppStore(createPinia())
  let resolve!: (detail: JobDetailResponse) => void
  let markStarted!: () => void
  const started = new Promise<void>((ready) => { markStarted = ready })
  const response = new Promise<JobDetailResponse>((fulfill) => { resolve = fulfill })
  const get = vi.spyOn(api, 'getJob').mockImplementation(() => {
    markStarted()
    return response
  })
  try {
    const first = store.loadJobDetail(1)
    const second = store.loadJobDetail(1)
    await started
    store._upsertItem(makeJobItem(5, 1, { status: 'done', revision: 3 }))
    store._upsertItem(makeJobItem(6, 1, { status: 'pending', revision: 1 }))
    resolve({ job: job(1, 'running', 5, 1), items: [makeJobItem(5, 1, { status: 'uploading', revision: 2 })] })
    await Promise.all([first, second])
    assert.equal(get.mock.calls.length, 1)
    assert.deepEqual(store.jobItems[1]?.map((row) => [row.id, row.status]), [[5, 'done'], [6, 'pending']])
  } finally {
    store._resetSessionState()
  }
})

test('open job cards keep their cached details until all their pins are released', async () => {
  const store = useAppStore(createPinia())
  vi.spyOn(api, 'getJob').mockImplementation(async (id) => ({ job: job(id, 'completed', 1), items: [makeJobItem(id, id)] }))
  const release1 = store.pinJobDetail(1)
  const release2 = store.pinJobDetail(1)
  try {
    for (let id = 1; id <= 10; id++) await store.loadJobDetail(id)
    release1()
    await store.loadJobDetail(11)
    assert.equal(store.jobItems[1]?.[0].id, 1)
    assert.equal(Object.keys(store.jobItems).length, 9)
    release2()
    assert.equal(store.jobItems[1], undefined)
    assert.equal(Object.keys(store.jobItems).length, 8)
  } finally {
    store._resetSessionState()
  }
})

test.each([1, 2])('discards session cache schema v%i', (v) => {
  const storage = mockSessionStorage('/', JSON.stringify({ v, ts: Date.now(), authenticated: true,
    settingsCache: { ph_password: 'test-secret' } }))
  const store = useAppStore(createPinia())
  assert.equal(store.hydratedFromCache, false)
  assert.equal(storage.raw, null)
})

test('session summaries only persist redacted credentials and allowlisted display fields', () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  const storage = mockSessionStorage()
  const store = seedRouteStore()
  try {
    const liveJob = { ...job(1, 'completed', 1), export_text: 'signed-media-url', params_json: 'raw-params',
      error_message: 'job-error-secret', params: { urls: ['raw-url'], resume_count: 1, keywords: 'private-query',
        profile_avatars: { member: 'https://cdn.example/avatar.jpg?token=signed-avatar' } } }
    store.jobs = [liveJob]
    store.overview = { ...overview, current_job: liveJob }
    store.setSettingsCache({ ...settings, ph_password: 'test-secret', ph_cookie: 'ph-cookie-secret',
      xh_password: 'xh-password-secret', xh_cookie: 'xh-cookie-secret', serverchan_send_key: 'serverchan-secret',
      yescaptcha_client_key: 'yescaptcha-secret', proxy_url: 'http://user:proxy-password@localhost:8080',
      serverchan_api_url: 'https://sctapi.ftqq.com?token=notify-url-secret', rclone_extra_flags: '--s3-access-key-id flags-secret',
      ...{ future_credential: 'unknown-settings-secret' } })
    store.setTargetsCache([{ ...target, avatar_url: 'https://user:avatar-password@cdn.example/pic.jpg' }])
    store.setHistoryCache({ ...history, items: [{ ...history.items[0],
      video_page_url: 'https://www.pornhub.com/view_video.php?token=signed-history?viewkey=ph123' }] })
    store.setRcloneRemotesCache([{ ...remotes[0], ...{ token: 'remote-secret' } }])
    store.tempFiles = [{ ...tempFile, ...{ path: 'private-temp-path' } }]
    store.subscriptionsCache = { member: { site: 'pornhub', username: 'private-subscriptions-user', subscriptions: [], fetchedAt: Date.now() } }
    store.subscriptionsLastSource = 'private-subscriptions-source'
    store.jobItems = { 1: [makeJobItem(1, 1, { download_url: 'signed-download-url' })] }
    store.liveProgress = { 1: { job_id: 1, item_id: 1, bytes_downloaded: 1, bytes_total: 2, speed_bps: 3, phase: 'downloading' } }
    store.persistCache()
    store.persistCache()
    vi.advanceTimersByTime(400)
    assert.equal(storage.writes, 1)
    assert.ok(storage.raw)
    const data = JSON.parse(storage.raw)
    assert.equal(data.v, 3)
    assert.equal(data.jobs[0].params.resume_count, 1)
    assert.equal(data.jobs[0].params.url_count, 1)
    assert.equal(data.jobs[0].params.urls, undefined)
    assert.equal(data.overview.current_job.params.urls, undefined)
    for (const key of ['ph_password', 'ph_cookie', 'xh_password', 'xh_cookie', 'serverchan_send_key',
      'yescaptcha_client_key', 'proxy_url', 'serverchan_api_url', 'rclone_extra_flags']) {
      assert.equal(data.settingsCache[key], '********')
    }
    for (const key of ['subscriptionsCache', 'subscriptionsLastSource', 'jobItems', 'liveProgress']) {
      assert.equal(data[key], undefined)
    }
    assert.deepEqual(data.tempFiles, [tempFile])
    assert.deepEqual(data.rcloneRemotesCache, remotes)
    assert.equal(data.targetsCache[0].last_error, '')
    assert.deepEqual(data.historyCache.items[0], { ...history.items[0], local_path: '', remote_path: '', video_page_url: '' })
    assert.doesNotMatch(storage.raw, /-secret|private-|signed-|raw-params|raw-url|proxy-password|avatar-password/)
    assert.doesNotMatch(storage.raw, /"(?:export_text|error_message|params_json|urls|download_url)"/)
    store.persistCache()
    vi.advanceTimersByTime(400)
    assert.equal(storage.writes, 1)
  } finally {
    store._resetSessionState()
  }
})

test('the first frame after reload has data for all six routes and identical job visuals', () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  const storage = mockSessionStorage('/jobs')
  const store = seedRouteStore()
  try {
    const creator = { ...job(1, 'completed', 1, 2), kind: 'manual_target' as const, params: {
      site: 'xhamster' as const, usernames: ['yourselena'], target_type: 'creator' as const, auto_upload: true,
      profile_names: { yourselena: 'YourSelena' }, profile_avatars: { yourselena: 'https://cdn.example/yourselena.jpg' },
      quality: '1080', pages: 'all', source_job_id: 87, resume_count: 2,
    } }
    store.jobs = [
      creator,
      { ...job(2, 'completed', 2), kind: 'baseline_sync', params: { target_id: target.id } },
      { ...job(3, 'completed', 3), kind: 'auto_update', params: {} },
      { ...job(4, 'completed', 4), kind: 'check_updates', params: { target_ids: [target.id] } },
      { ...job(5, 'completed', 5), params: { urls: ['https://xhamster.com/videos/smoke-xh12345'], group_by_uploader: true } },
      { ...job(6, 'completed', 6), params: { urls: ['https://www.pornhub.com/view_video.php?viewkey=ph123'],
        group_by_uploader: false, batch_name: 'Named batch' } },
      { ...job(7, 'completed', 7), kind: 'retry_items', params: { origin_label: 'Fragile Tempt', source_job_id: 87 } },
    ]
    const liveVisuals = store.jobs.map((row) => resolveJobVisual(row, store.targetsCache || []))
    store._applyLogsSnapshot(Array.from({ length: 110 }, (_, i) => ({ ...log, id: i + 1 })))
    storage.pagehide()
    const hydrated = useAppStore(createPinia())
    assert.equal(hydrated.hydratedFromCache, true)
    assert.equal(hydrated.authChecked, false)
    assert.equal(hydrated.jobsLoadedAt, 0)
    for (const route of ['download', 'jobs', 'targets', 'history', 'logs', 'settings']) {
      assert.equal(hydrated.hasRouteCache(route), true, `${route} has a first-frame snapshot`)
    }
    assert.deepEqual(hydrated.jobs[0].params, creator.params)
    assert.deepEqual(hydrated.jobs.slice(4, 6).map((row) => [row.params.url_count, row.params.site]), [[1, 'xhamster'], [1, 'pornhub']])
    assert.deepEqual(hydrated.jobs.map((row) => resolveJobVisual(row, hydrated.targetsCache || [])), liveVisuals)
    assert.deepEqual(hydrated.targetsCache?.[0], { ...target, last_error: '' })
    assert.deepEqual(hydrated.rcloneRemotesCache, remotes)
    assert.equal(hydrated.settingsCache?.serverchan_send_key, '********')
    assert.equal(hydrated.settingsCache?.yescaptcha_client_key, '')
    assert.equal(hydrated.settingsCache?.rclone_extra_flags, settings.rclone_extra_flags)
    assert.equal(hydrated.settingsCache?.proxy_url, settings.proxy_url)
    assert.equal(hydrated.accountAvatarUrl, '/api/settings/avatar?v=version1')
    assert.equal(hydrated.historyCache?.items[0].video_page_url, history.items[0].video_page_url)
    assert.deepEqual(hydrated.historyCache?.facets, history.facets)
    assert.equal(hydrated.logs.length, 100)
    assert.equal(hydrated.logs.at(-1)?.id, 110)
  } finally {
    store._resetSessionState()
  }
})

test('ensureRouteData fetches full jobs after hydration and reuses the live list afterward', async () => {
  const storage = mockSessionStorage('/jobs')
  const store = seedRouteStore()
  try {
    const fullJob = { ...job(1, 'completed', 1), params: { urls: ['https://www.pornhub.com/view_video.php?viewkey=ph123'] },
      export_text: 'live export', error_message: 'live error' }
    store.jobs = [fullJob]
    storage.pagehide()
    const hydrated = useAppStore(createPinia())
    assert.equal(hydrated.hasRouteCache('jobs'), true)
    assert.equal(hydrated.jobs[0].export_text, undefined)
    const listJobs = vi.spyOn(api, 'listJobs').mockResolvedValue({ items: [fullJob] })
    await hydrated.ensureRouteData('jobs')
    assert.deepEqual(hydrated.jobs, [fullJob])
    assert.ok(hydrated.jobsLoadedAt > 0)
    await hydrated.ensureRouteData('jobs')
    assert.equal(listJobs.mock.calls.length, 1)
  } finally {
    store._resetSessionState()
  }
})

test.each([
  ['download', ['jobs', 'tempFiles', 'targets']], ['jobs', ['jobs', 'targets']], ['settings', ['settings', 'remotes']],
  ['targets', ['targets', 'settings']], ['history', ['history']], ['logs', ['logs']],
])('revalidation refreshes the overview and all data for %s even when cached', async (route, expected) => {
  const storage = mockSessionStorage()
  const store = seedRouteStore()
  try {
    storage.pagehide()
    const hydrated = useAppStore(createPinia())
    vi.spyOn(hydrated, 'startRealtime').mockImplementation(() => {})
    vi.spyOn(hydrated, 'prefetchAll').mockImplementation(() => {})
    const auth = vi.spyOn(api, 'me').mockResolvedValue({ authenticated: true })
    const getOverview = vi.spyOn(api, 'overview').mockResolvedValue(overview)
    const responses = [
      ['jobs', vi.spyOn(api, 'listJobs').mockResolvedValue({ items: [] })],
      ['tempFiles', vi.spyOn(api, 'listTempFiles').mockResolvedValue({ items: [tempFile] })],
      ['targets', vi.spyOn(api, 'listTargets').mockResolvedValue({ items: [target] })],
      ['settings', vi.spyOn(api, 'getSettings').mockResolvedValue({ settings, cookie_status: accountCookie, xh_cookie_status: accountCookie })],
      ['remotes', vi.spyOn(api, 'listRcloneRemotes').mockResolvedValue({ remotes })],
      ['history', vi.spyOn(api, 'listVideos').mockResolvedValue(history)],
      ['logs', vi.spyOn(api, 'getLogs').mockResolvedValue({ items: [log] })],
    ] as const
    assert.equal(hydrated.hasRouteCache(route), true)
    assert.equal(await hydrated.revalidateSession(route), true)
    assert.equal(auth.mock.calls.length, 1)
    assert.equal(getOverview.mock.calls.length, 1)
    assert.deepEqual(responses.filter(([, call]) => call.mock.calls.length === 1).map(([key]) => key), expected)
    assert.equal(hydrated.hydratedFromCache, false)
  } finally {
    store._resetSessionState()
  }
})

test('pagehide flushes pending writes synchronously and logout cancels the next write', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  const storage = mockSessionStorage('/targets')
  const store = seedRouteStore()
  try {
    store.setTargetsCache([{ ...target, display_name: 'Newest target name' }])
    assert.equal(storage.writes, 0)
    storage.pagehide()
    assert.equal(storage.writes, 1)
    const hydrated = useAppStore(createPinia())
    assert.equal(hydrated.targetsCache?.[0].display_name, 'Newest target name')
    assert.deepEqual(storage.listener.mock.calls.map(([event]) => event), ['pagehide'])
    vi.advanceTimersByTime(400)
    assert.equal(storage.writes, 1)

    vi.spyOn(api, 'logout').mockResolvedValue({ authenticated: false })
    store.setTargetsCache([])
    const pendingLogout = store.logout()
    storage.pagehide()
    assert.equal(storage.raw, null)
    await pendingLogout
    vi.advanceTimersByTime(400)
    assert.equal(storage.writes, 1)
  } finally {
    store._resetSessionState()
  }
})

test('failed logout clears cached data immediately and preserves the authenticated retry path', async () => {
  const storage = mockSessionStorage('/jobs')
  const store = seedRouteStore()
  const logout = vi.spyOn(api, 'logout')
    .mockRejectedValueOnce(new Error('Gateway unavailable'))
    .mockResolvedValueOnce({ authenticated: false })
  try {
    store.persistCache()
    storage.pagehide()
    assert.ok(storage.raw)
    const attempt = store.logout()
    assert.equal(storage.raw, null)
    assert.equal(store.authenticated, true)
    await assert.rejects(attempt, /Gateway unavailable/)
    assert.equal(store.authenticated, true)
    store.setTargetsCache([target])
    storage.pagehide()
    assert.equal(storage.raw, null)
    assert.equal(storage.local.get(SESSION_LOGOUT_KEY), undefined)

    await store.logout()
    assert.equal(store.authenticated, false)
    assert.ok(storage.local.get(SESSION_LOGOUT_KEY))
    assert.equal(logout.mock.calls.length, 2)
  } finally {
    store._resetSessionState()
  }
})

test('logout treats a server 401 as confirmation of an expired session', async () => {
  const storage = mockSessionStorage('/jobs')
  const store = seedRouteStore()
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 401 })))
  try {
    await store.logout()
    assert.equal(store.authenticated, false)
    assert.ok(storage.local.get(SESSION_LOGOUT_KEY))
  } finally {
    store._resetSessionState()
  }
})

test('cross-tab logout invalidates persisted and already hydrated state', () => {
  const origin = mockSessionStorage('/jobs')
  const first = seedRouteStore()
  first.persistCache()
  origin.pagehide()
  assert.ok(origin.raw)
  const other = mockSessionStorage('/jobs', origin.raw, origin.local)
  const openTab = useAppStore(createPinia())
  assert.equal(openTab.authenticated, true)
  const timestamp = Date.now() + 1
  origin.local.set(SESSION_LOGOUT_KEY, String(timestamp))
  assert.equal(openTab.handleCrossTabLogout(timestamp), true)
  assert.equal(openTab.authenticated, false)
  assert.equal(other.raw, null)

  const sleeping = mockSessionStorage('/jobs', origin.raw, origin.local)
  const refreshed = useAppStore(createPinia())
  assert.equal(refreshed.hydratedFromCache, false)
  assert.equal(refreshed.authChecked, false)
  assert.equal(sleeping.raw, null)
  first._resetSessionState()
})

function startTestEventStream(store: ReturnType<typeof useAppStore>) {
  vi.stubGlobal('WebSocket', class {
    get [Symbol.toStringTag]() { return 'WebSocket' }
    close() {}
  })
  store.startRealtime()
  const socket = (store._wsClient as EventStreamClient).ws!
  return (msg: WsMessage) => socket.onmessage?.(new MessageEvent('message', { data: JSON.stringify(msg) }))
}

test('jobs_deleted removes a task across tabs and filters it out of an older REST response', async () => {
  mockSessionStorage('/jobs')
  const store = seedRouteStore()
  const removed = job(1, 'completed', 1)
  const preserved = job(2, 'completed', 1)
  store.jobs = [removed, preserved]
  let resolveList!: (result: { items: typeof removed[] }) => void
  const list = vi.spyOn(api, 'listJobs').mockImplementation(() => new Promise((resolve) => { resolveList = resolve }))
  const send = startTestEventStream(store)
  try {
    const pending = store.refreshJobs()
    await Promise.resolve()
    send({ type: 'jobs_deleted', ts: '', data: { job_ids: [removed.id] } })
    assert.deepEqual(store.jobs.map((row) => row.id), [2])
    resolveList({ items: [removed, preserved] })
    assert.deepEqual((await pending).map((row) => row.id), [2])
    assert.deepEqual(store.jobs.map((row) => row.id), [2])
    assert.equal(list.mock.calls.length, 1)
  } finally {
    store.handleUnauthorized()
  }
})

test('older REST job detail cannot replace progress received from the WebSocket during the request', async () => {
  vi.useFakeTimers()
  mockSessionStorage('/jobs')
  const store = seedRouteStore()
  const running = job(1, 'running', 1)
  store.jobs = [running]
  let resolveDetail!: (response: JobDetailResponse) => void
  vi.spyOn(api, 'getJob').mockImplementation(() => new Promise((resolve) => { resolveDetail = resolve }))
  const send = startTestEventStream(store)
  try {
    const pending = store.loadJobDetail(1)
    await Promise.resolve()
    const progress = { job_id: 1, item_id: 4, bytes_downloaded: 48, bytes_total: 64, speed_bps: 32, phase: 'downloading' }
    send({ type: 'item_progress', ts: '', data: progress })
    await vi.advanceTimersByTimeAsync(150)
    resolveDetail({ job: running, items: [makeJobItem(4, 1, { status: 'downloading', bytes_downloaded: 16 })] })
    await pending
    assert.deepEqual(store.liveProgress[4], progress)
  } finally {
    store.handleUnauthorized()
  }
})

test('resync_required reloads the jobs list, overview, and a pinned task detail', async () => {
  vi.useFakeTimers()
  mockSessionStorage('/jobs')
  const store = seedRouteStore()
  const task = job(1, 'completed', 1)
  const listJobs = vi.spyOn(api, 'listJobs').mockResolvedValue({ items: [task] })
  const overviewApi = vi.spyOn(api, 'overview').mockResolvedValue(overview)
  const detail = vi.spyOn(api, 'getJob').mockResolvedValue({ job: task, items: [] })
  const unpin = store.pinJobDetail(1)
  const send = startTestEventStream(store)
  try {
    send({ type: 'resync_required', data: {} })
    await vi.advanceTimersByTimeAsync(300)
    assert.equal(listJobs.mock.calls.length, 1)
    assert.equal(overviewApi.mock.calls.length, 1)
    assert.deepEqual(detail.mock.calls, [[1]])
  } finally {
    unpin()
    store.handleUnauthorized()
  }
})

test.each([['/jobs', 'history'], ['/logs', 'logs']])('oversized snapshots keep core data and prioritize %s', (pathname, kept) => {
  const storage = mockSessionStorage(pathname)
  const store = seedRouteStore()
  try {
    store.jobs = [job(1, 'completed', 1)]
    store.setHistoryCache({ ...history, items: [{ ...history.items[0], title: 'h'.repeat(140 * 1024) }] })
    store._applyLogsSnapshot(Array.from({ length: 100 }, (_, i) => ({ ...log, id: i + 1, message: 'l'.repeat(1500) })))
    storage.pagehide()
    assert.ok(storage.raw && storage.raw.length < SESSION_CACHE_MAX_CHARS)
    const hydrated = useAppStore(createPinia())
    for (const route of ['download', 'jobs', 'settings', 'targets']) assert.equal(hydrated.hasRouteCache(route), true)
    assert.equal(hydrated.hasRouteCache('history'), kept === 'history')
    assert.equal(hydrated.hasRouteCache('logs'), kept === 'logs')
    assert.equal(hydrated.jobs[0].id, 1)
  } finally {
    store._resetSessionState()
  }
})

test('late WebSocket events from a disconnected client cannot change the next connection', () => {
  vi.stubGlobal('window', { location: { protocol: 'https:', host: 'panel.example' } })
  vi.stubGlobal('WebSocket', class { close() {} })
  const events: WsMessage[] = []
  const statuses: boolean[] = []
  const client = new EventStreamClient({ onEvent: (event) => events.push(event), onStatusChange: (status) => statuses.push(status) })
  try {
    client.connect()
    const old = client.ws
    client.connect()
    assert.equal(client.ws, old)
    client.disconnect()
    client.connect()
    const fresh = client.ws
    assert.ok(fresh?.onopen)
    assert.ok(old?.onclose && old.onmessage)
    fresh.onopen(new Event('open'))
    old.onclose(Object.assign(new Event('close'), { code: 4401, reason: '', wasClean: false }))
    old.onmessage(new MessageEvent('message', { data: '{"type":"job_updated"}' }))
    assert.deepEqual(statuses, [false, true])
    assert.deepEqual(events, [])
    assert.equal(client.ws, fresh)
  } finally {
    client.disconnect()
  }
})

test('optimistic, snapshot and live cancellation clear telemetry until the terminal event', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  const storage = mockSessionStorage('/jobs')
  vi.stubGlobal('WebSocket', class {
    get [Symbol.toStringTag]() { return 'WebSocket' }
    close() {}
  })
  const store = useAppStore(createPinia())
  const running = job(1, 'running', 1)
  const other = job(2, 'running', 1)
  const scan = { job_id: 1, mode: 'pages', page: 1 } as const
  const scheduler = { job_id: 1, level: 'green', free_bytes: 1024 } as const
  const progress = { job_id: 1, item_id: 4, bytes_downloaded: 16, bytes_total: 64, speed_bps: 32, phase: 'downloading' }
  const otherProgress = { ...progress, job_id: 2, item_id: 8 }
  const readOverview = vi.spyOn(api, 'overview').mockResolvedValue(overview)
  vi.spyOn(api, 'listTempFiles').mockResolvedValue({ items: [] })
  vi.spyOn(api, 'getJob').mockResolvedValue({ job: running, items: [makeJobItem(4, 1, { status: 'downloading', speed_bps: 32 })] })
  try {
    store.authenticated = true
    store.overview = { ...overview, running: true, current_job: running }
    store.jobs = [running, other]
    store.startRealtime()
    const socket = (store._wsClient as EventStreamClient).ws!
    const send = (msg: WsMessage) => socket.onmessage?.(new MessageEvent('message', { data: JSON.stringify(msg) }))
    send({ type: 'snapshot', data: {
      logs: [], live_progress: { 4: progress, 8: otherProgress },
      scan_status: { 1: scan }, scheduler_status: { 1: scheduler },
    } })
    send({ type: 'item_progress', ts: '', data: progress })
    store.markJobCancelling(1)
    assert.equal(store.isJobCancelling(1), true)
    assert.equal(store.overview.cancelling_job_id, undefined, 'only server-confirmed cancellation is eligible for caching')
    storage.pagehide()
    assert.equal(useAppStore(createPinia()).isJobCancelling(1), false)
    assert.deepEqual(Object.values(store.liveProgress), [otherProgress])
    assert.deepEqual(store.schedulerStatus, {})
    assert.deepEqual(store.scanStatus, {})

    send({ type: 'snapshot', data: {
      logs: [], cancelling_jobs: [2], live_progress: { 4: progress, 8: otherProgress },
      scan_status: { 1: scan }, scheduler_status: { 1: scheduler },
    } })
    assert.equal(store.isJobCancelling(1), true)
    assert.equal(store.isJobCancelling(2), true)
    send({ type: 'job_cancelling', ts: '', data: { job_id: 1, cancelling: true } })
    assert.equal(store.overview.cancelling_job_id, 1)
    send({ type: 'item_progress', ts: '', data: progress })
    send({ type: 'job_scan', ts: '', data: scan })
    send({ type: 'job_scheduler', ts: '', data: scheduler })
    await store.loadJobDetail(1)
    await vi.advanceTimersByTimeAsync(150)
    assert.deepEqual(store.liveProgress, {})
    assert.deepEqual(store.schedulerStatus, {})
    assert.deepEqual(store.scanStatus, {})

    send({ type: 'job_cancelling', ts: '', data: { job_id: 1, cancelling: false } })
    assert.equal(store.isJobCancelling(1), true)
    send({ type: 'job_updated', ts: '', data: job(1, 'cancelled', 2) })
    assert.equal(store.jobs.find((row) => row.id === 1)?.status, 'cancelled')
    assert.equal(store.isJobCancelling(1), false)
    assert.equal(store.isJobCancelling(2), true)
    assert.equal(store.overview.running, false)
    assert.equal(store.overview.current_job, null)
    assert.equal(store.overview.cancelling_job_id, null)
    await Promise.resolve()
    assert.equal(readOverview.mock.calls.length, 1, 'overview refresh starts without a timer')
    await vi.advanceTimersByTimeAsync(360)
    assert.equal(readOverview.mock.calls.length, 1, 'terminal update clears the pending overview timer')
  } finally {
    store.stopRealtime()
    store._resetSessionState()
  }
})

test('overview restores stopping state on reload and deleting a job cleans runtime metadata', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  const storage = mockSessionStorage('/jobs')
  const store = seedRouteStore()
  const running = job(21, 'running', 1)
  const fetchOverview = vi.spyOn(api, 'overview').mockResolvedValue({ ...overview, running: true, current_job: running, cancelling_job_id: 21 })
  try {
    await store.refreshOverview()
    assert.equal(store.isJobCancelling(21), true)
    fetchOverview.mockResolvedValueOnce({ ...overview, running: true, current_job: running, cancelling_job_id: null })
    await store.refreshOverview()
    assert.equal(store.overview?.cancelling_job_id, 21)
    storage.pagehide()
    const hydrated = useAppStore(createPinia())
    assert.equal(hydrated.isJobCancelling(21), true)
    store.scanStatus[21] = { job_id: 21, mode: 'pages', page: 1 }
    store.schedulerStatus[21] = { job_id: 21, level: 'green', free_bytes: 1024 }
    store.removeJobs([21])
    assert.equal(store.scanStatus[21], undefined)
    assert.equal(store.schedulerStatus[21], undefined)
    assert.equal(store.isJobCancelling(21), false)
  } finally {
    store._resetSessionState()
  }
})

test('paginated job details dedupe matching prefetch, drop stale page switches, hydrate off-page progress, and keep WS updates scoped', async () => {
  vi.useFakeTimers()
  mockSessionStorage('/jobs')
  const store = seedRouteStore()
  const running = job(30, 'running', 1)
  store.jobs = [running]

  let resolvePage1!: (res: JobDetailResponse) => void
  let resolveFailed!: (res: JobDetailResponse) => void
  const getJob = vi.spyOn(api, 'getJob')
    .mockImplementationOnce(() => new Promise((resolve) => { resolvePage1 = resolve }))
    .mockImplementationOnce(() => new Promise((resolve) => { resolveFailed = resolve }))
    .mockResolvedValueOnce({
      job: running,
      items: [makeJobItem(2, 30, { status: 'failed', revision: 2 })],
      total: 1,
      total_all: 120,
      page: 1,
      page_size: 50,
      status: 'failed',
      status_counts: { done: 118, failed: 1, downloading: 1 },
      paginated: true,
    })

  const send = startTestEventStream(store)
  const unpin = store.pinJobDetail(30)
  try {
    // 1. Concurrent prefetch + expand with identical query reuses a single HTTP request.
    const prefetch = store.prefetchJobDetail(30, { page: 1, page_size: 50, status: 'all' })
    const expand = store.loadJobDetail(30, { page: 1, page_size: 50, status: 'all' })
    await Promise.resolve()
    assert.equal(getJob.mock.calls.length, 1)

    // 2. Switching filter while page 1 is still in flight issues a fresh request and ignores late page 1.
    const switchFilter = store.loadJobDetail(30, { page: 1, page_size: 50, status: 'failed' })
    await Promise.resolve()
    assert.equal(getJob.mock.calls.length, 2)
    assert.deepEqual(getJob.mock.calls[1], [30, { page: 1, page_size: 50, status: 'failed' }])

    resolveFailed({
      job: running,
      items: [makeJobItem(2, 30, { status: 'failed', revision: 1 })],
      total: 1,
      total_all: 120,
      page: 1,
      page_size: 50,
      status: 'failed',
      status_counts: { done: 118, failed: 1, downloading: 1 },
      paginated: true,
      live_progress: {
        95: { job_id: 30, item_id: 95, bytes_downloaded: 400, bytes_total: 1000, speed_bps: 500, phase: 'downloading' },
      },
    })
    await switchFilter
    assert.equal(store.jobItemMeta[30]?.status, 'failed')
    assert.equal(store.liveProgress[95]?.bytes_downloaded, 400, 'hydrates off-page active item progress')

    // Late page 1 response must NOT overwrite the newer failed-filter view.
    resolvePage1({
      job: running,
      items: [makeJobItem(1, 30, { status: 'done', revision: 1 })],
      total: 120,
      total_all: 120,
      page: 1,
      page_size: 50,
      status: 'all',
      status_counts: { done: 118, failed: 1, downloading: 1 },
      paginated: true,
    })
    await Promise.all([prefetch, expand])
    assert.equal(store.jobItemMeta[30]?.status, 'failed')
    assert.deepEqual(store.jobItems[30]?.map((it) => it.id), [2])

    // 3. Off-page WS item_updated must NOT pollute the paginated slice, and job_items_changed reloads the active filter query.
    send({ type: 'item_updated', ts: '', data: makeJobItem(99, 30, { status: 'done', revision: 2 }) })
    await vi.advanceTimersByTimeAsync(150)
    assert.deepEqual(store.jobItems[30]?.map((it) => it.id), [2])

    send({ type: 'job_items_changed', ts: '', data: { job_id: 30 } })
    await vi.advanceTimersByTimeAsync(300)
    assert.equal(getJob.mock.calls.length, 3)
    assert.deepEqual(getJob.mock.calls[2], [30, { page: 1, page_size: 50, status: 'failed' }])
  } finally {
    unpin()
    store.stopRealtime()
    store._resetSessionState()
  }
})

test('slice prefetch warms tabs without overwriting the visible page and enables instant tab switching', async () => {
  mockSessionStorage('/jobs')
  const store = seedRouteStore()
  const finished = { ...job(31, 'completed', 1), total_items: 1200 }
  store.jobs = [finished]

  const getJob = vi.spyOn(api, 'getJob')
    .mockResolvedValueOnce({
      job: finished,
      items: [makeJobItem(1, 31, { status: 'done' })],
      total: 1200,
      total_all: 1200,
      page: 1,
      page_size: 50,
      status: 'all',
      status_counts: { done: 1190, failed: 10 },
      paginated: true,
    })
    .mockResolvedValueOnce({
      job: finished,
      items: [makeJobItem(501, 31, { status: 'failed' })],
      total: 10,
      total_all: 1200,
      page: 1,
      page_size: 50,
      status: 'failed',
      status_counts: { done: 1190, failed: 10 },
      paginated: true,
    })

  try {
    await store.loadJobDetail(31, { page: 1, page_size: 50, status: 'all' })
    assert.deepEqual(store.jobItems[31]?.map((it) => it.id), [1])

    // Hovering / warming the failed pill caches the slice WITHOUT altering the visible 'all' page.
    await store.prefetchJobDetailSlice(31, { page: 1, page_size: 50, status: 'failed' })
    assert.equal(getJob.mock.calls.length, 2)
    assert.equal(store.jobItemMeta[31]?.status, 'all')
    assert.deepEqual(store.jobItems[31]?.map((it) => it.id), [1])

    // Switching to failed activates synchronously from cache with zero additional HTTP calls.
    const activated = store.activateJobDetailSlice(31, { page: 1, page_size: 50, status: 'failed' })
    assert.equal(activated.hit, true)
    assert.equal(activated.fresh, true)
    assert.equal(store.jobItemMeta[31]?.status, 'failed')
    assert.deepEqual(store.jobItems[31]?.map((it) => it.id), [501])

    // Switching back to 'all' also hits cache synchronously.
    const back = store.activateJobDetailSlice(31, { page: 1, page_size: 50, status: 'all' })
    assert.equal(back.hit, true)
    assert.equal(store.jobItemMeta[31]?.status, 'all')
    assert.deepEqual(store.jobItems[31]?.map((it) => it.id), [1])
    assert.equal(getJob.mock.calls.length, 2)
  } finally {
    store._resetSessionState()
  }
})

test('live item_updated patches cached slices instead of evicting them and slice activation supersedes stale inflight loads', async () => {
  vi.useFakeTimers()
  mockSessionStorage('/jobs')
  const store = seedRouteStore()
  const running = { ...job(32, 'running', 1), total_items: 2000 }
  store.jobs = [running]

  let resolveSlowDownloadRefresh!: (res: JobDetailResponse) => void
  const getJob = vi.spyOn(api, 'getJob')
    .mockResolvedValueOnce({
      job: running,
      items: [makeJobItem(1, 32, { status: 'done', revision: 1 })],
      total: 2000,
      total_all: 2000,
      page: 1,
      page_size: 50,
      status: 'all',
      status_counts: { done: 100, downloading: 1, uploading: 1, pending: 1898 },
      paginated: true,
    })
    .mockResolvedValueOnce({
      job: running,
      items: [makeJobItem(50, 32, { status: 'downloading', revision: 1 })],
      total: 1,
      total_all: 2000,
      page: 1,
      page_size: 50,
      status: 'downloading',
      status_counts: { done: 100, downloading: 1, uploading: 1, pending: 1898 },
      paginated: true,
    })
    .mockResolvedValueOnce({
      job: running,
      items: [makeJobItem(60, 32, { status: 'uploading', revision: 1 })],
      total: 1,
      total_all: 2000,
      page: 1,
      page_size: 50,
      status: 'uploading',
      status_counts: { done: 100, downloading: 1, uploading: 1, pending: 1898 },
      paginated: true,
    })
    .mockImplementationOnce(() => new Promise((resolve) => { resolveSlowDownloadRefresh = resolve }))

  const send = startTestEventStream(store)
  try {
    await store.loadJobDetail(32, { page: 1, page_size: 50, status: 'all' })
    await store.prefetchJobDetailSlice(32, { page: 1, page_size: 50, status: 'downloading' })
    await store.prefetchJobDetailSlice(32, { page: 1, page_size: 50, status: 'uploading' })

    // Live WS item_updated moves #50 from downloading -> uploading and adds #51 to downloading.
    send({ type: 'item_updated', ts: '', data: makeJobItem(50, 32, { status: 'uploading', revision: 2 }) })
    send({ type: 'item_updated', ts: '', data: makeJobItem(51, 32, { status: 'downloading', revision: 2 }) })
    await vi.advanceTimersByTimeAsync(150)

    // Cached 'downloading' slice MUST still hit synchronously (0ms switch) with #50 removed and #51 patched in!
    const dlAct = store.activateJobDetailSlice(32, { page: 1, page_size: 50, status: 'downloading' })
    assert.equal(dlAct.hit, true)
    assert.equal(dlAct.fresh, false, 'marked stale so background reconcile can run without spinner')
    assert.deepEqual(store.jobItems[32]?.map((it) => it.id), [51])

    // Start background reconcile for 'downloading', then immediately click 'uploading' before it returns.
    const slowBg = store.loadJobDetail(32, { page: 1, page_size: 50, status: 'downloading' })
    await Promise.resolve()
    const upAct = store.activateJobDetailSlice(32, { page: 1, page_size: 50, status: 'uploading' })
    assert.equal(upAct.hit, true)
    assert.deepEqual(store.jobItems[32]?.map((it) => it.id), [50, 60])

    // Late background response for 'downloading' MUST NOT overwrite the active 'uploading' tab!
    resolveSlowDownloadRefresh({
      job: running,
      items: [makeJobItem(51, 32, { status: 'downloading', revision: 2 })],
      total: 1,
      total_all: 2000,
      page: 1,
      page_size: 50,
      status: 'downloading',
      status_counts: { done: 100, downloading: 1, uploading: 2, pending: 1897 },
      paginated: true,
    })
    await slowBg
    assert.equal(store.jobItemMeta[32]?.status, 'uploading')
    assert.deepEqual(store.jobItems[32]?.map((it) => it.id), [50, 60])
    assert.equal(getJob.mock.calls.length, 4)
  } finally {
    store.stopRealtime()
    store._resetSessionState()
  }
})
