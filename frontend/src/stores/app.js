import { markRaw } from 'vue'
import { defineStore } from 'pinia'
import { toast } from 'vue-sonner'
import { api } from '../api/client.js'
import { EventStreamClient } from '../api/ws.js'
import { appendLiveLog, mergeLogsSnapshot } from '../utils/logBuffer.js'
import { JOB_LIMITS, HISTORY_PAGE_SIZES, readListPreference, saveListPreference } from '../utils/listPreferences.js'

export const DEFAULT_HISTORY_FILTERS = Object.freeze({
  site: '',
  search: '',
  uploader: '',
  status: '',
  quality: '',
  time_range: '',
  sort_by: 'activity',
  page: 1,
  page_size: 30,
})

const SESSION_CACHE_KEY = 'phdl-session-cache-v1'
const SESSION_CACHE_TTL_MS = 6 * 60 * 60 * 1000
const SUBSCRIPTIONS_CACHE_TTL_MS = 30 * 60 * 1000
const LIVE_PROGRESS_FLUSH_MS = 140
const ITEM_UPDATE_FLUSH_MS = 120
const JOB_ITEMS_RELOAD_DEBOUNCE_MS = 240
const JOB_DETAILS_HISTORY_LIMIT = 8
const SESSION_CACHE_MAX_CHARS = 256 * 1024
const JOB_RUN_ORDER = { queued: 0, scanning: 1, running: 2 }

const inflight = new Map()
let sessionEpoch = 0
let persistTimer = null
let persistIdleId = null
let persistGen = 0
let lastSnapshot = ''
let pendingLiveProgress = null
let liveProgressFlushTimer = null
let pendingItemUpdates = null
let itemUpdateFlushTimer = null
const jobItemIndexMap = new Map()
const jobItemsReloadTimers = new Map()
const jobItemAccessOrder = new Map()
const jobItemPins = new Map()
const detailUpdates = new Map()

function isActiveJobStatus(status) {
  return JOB_RUN_ORDER[status] !== undefined
}

function jobListPriority(status) {
  if (status === 'queued') return 1
  return isActiveJobStatus(status) ? 0 : 2
}

function compareJobListOrder(a, b) {
  return jobListPriority(a.status) - jobListPriority(b.status) ||
    String(b.finished_at || b.created_at || '').localeCompare(String(a.finished_at || a.created_at || '')) || b.id - a.id
}

function isOlderJobUpdate(job, current) {
  if (!current) return false
  if (Number.isInteger(job.revision) && Number.isInteger(current.revision)) {
    return job.revision < current.revision
  }
  const incomingRun = Number(job.params?.resume_count || 0)
  const currentRun = Number(current.params?.resume_count || 0)
  return incomingRun < currentRun || (
    incomingRun === currentRun && (JOB_RUN_ORDER[job.status] ?? 3) < (JOB_RUN_ORDER[current.status] ?? 3)
  )
}

function touchJobItems(jobId, status = jobItemAccessOrder.get(jobId)) {
  jobItemAccessOrder.delete(jobId)
  jobItemAccessOrder.set(jobId, status)
}

function updateCachedJobStatus(store, job, previousJob) {
  const jid = Number(job.id)
  if (!jobItemAccessOrder.has(jid)) return
  const previous = jobItemAccessOrder.get(jid)
  jobItemAccessOrder.set(jid, job.status)
  if (Number(job.params?.resume_count || 0) > Number(previousJob?.params?.resume_count || 0) ||
      (!isActiveJobStatus(previous) && isActiveJobStatus(job.status))) {
    pendingItemUpdates?.delete(jid)
    clearLiveProgressForJob(store, jid)
    scheduleJobItemsReload(store, jid)
  }
}

function evictJobItems(store, jid) {
  delete store.jobItems[jid]
  jobItemAccessOrder.delete(jid)
  jobItemIndexMap.delete(jid)
  detailUpdates.delete(jid)
  inflight.delete(`jobDetail:${jid}`)
  pendingItemUpdates?.delete(jid)
  clearTimeout(jobItemsReloadTimers.get(jid))
  jobItemsReloadTimers.delete(jid)
  clearLiveProgressForJob(store, jid)
}

