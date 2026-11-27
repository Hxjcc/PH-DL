import assert from 'node:assert/strict'
import { afterEach, test, vi } from 'vitest'
import { createPinia } from 'pinia'
import { api } from '../src/api/client'
import { EventStreamClient } from '../src/api/ws'
import { useAppStore } from '../src/stores/app'
import type { JobDetailResponse, JobStatus, RuntimeSettings, WsMessage } from '../src/types/api'
import { makeJob, makeJobItem } from './fixtures'

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function job(id: number, status: JobStatus, revision: number, resumeCount = 0) {
  return { ...makeJob(id, status, { resume_count: resumeCount }), revision }
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

test('persistent session summaries omit previous caches, credentials and signed media', () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  let raw: string | null = JSON.stringify({ v: 1, authenticated: true, settingsCache: { ph_password: 'test-secret' } })
  let writes = 0
  vi.stubGlobal('window', { sessionStorage: {
    getItem: () => raw,
    removeItem: () => { raw = null },
    setItem: (_key: string, value: string) => {
      raw = value
      writes++
    },
  } })
  const store = useAppStore(createPinia())
  try {
    assert.equal(raw, null)
    store.authenticated = true
    store.jobsLoaded = true
    store.jobs = [{ ...job(1, 'completed', 1), export_text: 'signed-media-url', params_json: 'raw-params',
      params: { urls: ['raw-url'], resume_count: 1 } }]
    store.settingsCache = { ph_password: 'test-secret' } as RuntimeSettings
    store.jobItems = { 1: [makeJobItem(1, 1, { download_url: 'signed-download-url' })] }
    store.persistCache()
    store.persistCache()
    vi.advanceTimersByTime(1200)
    assert.equal(writes, 1)
    assert.ok(raw)
    assert.equal(JSON.parse(raw).jobs[0].params.resume_count, 1)
    assert.doesNotMatch(raw, /test-secret|signed-media|signed-download|raw-params|raw-url/)
    store.persistCache()
    vi.advanceTimersByTime(1200)
    assert.equal(writes, 1)
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
