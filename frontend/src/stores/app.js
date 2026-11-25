import { defineStore } from 'pinia'
import { toast } from 'vue-sonner'
import { api } from '../api/client'
import { EventStreamClient } from '../api/ws'
import { JOB_LIMITS, HISTORY_PAGE_SIZES, readListPreference, saveListPreference } from '../utils/listPreferences'

export const DEFAULT_HISTORY_FILTERS = Object.freeze({
  search: '',
  uploader: '',
  status: '',
  quality: '',
  page: 1,
  page_size: 30,
})

const SESSION_CACHE_KEY = 'phdl-session-cache-v1'
const SESSION_CACHE_TTL_MS = 30 * 60 * 1000
const SUBSCRIPTIONS_CACHE_TTL_MS = 30 * 60 * 1000

const inflight = new Map()
const latestSeq = new Map()
let sessionEpoch = 0
let persistTimer = null
let persistIdleId = null
let persistGen = 0

function cancelScheduledPersist() {
  if (persistTimer !== null) {
    clearTimeout(persistTimer)
    persistTimer = null
  }
  if (persistIdleId !== null && typeof window !== 'undefined' && typeof window.cancelIdleCallback === 'function') {
    window.cancelIdleCallback(persistIdleId)
    persistIdleId = null
  }
}

function readSessionCache() {
  try {
    const raw = window.sessionStorage.getItem(SESSION_CACHE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw)
    if (!parsed || parsed.v !== 1 || !parsed.authenticated) return null
    if (Date.now() - Number(parsed.ts || 0) > SESSION_CACHE_TTL_MS) {
      window.sessionStorage.removeItem(SESSION_CACHE_KEY)
      return null
    }
    return parsed
  } catch {
    return null
  }
}

function clearSessionCache() {
  persistGen += 1
  cancelScheduledPersist()
  try {
    window.sessionStorage.removeItem(SESSION_CACHE_KEY)
  } catch {}
}

function schedulePersistSessionCache(store) {
  if (!store.authenticated) return
  if (persistTimer !== null || persistIdleId !== null) return
  const gen = persistGen
  const flush = () => {
    persistTimer = null
    persistIdleId = null
    if (gen !== persistGen || !store.authenticated) return
    try {
      const runningJobId = store.overview?.current_job?.id
      const compactJobItems = {}
      if (runningJobId && Array.isArray(store.jobItems[runningJobId])) {
        compactJobItems[runningJobId] = store.jobItems[runningJobId].slice(0, 100)
      }
      const payload = {
        v: 1,
        ts: Date.now(),
        authenticated: true,
        overview: store.overviewLoaded ? store.overview : null,
        overviewLoaded: store.overviewLoaded,
        jobs: store.jobsLoaded ? store.jobs.slice(0, store.jobsLimit) : null,
        jobsFetchedLimit: Math.min(store.jobsFetchedLimit, store.jobsLimit),
        tempFiles: store.tempFilesLoaded ? store.tempFiles : null,
        targetsCache: store.targetsCache,
        subscriptionsCache: store.subscriptionsCache,
        subscriptionsLastSource: store.subscriptionsLastSource,
        settingsCache: store.settingsCache,
        historyCache: store.historyCache,
        jobItems: compactJobItems,
        logs: store.logsLoaded ? store.logs.slice(-200) : null,
      }
      window.sessionStorage.setItem(SESSION_CACHE_KEY, JSON.stringify(payload))
    } catch {}
  }
  persistTimer = setTimeout(() => {
    persistTimer = null
    if (gen !== persistGen || !store.authenticated) return
    if (typeof window !== 'undefined' && typeof window.requestIdleCallback === 'function') {
      persistIdleId = window.requestIdleCallback(flush, { timeout: 800 })
    } else {
      flush()
    }
  }, 450)
}

function resetInflight() {
  sessionEpoch += 1
  inflight.clear()
  latestSeq.clear()
}

function runRequest(key, fn) {
  const seq = (latestSeq.get(key) || 0) + 1
  latestSeq.set(key, seq)
  const epoch = sessionEpoch
  const isLatest = () => epoch === sessionEpoch && latestSeq.get(key) === seq
  const p = Promise.resolve()
    .then(() => fn({ isLatest }))
    .finally(() => {
      if (inflight.get(key) === p) {
        inflight.delete(key)
      }
    })
  inflight.set(key, p)
  return p
}