function trimJobItemsCache(store) {
  const inactive = []
  for (const [jid, status] of jobItemAccessOrder) {
    if (!isActiveJobStatus(status) && !jobItemPins.has(jid)) inactive.push(jid)
  }
  for (let i = 0; i < inactive.length - JOB_DETAILS_HISTORY_LIMIT; i++) {
    evictJobItems(store, inactive[i])
  }
}

function indexJobItems(jobId, items) {
  const jid = Number(jobId)
  if (!jid || !Array.isArray(items)) return
  const map = new Map()
  for (let i = 0; i < items.length; i++) {
    map.set(items[i].id, i)
  }
  jobItemIndexMap.set(jid, map)
}

function clearPendingItemUpdates() {
  if (itemUpdateFlushTimer !== null) {
    clearTimeout(itemUpdateFlushTimer)
    itemUpdateFlushTimer = null
  }
  pendingItemUpdates = null
  detailUpdates.clear()
  jobItemIndexMap.clear()
  for (const timer of jobItemsReloadTimers.values()) {
    clearTimeout(timer)
  }
  jobItemsReloadTimers.clear()
}

function flushPendingItemUpdates(store) {
  if (itemUpdateFlushTimer !== null) {
    clearTimeout(itemUpdateFlushTimer)
    itemUpdateFlushTimer = null
  }
  if (!pendingItemUpdates || pendingItemUpdates.size === 0) return
  const batchByJob = pendingItemUpdates
  pendingItemUpdates = null
  for (const [jid, updatesById] of batchByJob) {
    const list = store.jobItems[jid]
    if (!Array.isArray(list)) continue
    let idxMap = jobItemIndexMap.get(jid)
    if (!idxMap || idxMap.size !== list.length) {
      indexJobItems(jid, list)
      idxMap = jobItemIndexMap.get(jid)
    }
    const nextList = list.slice()
    for (const [itemId, item] of updatesById) {
      const idx = idxMap.get(itemId)
      if (idx !== undefined && idx < nextList.length) {
        if (!(item.revision < nextList[idx].revision)) nextList[idx] = { ...nextList[idx], ...item }
      } else {
        idxMap.set(itemId, nextList.length)
        nextList.push(item)
      }
    }
    store.jobItems[jid] = markRaw(nextList)
  }
}

function queueItemUpdate(store, item) {
  if (!item || item.id == null || item.job_id == null) return
  if (item.status !== 'downloading' && item.status !== 'uploading') {
    pendingLiveProgress?.delete(item.id)
    delete store.liveProgress[item.id]
  }
  const jid = Number(item.job_id)
  detailUpdates.get(jid)?.set(item.id, item)
  if (!Array.isArray(store.jobItems[jid])) return
  if (!pendingItemUpdates) pendingItemUpdates = new Map()
  let jobMap = pendingItemUpdates.get(jid)
  if (!jobMap) {
    jobMap = new Map()
    pendingItemUpdates.set(jid, jobMap)
  }
  jobMap.set(item.id, item)
  if (itemUpdateFlushTimer === null) {
    itemUpdateFlushTimer = setTimeout(() => flushPendingItemUpdates(store), ITEM_UPDATE_FLUSH_MS)
  }
}

function scheduleJobItemsReload(store, jobId) {
  const jid = Number(jobId)
  if (!jid || !store.authenticated || (!Array.isArray(store.jobItems[jid]) && !jobItemPins.has(jid))) return
  if (jobItemsReloadTimers.has(jid)) return
  const timer = setTimeout(async () => {
    const pending = inflight.get(`jobDetail:${jid}`)
    if (pending) await pending.catch(() => {})
    if (jobItemsReloadTimers.get(jid) !== timer) return
    jobItemsReloadTimers.delete(jid)
    if (!store.authenticated || (!Array.isArray(store.jobItems[jid]) && !jobItemPins.has(jid))) return
    store.loadJobDetail(jid).catch(() => {})
  }, JOB_ITEMS_RELOAD_DEBOUNCE_MS)
  jobItemsReloadTimers.set(jid, timer)
}

