import assert from 'node:assert/strict'
import test from 'node:test'
import { createPinia } from 'pinia'
import { api } from '../src/api/client.js'
import { EventStreamClient } from '../src/api/ws.js'
import { useAppStore } from '../src/stores/app.js'

function job(id, status, revision, resumeCount = 0) {
  return { id, status, revision, params: { resume_count: resumeCount } }
}

test('job revisions order repeated resumes and reject stale terminal snapshots', () => {
  const store = useAppStore(createPinia())
  try {
    for (const [status, revision, run] of [
      ['failed', 10, 0], ['queued', 11, 1], ['scanning', 12, 1], ['running', 13, 1],
      ['scanning', 14, 1], ['cancelled', 15, 1], ['queued', 16, 2], ['running', 17, 2],
    ]) {
      store._upsertJob(job(1, status, revision, run))
      assert.equal(store.jobs[0].status, status)
    }
    assert.equal(store._upsertJob(job(1, 'cancelled', 15, 1)), false)
    assert.equal(store.jobs[0].revision, 17)
  } finally {
    store._resetSessionState()
  }
})

test('job detail requests merge item events received while the snapshot was in flight', async (t) => {
  const store = useAppStore(createPinia())
  let resolve
  let markStarted
  const started = new Promise((ready) => { markStarted = ready })
  const response = new Promise((fulfill) => { resolve = fulfill })
  const get = t.mock.method(api, 'getJob', () => { markStarted(); return response })
  try {
    const first = store.loadJobDetail(1)
    const second = store.loadJobDetail(1)
    await started
    store._upsertItem({ id: 5, job_id: 1, status: 'done', revision: 3 })
    store._upsertItem({ id: 6, job_id: 1, status: 'pending', revision: 1 })
    resolve({ job: job(1, 'running', 5, 1), items: [{ id: 5, job_id: 1, status: 'uploading', revision: 2 }] })
    await Promise.all([first, second])
    assert.equal(get.mock.callCount(), 1)
    assert.deepEqual(store.jobItems[1].map((row) => [row.id, row.status]), [[5, 'done'], [6, 'pending']])
  } finally {
    store._resetSessionState()
  }
})

test('open job cards keep their cached details until all their pins are released', async (t) => {
  const store = useAppStore(createPinia())
  t.mock.method(api, 'getJob', async (id) => ({ job: job(id, 'completed', 1), items: [{ id, job_id: id }] }))
  const release1 = store.pinJobDetail(1)
  const release2 = store.pinJobDetail(1)
  try {
    for (let id = 1; id <= 10; id++) await store.loadJobDetail(id)
    release1()
    await store.loadJobDetail(11)
    assert.equal(store.jobItems[1][0].id, 1)
    assert.equal(Object.keys(store.jobItems).length, 9)
    release2()
    assert.equal(store.jobItems[1], undefined)
    assert.equal(Object.keys(store.jobItems).length, 8)
  } finally {
    store._resetSessionState()
  }
})

test('persistent session summaries omit previous caches, credentials and signed media', (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const previousWindow = globalThis.window
  let raw = JSON.stringify({ v: 1, authenticated: true, settingsCache: { ph_password: 'test-secret' } })
  let writes = 0
  globalThis.window = { sessionStorage: {
    getItem: () => raw,
    removeItem: () => { raw = null },
    setItem: (_key, value) => { raw = value; writes++ },
  } }
  const store = useAppStore(createPinia())
  try {
    assert.equal(raw, null)
    store.authenticated = true
    store.jobsLoaded = true
    store.jobs = [{ ...job(1, 'completed', 1), export_text: 'signed-media-url', params_json: 'raw-params',
      params: { urls: ['raw-url'], resume_count: 1 } }]
    store.settingsCache = { ph_password: 'test-secret' }
    store.jobItems = { 1: [{ download_url: 'signed-download-url' }] }
    store.persistCache()
    store.persistCache()
    t.mock.timers.tick(1200)
    assert.equal(writes, 1)
    assert.equal(JSON.parse(raw).jobs[0].params.resume_count, 1)
    assert.doesNotMatch(raw, /test-secret|signed-media|signed-download|raw-params|raw-url/)
    store.persistCache()
    t.mock.timers.tick(1200)
    assert.equal(writes, 1)
  } finally {
    store._resetSessionState()
    globalThis.window = previousWindow
  }
})

test('late WebSocket events from a disconnected client cannot change the next connection', () => {
  const previousWindow = globalThis.window
  const PreviousWebSocket = globalThis.WebSocket
  globalThis.window = { location: { protocol: 'https:', host: 'panel.example' } }
  globalThis.WebSocket = class { close() {} }
  const events = []
  const statuses = []
  const client = new EventStreamClient({ onEvent: (event) => events.push(event), onStatusChange: (status) => statuses.push(status) })
  try {
    client.connect()
    const old = client.ws
    client.connect()
    assert.equal(client.ws, old)
    client.disconnect()
    client.connect()
    const fresh = client.ws
    fresh.onopen()
    old.onclose({ code: 4401 })
    old.onmessage({ data: '{"type":"job_updated"}' })
    assert.deepEqual(statuses, [false, true])
    assert.deepEqual(events, [])
    assert.equal(client.ws, fresh)
  } finally {
    client.disconnect()
    globalThis.window = previousWindow
    globalThis.WebSocket = PreviousWebSocket
  }
})