function ensureRequest(key, isLoaded, fn) {
  if (isLoaded()) return Promise.resolve()
  const existing = inflight.get(key)
  if (existing) return existing
  return runRequest(key, fn)
}

export const useAppStore = defineStore('app', {
  state: () => {
    const cached = readSessionCache()
    const hasCache = Boolean(cached && cached.authenticated)
    return {
      authChecked: false,
      hydratedFromCache: hasCache,
      authenticated: hasCache,
      wsConnected: hasCache,
      overview: hasCache && cached.overviewLoaded ? cached.overview : null,
      overviewLoaded: Boolean(hasCache && cached.overviewLoaded),
      jobs: hasCache && Array.isArray(cached.jobs) ? cached.jobs : [],
      jobsLoaded: Boolean(hasCache && Array.isArray(cached.jobs)),
      jobsLoadedAt: 0,
      jobsLimit: readListPreference('jobs', JOB_LIMITS),
      jobsFetchedLimit: hasCache && Array.isArray(cached.jobs) ? Number(cached.jobsFetchedLimit || 30) : 0,
      _jobsRequestedLimit: 0,
      tempFiles: hasCache && Array.isArray(cached.tempFiles) ? cached.tempFiles : [],
      tempFilesLoaded: Boolean(hasCache && Array.isArray(cached.tempFiles)),
      tempFilesLoadedAt: 0,
      targetsCache: hasCache && Array.isArray(cached.targetsCache) ? cached.targetsCache : null,
      subscriptionsCache: hasCache && cached.subscriptionsCache ? cached.subscriptionsCache : {},
      subscriptionsLastSource: hasCache ? cached.subscriptionsLastSource || '' : '',
      targetsLoadedAt: 0,
      settingsCache: hasCache && cached.settingsCache ? cached.settingsCache : null,
      settingsLoadedAt: 0,
      historyCache: hasCache && cached.historyCache ? cached.historyCache : null,
      historyLoadedAt: 0,
      historyPageSize: readListPreference('history', HISTORY_PAGE_SIZES),
      jobItems: hasCache && cached.jobItems && typeof cached.jobItems === 'object' ? cached.jobItems : {},
      liveProgress: {},
      schedulerStatus: {},
      logs: hasCache && Array.isArray(cached.logs) ? cached.logs : [],
      logsLoaded: Boolean(hasCache && Array.isArray(cached.logs)),
      _wsClient: null,
      _wsOpenedOnce: false,
      _wsGraceTimer: null,
      _overviewTimer: null,
    }
  },

  actions: {
    hasRouteCache(routeName) {
      if (!this.overviewLoaded) return false
      if (routeName === 'download') {
        return this.tempFilesLoaded && this.jobsLoaded
      }
      if (routeName === 'jobs') {
        return this.jobsLoaded && this.jobsFetchedLimit >= this.jobsLimit
      }
      if (routeName === 'targets') {
        return this.targetsCache !== null && this.settingsCache !== null
      }
      if (routeName === 'history') {
        return this.historyCache !== null && Number(this.historyCache.page_size || 30) === this.historyPageSize
      }
      if (routeName === 'logs') {
        return this.logsLoaded
      }
      if (routeName === 'settings') {
        return this.settingsCache !== null
      }
      return true
    },

    persistCache() {
      schedulePersistSessionCache(this)
    },

    setJobsLimit(value) {
      const limit = Number(value)
      if (!JOB_LIMITS.includes(limit)) return false
      this.jobsLimit = limit
      saveListPreference('jobs', limit)
      this.persistCache()
      return true
    },

    setHistoryPageSize(value) {
      const size = Number(value)
      if (!HISTORY_PAGE_SIZES.includes(size)) return false
      this.historyPageSize = size
      saveListPreference('history', size)
      return true
    },

    defaultHistoryFilters() {
      return { ...DEFAULT_HISTORY_FILTERS, page_size: this.historyPageSize }
    },

    notify(message, type = 'info') {
      if (!message) return
      if (type === 'success') toast.success(String(message))
      else if (type === 'error') toast.error(String(message))
      else if (type === 'warning') toast.warning(String(message))
      else toast(String(message))
    },

    async checkAuth(initialRouteName = null) {
      try {
        const res = await api.me()
        this.authenticated = Boolean(res?.authenticated)
      } catch {
        this.authenticated = false
      } finally {
        this.authChecked = true
        this.hydratedFromCache = false
      }
      if (this.authenticated) {
        this.startRealtime()
        if (initialRouteName) {
          await this.ensureRouteData(initialRouteName)
        } else {
          await this.ensureOverview()
        }
        this.persistCache()
        this.prefetchAll()
      } else {
        this._resetSessionState()
      }
      return this.authenticated
    },

    revalidateSession(routeName = null) {
      const existing = inflight.get('revalidate')
      if (existing) return existing
      return runRequest('revalidate', async ({ isLatest }) => {
        let ok = false
        try {
          const res = await api.me()
          ok = Boolean(res?.authenticated)
        } catch {
          ok = false
        }
        if (!isLatest()) return ok
        this.authChecked = true
        this.hydratedFromCache = false
        if (!ok) {
          this.handleUnauthorized()
          return false
        }
        this.authenticated = true
        this.startRealtime()
        const tasks = [this.refreshOverview()]
        if (routeName === 'download') {
          tasks.push(this.refreshTempFiles(), this.refreshJobs())
        } else if (routeName === 'jobs') {
          tasks.push(this.refreshJobs(), this.ensureTargets())
        } else if (routeName === 'targets') {
          tasks.push(this.refreshTargets(), this.refreshSettings())
        } else if (routeName === 'history') {
          tasks.push(this.refreshHistory())
        } else if (routeName === 'logs') {
          tasks.push(this.refreshLogs())
        } else if (routeName === 'settings') {
          tasks.push(this.refreshSettings())
        }
        await Promise.allSettled(tasks)
        if (
          (routeName === 'download' || routeName === 'jobs') &&
          this.overview?.current_job?.id
        ) {
          this.loadJobDetail(this.overview.current_job.id).catch(() => {})
        }
        this.persistCache()
        this.prefetchAll()
        return true
      })
    },

    async login(password) {
      await api.login(password)
      this.authenticated = true
      this.authChecked = true
      this.hydratedFromCache = false
      this.startRealtime()
      await this.refreshOverview()
      this.persistCache()
      this.prefetchAll()
    },

    _resetSessionState() {
      if (this._overviewTimer) {
        clearTimeout(this._overviewTimer)
        this._overviewTimer = null
      }
      clearSessionCache()
      this.hydratedFromCache = false
      this.authenticated = false
      this.overview = null
      this.overviewLoaded = false
      this.jobs = []
      this.jobsLoaded = false
      this.jobsLoadedAt = 0
      this.jobsFetchedLimit = 0
      this._jobsRequestedLimit = 0
      this.tempFiles = []
      this.tempFilesLoaded = false
      this.tempFilesLoadedAt = 0
      this.targetsCache = null
      this.subscriptionsCache = {}
      this.subscriptionsLastSource = ''
      this.targetsLoadedAt = 0
      this.settingsCache = null
      this.settingsLoadedAt = 0
      this.historyCache = null
      this.historyLoadedAt = 0
      this.jobItems = {}
      this.liveProgress = {}
      this.logs = []
      this.logsLoaded = false
    },

    async logout() {
      this.stopRealtime()
      resetInflight()
      try {
        await api.logout()
      } catch {}
      this._resetSessionState()
    },

    handleUnauthorized() {
      const wasAuth = this.authenticated
      this.stopRealtime()
      resetInflight()
      this._resetSessionState()
      if (!wasAuth) return
    },

    _scheduleOverviewRefresh(delayMs = 350) {
      if (!this.authenticated || this._overviewTimer) return
      this._overviewTimer = setTimeout(() => {
        this._overviewTimer = null
        if (this.authenticated) this.refreshOverview()
      }, delayMs)
    },

    getCachedSubscriptions(username = '') {
      const source = username.trim() || this.settingsCache?.ph_logged_in_username || this.subscriptionsLastSource
      const entry = this.subscriptionsCache[source.toLowerCase()]
      const fresh = entry && Date.now() - entry.fetchedAt < SUBSCRIPTIONS_CACHE_TTL_MS
      if (!fresh || !Array.isArray(entry.subscriptions)) return null
      // Older subscription entries predate avatar_url and need one fresh pull.
      return entry.subscriptions.every((sub) => typeof sub?.avatar_url === 'string') ? entry : null
    },

    clearSubscriptionsCache() {
      this.subscriptionsCache = {}
      this.subscriptionsLastSource = ''
      this.persistCache()
    },

    async loadSubscriptions(username = '', force = false) {
      if (!username.trim() && this.settingsCache === null) await this.ensureSettings()
      const source = username.trim() || this.settingsCache?.ph_logged_in_username || this.subscriptionsLastSource
      const cached = this.getCachedSubscriptions(source)
      if (cached && !force) return { ...cached, fromCache: true }
      const key = `subscriptions:${source.toLowerCase()}`
      if (inflight.has(key)) return inflight.get(key)
      return runRequest(key, async ({ isLatest }) => {
        const res = await api.subscriptions(source)
        const entry = { username: res.username || source, subscriptions: res.subscriptions || [], fetchedAt: Date.now() }
        if (isLatest()) {
          const cache = { ...this.subscriptionsCache, [entry.username.toLowerCase()]: entry }
          this.subscriptionsCache = Object.fromEntries(Object.entries(cache)
            .filter(([, value]) => Date.now() - value.fetchedAt < SUBSCRIPTIONS_CACHE_TTL_MS)
            .sort((a, b) => b[1].fetchedAt - a[1].fetchedAt).slice(0, 3))
          this.subscriptionsLastSource = entry.username
          this.persistCache()
        }
        return { ...entry, fromCache: false }
      })
    },

    async refreshOverview() {
      if (!this.authenticated) return null
      return runRequest('overview', async ({ isLatest }) => {
        try {
          const data = await api.overview()
          if (isLatest()) {
            this.overview = data
            this.overviewLoaded = true
            this.persistCache()
          }
          return data
        } catch {
          if (isLatest()) {
            this.overviewLoaded = true
          }
          return null
        }
      })
    },

    ensureOverview() {
      if (!this.authenticated) return Promise.resolve()
      return ensureRequest('overview', () => this.overviewLoaded, async ({ isLatest }) => {
        try {
          const data = await api.overview()
          if (isLatest()) {
            this.overview = data
            this.overviewLoaded = true
            this.persistCache()
          }
        } catch {
          if (isLatest()) {
            this.overviewLoaded = true
          }
        }
      })
    },

    async refreshJobs(limit = this.jobsLimit) {
      if (!this.authenticated) return []
      limit = Math.max(1, Math.min(200, Math.trunc(Number(limit)) || this.jobsLimit))
      this._jobsRequestedLimit = limit
      return runRequest('jobs', async ({ isLatest }) => {
        try {
          const res = await api.listJobs(limit)
          const items = res.items || []
          if (isLatest()) {
            this.jobs = items
            this.jobsLoaded = true
            this.jobsLoadedAt = Date.now()
            this.jobsFetchedLimit = limit
            this.persistCache()
          }
          return items
        } catch {
          if (isLatest()) {
            this.jobsLoaded = true
          }
          return this.jobs
        }
      })
    },

    ensureJobs(limit = this.jobsLimit) {
      if (!this.authenticated) return Promise.resolve()
      if (this.jobsLoaded && this.jobsFetchedLimit >= limit) return Promise.resolve(this.jobs)
      if (inflight.has('jobs') && this._jobsRequestedLimit === limit) return inflight.get('jobs')
      return this.refreshJobs(limit)
    },

    async refreshTempFiles() {
      if (!this.authenticated) return []
      return runRequest('tempFiles', async ({ isLatest }) => {
        try {
          const res = await api.listTempFiles()
          const items = res.items || []
          if (isLatest()) {
            this.tempFiles = items
            this.tempFilesLoaded = true
            this.tempFilesLoadedAt = Date.now()
            this.persistCache()
          }
          return items
        } catch {
          if (isLatest()) {
            this.tempFilesLoaded = true
          }
          return this.tempFiles
        }
      })
    },

    ensureTempFiles() {
      if (!this.authenticated) return Promise.resolve()
      return ensureRequest('tempFiles', () => this.tempFilesLoaded, async ({ isLatest }) => {
        try {
          const res = await api.listTempFiles()
          if (isLatest()) {
            this.tempFiles = res.items || []
            this.tempFilesLoaded = true
            this.tempFilesLoadedAt = Date.now()
            this.persistCache()
          }
        } catch {
          if (isLatest()) {
            this.tempFilesLoaded = true
          }
        }
      })
    },

    setTargetsCache(items) {
      if (!Array.isArray(items)) return
      this.targetsCache = [...items]
      this.targetsLoadedAt = Date.now()
      this.persistCache()
    },

    async refreshTargets() {
      if (!this.authenticated) return []
      return runRequest('targets', async ({ isLatest }) => {
        const res = await api.listTargets()
        const items = res.items || []
        if (isLatest()) {
          this.setTargetsCache(items)
        }
        return items
      })
    },

    ensureTargets() {
      if (!this.authenticated) return Promise.resolve()
      return ensureRequest('targets', () => this.targetsCache !== null, async ({ isLatest }) => {
        try {
          const res = await api.listTargets()
          if (isLatest()) {
            this.setTargetsCache(res.items || [])
          }
        } catch {}
      })
    },

    setSettingsCache(settings) {
      if (!settings) return
      if (this.settingsCache && settings.ph_logged_in_username !== this.settingsCache.ph_logged_in_username) this.clearSubscriptionsCache()
      this.settingsCache = { ...settings }
      this.settingsLoadedAt = Date.now()
      this.persistCache()
    },

    async refreshSettings() {
      if (!this.authenticated) return null
      return runRequest('settings', async ({ isLatest }) => {
        const res = await api.getSettings()
        const settings = res.settings || null
        if (isLatest() && settings) {
          this.setSettingsCache(settings)
        }
        return settings
      })
    },

    ensureSettings() {
      if (!this.authenticated) return Promise.resolve()
      return ensureRequest('settings', () => this.settingsCache !== null, async ({ isLatest }) => {
        try {
          const res = await api.getSettings()
          if (isLatest() && res.settings) {
            this.setSettingsCache(res.settings)
          }
        } catch {}
      })
    },

    setHistoryCache(data) {
      if (!data) return
      const pageSize = Number(data.page_size || this.historyPageSize)
      if (pageSize !== this.historyPageSize) return
      this.historyCache = {
        items: data.items || [],
        total: Number(data.total || 0),
        facets: data.facets || { uploaders: [], qualities: [] },
        stats: data.stats || { total: 0, uploaded: 0, downloaded: 0, baseline: 0 },
        page_size: pageSize,
      }
      this.historyLoadedAt = Date.now()
      this.persistCache()
    },

    async refreshHistory(filters = this.defaultHistoryFilters()) {
      if (!this.authenticated) return null
      return runRequest('history', async ({ isLatest }) => {
        const res = await api.listVideos(filters)
        if (isLatest()) {
          this.setHistoryCache(res)
        }
        return res
      })
    },

    ensureHistory() {
      if (!this.authenticated) return Promise.resolve()
      return ensureRequest('history', () => this.hasRouteCache('history'), async ({ isLatest }) => {
        try {
          const res = await api.listVideos(this.defaultHistoryFilters())
          if (isLatest()) {
            this.setHistoryCache(res)
          }
        } catch {}
      })
    },

    _applyLogsSnapshot(incoming) {
      if (!Array.isArray(incoming)) return
      if (incoming.length === 0) {
        if (this.logs.length > 0) this.logs = []
        this.logsLoaded = true
        this.persistCache()
        return
      }
      if (
        this.logs.length === incoming.length &&
        this.logs[0]?.id === incoming[0]?.id &&
        this.logs[this.logs.length - 1]?.id === incoming[incoming.length - 1]?.id
      ) {
        this.logsLoaded = true
        this.persistCache()
        return
      }
      const map = new Map()
      for (const item of incoming) {
        if (item && item.id != null) map.set(item.id, item)
      }
      const maxIncoming = incoming.reduce((m, x) => Math.max(m, Number(x?.id || 0)), 0)
      for (const cur of this.logs) {
        if (cur && cur.id != null && Number(cur.id) > maxIncoming && !map.has(cur.id)) {
          map.set(cur.id, cur)
        }
      }
      this.logs = Array.from(map.values())
        .sort((a, b) => Number(a.id) - Number(b.id))
        .slice(-500)
      this.logsLoaded = true
      this.persistCache()
    },

    async refreshLogs() {
      if (!this.authenticated) return []
      return runRequest('logs', async ({ isLatest }) => {
        try {
          const res = await api.getLogs()
          if (isLatest()) {
            this._applyLogsSnapshot(res.items || [])
          }
          return this.logs
        } catch {
          if (isLatest()) {
            this.logsLoaded = true
          }
          return this.logs
        }
      })
    },

    ensureLogs() {
      if (!this.authenticated) return Promise.resolve()
      return ensureRequest('logs', () => this.logsLoaded, async ({ isLatest }) => {
        try {
          const res = await api.getLogs()
          if (isLatest()) {
            this._applyLogsSnapshot(res.items || [])
          }
        } catch {
          if (isLatest()) {
            this.logsLoaded = true
          }
        }
      })
    },

    async ensureRouteData(routeName) {
      if (!this.authenticated) return
      const tasks = [this.ensureOverview()]
      if (routeName === 'download') {
        tasks.push(this.ensureTempFiles(), this.ensureJobs())
      } else if (routeName === 'jobs') {
        tasks.push(this.ensureJobs(), this.ensureTargets())
      } else if (routeName === 'targets') {
        tasks.push(this.ensureTargets(), this.ensureSettings())
      } else if (routeName === 'history') {
        tasks.push(this.ensureHistory())
      } else if (routeName === 'logs') {
        tasks.push(this.ensureLogs())
      } else if (routeName === 'settings') {
        tasks.push(this.ensureSettings())
      }
      await Promise.all(tasks)

      if (
        (routeName === 'download' || routeName === 'jobs') &&
        this.overview?.current_job?.id
      ) {
        await this.ensureJobDetail(this.overview.current_job.id)
      }
    },

    prefetchAll() {
      if (!this.authenticated) return
      // Non-blocking background warm-up so switching to any sidebar tab is instant with zero loading flash
      Promise.allSettled([
        this.ensureOverview(),
        this.ensureJobs(),
        this.ensureTempFiles(),
        this.ensureTargets(),
        this.ensureSettings(),
        this.ensureHistory(),
        this.ensureLogs(),
      ])
    },

    removeJobs(jobIds) {
      const idSet = new Set(jobIds.map((x) => Number(x)))
      this.jobs = this.jobs.filter((j) => !idSet.has(Number(j.id)))
      for (const jid of idSet) {
        delete this.jobItems[jid]
        delete this.schedulerStatus[jid]
      }
      this.persistCache()
    },

    async loadJobDetail(jobId) {
      const res = await api.getJob(jobId)
      if (res?.job) {
        this._upsertJob(res.job)
      }
      if (Array.isArray(res?.items)) {
        this.jobItems[jobId] = res.items
        for (const it of res.items) {
          if (it.speed_bps > 0 || it.status === 'downloading' || it.status === 'uploading') {
            const isUploading = it.status === 'uploading' || ['uploading', 'verifying'].includes(it.progress_phase)
            this.liveProgress[it.id] = {
              job_id: Number(jobId),
              item_id: it.id,
              bytes_downloaded: isUploading ? Number(it.upload_bytes || 0) : Number(it.bytes_downloaded || 0),
              bytes_total: Number(it.bytes_total || 0),
              speed_bps: Number(it.speed_bps || 0),
              phase: it.progress_phase || (isUploading ? 'uploading' : 'downloading'),
            }
          }
        }
        if (Number(jobId) === Number(this.overview?.current_job?.id)) {
          this.persistCache()
        }
      }
      return res
    },

    ensureJobDetail(jobId) {
      if (!this.authenticated || !jobId) return Promise.resolve()
      return ensureRequest(
        `jobDetail:${jobId}`,
        () => Array.isArray(this.jobItems[jobId]),
        async () => {
          try {
            await this.loadJobDetail(jobId)
          } catch {}
        }
      )
    },

    _upsertJob(job) {
      const idx = this.jobs.findIndex((j) => j.id === job.id)
      if (idx >= 0) {
        this.jobs[idx] = { ...this.jobs[idx], ...job }
      } else {
        this.jobs.unshift(job)
      }
      if (this.jobs.length > this.jobsLimit) {
        this.jobs = this.jobs.slice(0, this.jobsLimit)
        this.jobsFetchedLimit = Math.min(this.jobsFetchedLimit, this.jobsLimit)
      }
      this.persistCache()
    },

    _upsertItem(item) {
      if (item.status !== 'downloading' && item.status !== 'uploading') {
        delete this.liveProgress[item.id]
      }
      const list = this.jobItems[item.job_id]
      if (!list) return
      const idx = list.findIndex((x) => x.id === item.id)
      if (idx >= 0) {
        list[idx] = { ...list[idx], ...item }
      } else {
        list.push(item)
      }
    },

    startRealtime() {
      if (this._wsClient) return
      this._wsOpenedOnce = false
      if (this._wsGraceTimer) {
        clearTimeout(this._wsGraceTimer)
        this._wsGraceTimer = null
      }
      this._wsGraceTimer = setTimeout(() => {
        this._wsGraceTimer = null
        if (!this._wsOpenedOnce) {
          this.wsConnected = false
        }
      }, 2000)
      this._wsClient = new EventStreamClient({
        onStatusChange: (ok) => {
          if (this._wsGraceTimer) {
            clearTimeout(this._wsGraceTimer)
            this._wsGraceTimer = null
          }
          const reconnected = ok && this._wsOpenedOnce && !this.wsConnected
          if (ok) this._wsOpenedOnce = true
          this.wsConnected = ok
          if (reconnected && this.authenticated) {
            this.refreshOverview()
            if (this.jobsLoaded) this.refreshJobs()
          }
        },
        onUnauthorized: () => {
          this.handleUnauthorized()
          if (typeof window !== 'undefined' && window.location.pathname !== '/login') {
            window.location.assign('/login')
          }
        },
        onEvent: (msg) => {
          if (msg.type === 'snapshot') {
            this._applyLogsSnapshot(msg.data?.logs || [])
            this.liveProgress = msg.data?.live_progress || {}
            this.schedulerStatus = msg.data?.scheduler_status || {}
          } else if (msg.type === 'log') {
            const entry = msg.data
            if (entry && (entry.id == null || !this.logs.some((x) => x.id === entry.id))) {
              this.logs.push(entry)
              if (this.logs.length > 500) this.logs.shift()
            }
            this.logsLoaded = true
          } else if (msg.type === 'job_updated') {
            this._upsertJob(msg.data)
            this._scheduleOverviewRefresh()
            if (['completed', 'cancelled', 'failed', 'partial_failed'].includes(msg.data?.status)) {
              delete this.schedulerStatus[msg.data.id]
              this.refreshTempFiles()
              this.historyLoadedAt = 0
              if (this.historyCache !== null) {
                this.refreshHistory().catch(() => {})
              }
            }
          } else if (msg.type === 'item_updated') {
            this._upsertItem(msg.data)
          } else if (msg.type === 'item_progress') {
            this.liveProgress[msg.data.item_id] = msg.data
          } else if (msg.type === 'job_scheduler') {
            const jid = Number(msg.data?.job_id)
            if (jid > 0) {
              if (msg.data?.cleared) {
                delete this.schedulerStatus[jid]
              } else {
                this.schedulerStatus[jid] = msg.data
              }
            }
          } else if (msg.type === 'overview_changed') {
            this._scheduleOverviewRefresh()
          }
        },
      })
      this._wsClient.connect()
    },

    stopRealtime() {
      if (this._wsGraceTimer) {
        clearTimeout(this._wsGraceTimer)
        this._wsGraceTimer = null
      }
      this._wsOpenedOnce = false
      if (this._wsClient) {
        this._wsClient.disconnect()
        this._wsClient = null
      }
      this.wsConnected = false
    },

    async clearLogs() {
      await api.clearLogs()
      this.logs = []
      this.persistCache()
    },
  },
})