function clearPendingLiveProgress() {
  if (liveProgressFlushTimer !== null) {
    clearTimeout(liveProgressFlushTimer)
    liveProgressFlushTimer = null
  }
  pendingLiveProgress = null
}

function clearLiveProgressForJob(store, jobId) {
  const jid = Number(jobId)
  if (!jid) return
  if (pendingLiveProgress) {
    for (const [itemId, payload] of pendingLiveProgress) {
      if (Number(payload?.job_id) === jid) {
        pendingLiveProgress.delete(itemId)
      }
    }
  }
  for (const [itemId, payload] of Object.entries(store.liveProgress || {})) {
    if (Number(payload?.job_id) === jid) {
      delete store.liveProgress[itemId]
    }
  }
}

function flushPendingLiveProgress(store) {
  if (liveProgressFlushTimer !== null) {
    clearTimeout(liveProgressFlushTimer)
    liveProgressFlushTimer = null
  }
  if (!pendingLiveProgress) return
  const batch = pendingLiveProgress
  pendingLiveProgress = null
  for (const [itemId, payload] of batch) {
    store.liveProgress[itemId] = payload
  }
}

function queueLiveProgress(store, payload) {
  if (!payload || payload.item_id == null) return
  if (!pendingLiveProgress) pendingLiveProgress = new Map()
  pendingLiveProgress.set(payload.item_id, payload)
  if (liveProgressFlushTimer === null) {
    liveProgressFlushTimer = setTimeout(() => flushPendingLiveProgress(store), LIVE_PROGRESS_FLUSH_MS)
  }
}

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
    if (!parsed || parsed.v !== 2 || !parsed.authenticated || Date.now() - Number(parsed.ts || 0) > SESSION_CACHE_TTL_MS) {
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
  lastSnapshot = ''
  cancelScheduledPersist()
  try {
    window.sessionStorage.removeItem(SESSION_CACHE_KEY)
  } catch {}
}

function cachedJobSummary(job) {
  if (!job) return null
  const { params, params_json, export_text, error_message, ...summary } = job
  return {
    ...summary,
    label: String(job.label || '').slice(0, 512),
    params: {
      resume_count: params?.resume_count,
      site: params?.site,
      export_only: params?.export_only,
      target_type: params?.target_type,
    },
  }
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
      const snapshot = {
        overview: store.overview ? { ...store.overview, current_job: cachedJobSummary(store.overview.current_job) } : null,
        overviewLoaded: Boolean(store.overview),
        jobs: store.jobsLoaded ? store.jobs.slice(0, store.jobsLimit).map(cachedJobSummary) : null,
        jobsFetchedLimit: Math.min(store.jobsFetchedLimit, store.jobsLimit),
      }
      const serialized = JSON.stringify(snapshot)
      if (serialized.length > SESSION_CACHE_MAX_CHARS || serialized === lastSnapshot) return
      window.sessionStorage.setItem(SESSION_CACHE_KEY, JSON.stringify({ v: 2, ts: Date.now(), authenticated: true, ...snapshot }))
      lastSnapshot = serialized
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
  }, 1200)
}

function resetInflight() {
  sessionEpoch += 1
  inflight.clear()
}

function runRequest(key, fn) {
  const epoch = sessionEpoch
  const isLatest = () => epoch === sessionEpoch && inflight.get(key) === p
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
      rcloneRemotesCache:
        hasCache && Array.isArray(cached.rcloneRemotesCache) ? cached.rcloneRemotesCache : null,
      historyCache: hasCache && cached.historyCache ? cached.historyCache : null,
      historyLoadedAt: 0,
      historyPageSize: readListPreference('history', HISTORY_PAGE_SIZES),
      jobItems: {},
      liveProgress: {},
      schedulerStatus: {},
      scanStatus: {},
      logs: hasCache && Array.isArray(cached.logs) ? cached.logs : [],
      logsLoaded: Boolean(hasCache && Array.isArray(cached.logs)),
      _wsClient: null,
      _wsOpenedOnce: false,
      _wsGraceTimer: null,
      _overviewTimer: null,
    }
  },

  getters: {
    accountUsername(state) {
      return String(
        state.settingsCache?.ph_logged_in_username ||
        state.overview?.account?.username ||
        state.settingsCache?.ph_username ||
        ''
      ).trim()
    },
    xhAccountUsername(state) {
      return String(
        state.settingsCache?.xh_logged_in_username ||
        state.overview?.xh_cookie?.username ||
        ''
      ).trim()
    },
    accountAvatarUrl(state) {
      if (state.settingsCache && typeof state.settingsCache.account_avatar_version === 'string') {
        return state.settingsCache.account_avatar_version
          ? `/api/settings/avatar?v=${state.settingsCache.account_avatar_version}`
          : ''
      }
      return String(state.overview?.account?.avatar_url || '')
    },
  },

  actions: {
    hasRouteCache(routeName) {
      if (!this.overviewLoaded) return false
      if (routeName === 'download') {
        return this.tempFilesLoaded && this.jobsLoaded && this.targetsCache !== null
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
      clearPendingLiveProgress()
      clearPendingItemUpdates()
      jobItemAccessOrder.clear()
      jobItemPins.clear()
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
      this.rcloneRemotesCache = null
      this.historyCache = null
      this.historyLoadedAt = 0
      this.jobItems = {}
      this.liveProgress = {}
      this.schedulerStatus = {}
      this.scanStatus = {}
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

    getCachedSubscriptions(username = '', site = 'pornhub') {
      const normSite = String(site || 'pornhub').toLowerCase() === 'xhamster' ? 'xhamster' : 'pornhub'
      const source =
        normSite === 'xhamster'
          ? 'self'
          : (username.trim() || this.settingsCache?.ph_logged_in_username || this.subscriptionsLastSource)
      const cacheKey = normSite === 'xhamster' ? 'xhamster:self' : source.toLowerCase()
      const entry = this.subscriptionsCache[cacheKey]
      const fresh = entry && Date.now() - entry.fetchedAt < SUBSCRIPTIONS_CACHE_TTL_MS
      if (!fresh || !Array.isArray(entry.subscriptions)) return null
      // Older subscription entries predate avatar_url or xHamster bound_username and need one fresh pull.
      if (!entry.subscriptions.every((sub) => typeof sub?.avatar_url === 'string')) return null
      if (normSite === 'xhamster' && !entry.subscriptions.every((sub) => typeof sub?.bound_username === 'string')) {
        return null
      }
      return entry
    },

    clearSubscriptionsCache() {
      this.subscriptionsCache = {}
      this.subscriptionsLastSource = ''
      this.persistCache()
    },

    async loadSubscriptions(username = '', force = false, site = 'pornhub') {
      const normSite = String(site || 'pornhub').toLowerCase() === 'xhamster' ? 'xhamster' : 'pornhub'
      if (normSite === 'pornhub' && !username.trim() && this.settingsCache === null) await this.ensureSettings()
      const source =
        normSite === 'xhamster'
          ? ''
          : (username.trim() || this.settingsCache?.ph_logged_in_username || this.subscriptionsLastSource)
      const cached = this.getCachedSubscriptions(source, normSite)
      if (cached && !force) return { ...cached, fromCache: true }
      const cacheKey = normSite === 'xhamster' ? 'xhamster:self' : source.toLowerCase()
      const key = `subscriptions:${normSite}:${cacheKey}`
      if (inflight.has(key)) return inflight.get(key)
      return runRequest(key, async ({ isLatest }) => {
        const res = await api.subscriptions(source, normSite)
        const entry = {
          site: normSite,
          username: res.username || source,
          subscriptions: res.subscriptions || [],
          fetchedAt: Date.now(),
        }
        if (isLatest()) {
          const storeKey = normSite === 'xhamster' ? 'xhamster:self' : entry.username.toLowerCase()
          const cache = { ...this.subscriptionsCache, [storeKey]: entry }
          this.subscriptionsCache = Object.fromEntries(Object.entries(cache)
            .filter(([, value]) => Date.now() - value.fetchedAt < SUBSCRIPTIONS_CACHE_TTL_MS)
            .sort((a, b) => b[1].fetchedAt - a[1].fetchedAt).slice(0, 4))
          if (normSite === 'pornhub') {
            this.subscriptionsLastSource = entry.username
          }
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
      if (inflight.has('jobs') && this._jobsRequestedLimit === limit) return inflight.get('jobs')
      this._jobsRequestedLimit = limit
      return runRequest('jobs', async ({ isLatest }) => {
        try {
          const before = new Map(this.jobs.map((job) => [job.id, job]))
          const res = await api.listJobs(limit)
          const items = res.items || []
          if (isLatest()) {
            const current = new Map(this.jobs.map((job) => [job.id, job]))
            const merged = new Map(items.map((job) => [job.id, isOlderJobUpdate(job, current.get(job.id)) ? current.get(job.id) : job]))
            for (const job of current.values()) {
              if (!merged.has(job.id) && before.get(job.id) !== job) merged.set(job.id, job)
            }
            this.jobs = [...merged.values()].sort(compareJobListOrder).slice(0, limit)
            for (const job of this.jobs) updateCachedJobStatus(this, job, current.get(job.id))
            trimJobItemsCache(this)
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
      if (this.overview) {
        this.overview = {
          ...this.overview,
          account: {
            username: (settings.ph_logged_in_username || settings.ph_username || this.overview.account?.username || '').trim(),
            avatar_url: settings.account_avatar_version
              ? `/api/settings/avatar?v=${settings.account_avatar_version}`
              : '',
          },
        }
      }
      this.persistCache()
    },

    async uploadAccountAvatar(dataUrl) {
      const res = await api.uploadAccountAvatar(dataUrl)
      if (res?.settings) {
        this.setSettingsCache(res.settings)
      } else if (this.overview) {
        this.overview = {
          ...this.overview,
          account: {
            ...(this.overview.account || {}),
            avatar_url: res?.avatar_url || '',
          },
        }
        this.persistCache()
      }
      return res
    },

    async removeAccountAvatar() {
      const res = await api.deleteAccountAvatar()
      if (res?.settings) {
        this.setSettingsCache(res.settings)
      } else if (this.overview) {
        this.overview = {
          ...this.overview,
          account: {
            ...(this.overview.account || {}),
            avatar_url: '',
          },
        }
        this.persistCache()
      }
      return res
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

    setRcloneRemotesCache(remotes) {
      this.rcloneRemotesCache = Array.isArray(remotes) ? remotes : []
      this.persistCache()
    },

    async refreshRcloneRemotes() {
      if (!this.authenticated) return []
      return runRequest('rclone_remotes', async ({ isLatest }) => {
        const res = await api.listRcloneRemotes()
        const remotes = Array.isArray(res?.remotes) ? res.remotes : []
        if (isLatest()) {
          this.setRcloneRemotesCache(remotes)
        }
        return remotes
      })
    },

    ensureRcloneRemotes() {
      if (!this.authenticated) return Promise.resolve()
      return ensureRequest(
        'rclone_remotes',
        () => this.rcloneRemotesCache !== null,
        async ({ isLatest }) => {
          try {
            const res = await api.listRcloneRemotes()
            if (isLatest()) {
              this.setRcloneRemotesCache(Array.isArray(res?.remotes) ? res.remotes : [])
            }
          } catch {}
        }
      )
    },

    setHistoryCache(data) {
      if (!data) return
      const pageSize = Number(data.page_size || this.historyPageSize)
      if (pageSize !== this.historyPageSize) return
      this.historyCache = {
        items: data.items || [],
        total: Number(data.total || 0),
        facets: data.facets || { uploaders: [], qualities: [] },
        stats: data.stats || { total: 0, uploaded: 0, downloaded: 0, baseline: 0, published_7d: 0 },
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
      const merged = mergeLogsSnapshot(this.logs, incoming, 500)
      if (merged !== this.logs) {
        this.logs = merged
      }
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
        tasks.push(this.ensureTempFiles(), this.ensureJobs(), this.ensureTargets())
      } else if (routeName === 'jobs') {
        tasks.push(this.ensureJobs(), this.ensureTargets())
      } else if (routeName === 'targets') {
        tasks.push(this.ensureTargets(), this.ensureSettings())
      } else if (routeName === 'history') {
        tasks.push(this.ensureHistory())
      } else if (routeName === 'logs') {
        tasks.push(this.ensureLogs())
      } else if (routeName === 'settings') {
        tasks.push(this.ensureSettings(), this.ensureRcloneRemotes())
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
        this.ensureRcloneRemotes(),
        this.ensureHistory(),
        this.ensureLogs(),
      ])
    },

    removeJobs(jobIds) {
      const idSet = new Set(jobIds.map((x) => Number(x)))
      this.jobs = this.jobs.filter((j) => !idSet.has(Number(j.id)))
      for (const jid of idSet) {
        evictJobItems(this, jid)
        inflight.delete(`jobDetail:${jid}`)
        delete this.schedulerStatus[jid]
      }
      this.persistCache()
    },

    async loadJobDetail(jobId) {
      const jid = Number(jobId)
      const key = `jobDetail:${jid}`
      if (inflight.has(key)) return inflight.get(key)
      clearTimeout(jobItemsReloadTimers.get(jid))
      jobItemsReloadTimers.delete(jid)
      return runRequest(key, async ({ isLatest }) => {
        const updates = new Map()
        detailUpdates.set(jid, updates)
        let res
        try {
          res = await api.getJob(jid)
        } finally {
          if (detailUpdates.get(jid) === updates) detailUpdates.delete(jid)
        }
        if (!isLatest()) return res
        const current = this.jobs.find((job) => job.id === jid)
        if (res?.job && Number(res.job.params?.resume_count || 0) < Number(current?.params?.resume_count || 0)) {
          scheduleJobItemsReload(this, jid)
          return res
        }
        if (res?.job) this._upsertJob(res.job)
        if (Array.isArray(res?.items)) {
          pendingItemUpdates?.delete(jid)
          const rows = new Map(res.items.map((item) => [item.id, item]))
          for (const item of updates.values()) {
            if (!(item.revision < rows.get(item.id)?.revision)) rows.set(item.id, item)
          }
          const items = [...rows.values()]
          this.jobItems[jid] = markRaw(items)
          indexJobItems(jid, items)
          const status = this.jobs.find((job) => job.id === jid)?.status || res.job?.status
          touchJobItems(jid, status)
          for (const it of isActiveJobStatus(status) ? items : []) {
            if (!it.speed_bps && it.status !== 'downloading' && it.status !== 'uploading') continue
            const isUploading = it.status === 'uploading' || ['uploading', 'verifying'].includes(it.progress_phase)
            this.liveProgress[it.id] = {
              job_id: jid,
              item_id: it.id,
              bytes_downloaded: isUploading ? Number(it.upload_bytes || 0) : Number(it.bytes_downloaded || 0),
              bytes_total: Number(it.bytes_total || 0),
              speed_bps: Number(it.speed_bps || 0),
              phase: it.progress_phase || (isUploading ? 'uploading' : 'downloading'),
            }
          }
          trimJobItemsCache(this)
          if (jid === Number(this.overview?.current_job?.id)) this.persistCache()
        }
        return res
      })
    },

    pinJobDetail(jobId) {
      const jid = Number(jobId)
      jobItemPins.set(jid, (jobItemPins.get(jid) || 0) + 1)
      this.touchJobDetail(jid)
      return () => {
        const count = jobItemPins.get(jid)
        if (count > 1) jobItemPins.set(jid, count - 1)
        else jobItemPins.delete(jid)
        trimJobItemsCache(this)
      }
    },

    touchJobDetail(jobId) {
      const jid = Number(jobId)
      if (Array.isArray(this.jobItems[jid])) touchJobItems(jid)
    },

    ensureJobDetail(jobId) {
      if (!this.authenticated || !jobId) return Promise.resolve()
      if (Array.isArray(this.jobItems[jobId])) {
        this.touchJobDetail(jobId)
        return Promise.resolve()
      }
      return inflight.get(`jobDetail:${jobId}`) || this.loadJobDetail(jobId).catch(() => {})
    },

    _upsertJob(job) {
      const idx = this.jobs.findIndex((j) => j.id === job.id)
      let orderChanged = idx < 0
      const previousJob = idx >= 0 ? this.jobs[idx] : undefined
      if (idx >= 0) {
        const cur = this.jobs[idx]
        if (isOlderJobUpdate(job, cur)) return false
        const unchanged =
          cur.status === job.status &&
          cur.total_items === job.total_items &&
          cur.done_items === job.done_items &&
          cur.uploaded_items === job.uploaded_items &&
          cur.failed_items === job.failed_items &&
          cur.skipped_items === job.skipped_items &&
          cur.error_message === job.error_message &&
          cur.started_at === job.started_at &&
          cur.finished_at === job.finished_at &&
          cur.label === job.label &&
          cur.export_text === job.export_text &&
          cur.params_json === job.params_json &&
          cur.revision === job.revision
        if (unchanged) return
        orderChanged = jobListPriority(cur.status) !== jobListPriority(job.status) || cur.finished_at !== job.finished_at
        this.jobs[idx] = { ...cur, ...job }
      } else {
        this.jobs.unshift(job)
      }
      updateCachedJobStatus(this, job, previousJob)
      trimJobItemsCache(this)
      if (orderChanged) {
        this.jobs.sort(compareJobListOrder)
      }
      if (this.jobs.length > this.jobsLimit) {
        this.jobs = this.jobs.slice(0, this.jobsLimit)
        this.jobsFetchedLimit = Math.min(this.jobsFetchedLimit, this.jobsLimit)
      }
      this.persistCache()
    },

    _upsertItem(item) {
      queueItemUpdate(this, item)
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
            for (const [jid, status] of jobItemAccessOrder) {
              if (isActiveJobStatus(status) || jobItemPins.has(jid)) scheduleJobItemsReload(this, jid)
            }
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
            clearPendingLiveProgress()
            this._applyLogsSnapshot(msg.data?.logs || [])
            this.liveProgress = msg.data?.live_progress || {}
            this.schedulerStatus = msg.data?.scheduler_status || {}
            this.scanStatus = msg.data?.scan_status || {}
          } else if (msg.type === 'log') {
            const entry = msg.data
            const nextLogs = appendLiveLog(this.logs, entry, 500)
            if (nextLogs !== this.logs) {
              this.logs = nextLogs
            }
            this.logsLoaded = true
          } else if (msg.type === 'job_updated') {
            if (this._upsertJob(msg.data) === false) return
            this._scheduleOverviewRefresh()
            if (['completed', 'cancelled', 'failed', 'partial_failed'].includes(msg.data?.status)) {
              flushPendingItemUpdates(this)
              clearLiveProgressForJob(this, msg.data.id)
              delete this.schedulerStatus[msg.data.id]
              delete this.scanStatus[msg.data.id]
              this.refreshTempFiles()
              this.targetsLoadedAt = 0
              if (this.targetsCache !== null) {
                this.refreshTargets().catch(() => {})
              }
              this.historyLoadedAt = 0
              if (this.historyCache !== null) {
                this.refreshHistory().catch(() => {})
              }
            }
          } else if (msg.type === 'item_updated') {
            this._upsertItem(msg.data)
          } else if (msg.type === 'job_items_changed') {
            scheduleJobItemsReload(this, msg.data?.job_id)
          } else if (msg.type === 'item_progress') {
            queueLiveProgress(this, msg.data)
          } else if (msg.type === 'job_scheduler') {
            const jid = Number(msg.data?.job_id)
            if (jid > 0) {
              if (msg.data?.cleared) {
                delete this.schedulerStatus[jid]
              } else {
                this.schedulerStatus[jid] = msg.data
              }
            }
          } else if (msg.type === 'job_scan') {
            const jid = Number(msg.data?.job_id)
            if (jid > 0) {
              if (msg.data?.cleared) {
                delete this.scanStatus[jid]
              } else {
                this.scanStatus[jid] = msg.data
              }
            }
          } else if (msg.type === 'overview_changed') {
            this._scheduleOverviewRefresh()
          } else if (msg.type === 'targets_changed') {
            this.targetsLoadedAt = 0
            this.refreshTargets().catch(() => {})
            this._scheduleOverviewRefresh()
          } else if (msg.type === 'subscriptions_changed') {
            this.clearSubscriptionsCache()
          }
        },
      })
      this._wsClient.connect()
    },

    stopRealtime() {
      clearPendingLiveProgress()
      clearPendingItemUpdates()
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
