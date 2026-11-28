import { markRaw } from 'vue'
import { defineStore } from 'pinia'
import { toast } from 'vue-sonner'
import type { RouteRecordName } from 'vue-router'
import { api } from '../api/client'
import { EventStreamClient } from '../api/ws'
import type {
  JobDetailQuery,
  JobDetailResponse,
  JobItemPageMeta,
  JobItemRecord,
  JobRecord,
  JobStatus,
  JobStatusCounts,
  LiveProgressEntry,
  LogEntry,
  OverviewResponse,
  RcloneRemote,
  RuntimeSettings,
  ScanStatusEntry,
  SchedulerStatus,
  SiteKey,
  SubscriptionsResponse,
  TargetRecord,
  TempFileEntry,
  VideoFilters,
  VideoListResponse,
} from '../types/api'
import { appendLiveLog, mergeLogsSnapshot } from '../utils/logBuffer'
import { JOB_LIMITS, HISTORY_PAGE_SIZES, readListPreference, saveListPreference } from '../utils/listPreferences'
import {
  broadcastSessionLogout, readLogoutTimestamp, readSessionCache, serializeSessionCache, SESSION_CACHE_KEY, type HistoryCache,
} from './sessionCache'

type RouteName = RouteRecordName | null | undefined
type Timer = ReturnType<typeof setTimeout>
type NoticeType = 'info' | 'success' | 'error' | 'warning'
type SubscriptionsCacheEntry = SubscriptionsResponse & { fetchedAt: number }
type SubscriptionsResult = SubscriptionsCacheEntry & { fromCache: boolean }
type ItemUpdates = Map<number, Map<number, JobItemRecord>>

interface AppState {
  authChecked: boolean
  hydratedFromCache: boolean
  authenticated: boolean
  _cacheSuspended: boolean
  _logoutTimestamp: number
  wsConnected: boolean
  overview: OverviewResponse | null
  overviewLoaded: boolean
  jobs: JobRecord[]
  jobsLoaded: boolean
  jobsLoadedAt: number
  jobsLimit: number
  jobsFetchedLimit: number
  _jobsRequestedLimit: number
  tempFiles: TempFileEntry[]
  tempFilesLoaded: boolean
  tempFilesLoadedAt: number
  targetsCache: TargetRecord[] | null
  subscriptionsCache: Record<string, SubscriptionsCacheEntry>
  subscriptionsLastSource: string
  targetsLoadedAt: number
  settingsCache: RuntimeSettings | null
  settingsLoadedAt: number
  rcloneRemotesCache: RcloneRemote[] | null
  historyCache: HistoryCache | null
  historyLoadedAt: number
  historyPageSize: number
  jobItems: Partial<Record<number, JobItemRecord[]>>
  jobItemMeta: Partial<Record<number, JobItemPageMeta>>
  liveProgress: Partial<Record<string, LiveProgressEntry>>
  schedulerStatus: Partial<Record<number, SchedulerStatus>>
  scanStatus: Partial<Record<number, ScanStatusEntry>>
  cancellingJobIds: Partial<Record<number, true>>
  logs: LogEntry[]
  logsLoaded: boolean
  _wsClient: Pick<EventStreamClient, 'connect' | 'disconnect'> | null
  _wsOpenedOnce: boolean
  _wsGraceTimer: Timer | null
  _overviewTimer: Timer | null
}

interface RequestResults {
  revalidate: boolean
  overview: OverviewResponse | null | void
  jobs: JobRecord[]
  tempFiles: TempFileEntry[] | void
  targets: TargetRecord[] | void
  settings: RuntimeSettings | null | void
  rclone_remotes: RcloneRemote[] | void
  history: VideoListResponse | void
  logs: LogEntry[] | void
  [key: `jobDetail:${number}`]: JobDetailResponse
  [key: `subscriptions:${SiteKey}:${string}`]: SubscriptionsResult
}

type RequestHandler<T> = (context: { isLatest: () => boolean }) => Promise<T>
type JobDetailCache = Pick<AppState, 'jobItems' | 'jobItemMeta' | 'liveProgress' | 'authenticated'> & {
  loadJobDetail: (jobId: number, query?: JobDetailQuery) => Promise<JobDetailResponse>
}

export const DEFAULT_HISTORY_FILTERS = Object.freeze<VideoFilters>({
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

const SESSION_PERSIST_MS = 300
const SUBSCRIPTIONS_CACHE_TTL_MS = 30 * 60 * 1000
const LIVE_PROGRESS_FLUSH_MS = 140
const ITEM_UPDATE_FLUSH_MS = 120
const JOB_ITEMS_RELOAD_DEBOUNCE_MS = 240
const JOB_ITEMS_SOFT_RELOAD_DEBOUNCE_MS = 900
const JOB_DETAILS_HISTORY_LIMIT = 8
const DEFAULT_JOB_ITEMS_PAGE_SIZE = 50
const JOB_RUN_ORDER: Partial<Record<JobStatus, number>> = { queued: 0, scanning: 1, running: 2 }

const inflight = new Map<keyof RequestResults, Promise<RequestResults[keyof RequestResults]>>()
let sessionEpoch = 0
let persistTimer: Timer | null = null
let pendingPersist: AppState | null = null
let persistWindow: Window | undefined
let lastSnapshot: string | null = null
let pendingLiveProgress: Map<number, LiveProgressEntry> | null = null
let liveProgressFlushTimer: Timer | null = null
let pendingItemUpdates: ItemUpdates | null = null
let itemUpdateFlushTimer: Timer | null = null
const jobItemIndexMap = new Map<number, Map<number, number>>()
const jobItemsReloadTimers = new Map<number, Timer>()
const jobItemsReloadDeadlines = new Map<number, number>()
const jobDetailQueries = new Map<number, Required<JobDetailQuery>>()
const jobDetailInflightQuery = new Map<number, string>()
const jobItemAccessOrder = new Map<number, JobStatus | undefined>()
const jobItemPins = new Map<number, number>()
const detailUpdates: ItemUpdates = new Map()
const detailProgressVersions = new Map<number, Map<number, number>>()
const deletedJobVersions = new Map<number, number>()
let progressVersion = 0
let deletionVersion = 0

function normalizeJobDetailQuery(query?: JobDetailQuery): Required<JobDetailQuery> | undefined {
  if (!query) return undefined
  const rawStatus = String(query.status || '').trim()
  const hasExplicitPagination = query.page != null || query.page_size != null || (Boolean(rawStatus) && rawStatus !== 'all')
  if (!hasExplicitPagination) return undefined
  return {
    page: Math.max(1, Number(query.page || 1)),
    page_size: Math.max(1, Math.min(200, Number(query.page_size || DEFAULT_JOB_ITEMS_PAGE_SIZE))),
    status: rawStatus || 'all',
  }
}

function jobDetailQuerySignature(query?: Required<JobDetailQuery>): string {
  return query ? `${query.page}:${query.page_size}:${query.status}` : 'full'
}

function computeStatusCounts(items: JobItemRecord[]): JobStatusCounts {
  const counts: JobStatusCounts = Object.create(null)
  for (const item of items) {
    const status = String(item?.status || '').trim()
    if (!status) continue
    counts[status] = (counts[status] ?? 0) + 1
  }
  return counts
}

function matchesJobDetailMeta(meta: JobItemPageMeta | undefined, query?: Required<JobDetailQuery>): boolean {
  if (!query) return true
  if (!meta) return false
  if (!meta.paginated) return true
  return (
    meta.status === query.status &&
    meta.page === query.page &&
    meta.page_size === query.page_size
  )
}

function isActiveJobStatus(status: JobStatus | undefined) {
  return status !== undefined && JOB_RUN_ORDER[status] !== undefined
}

function jobListPriority(status: JobStatus) {
  if (status === 'queued') return 1
  return isActiveJobStatus(status) ? 0 : 2
}

function compareJobListOrder(a: JobRecord, b: JobRecord) {
  return jobListPriority(a.status) - jobListPriority(b.status) ||
    String(b.finished_at || b.created_at || '').localeCompare(String(a.finished_at || a.created_at || '')) || b.id - a.id
}

function isOlderJobUpdate(job: JobRecord, current: JobRecord | undefined) {
  if (!current) return false
  const revision = job.revision
  const currentRevision = current.revision
  if (revision !== undefined && currentRevision !== undefined && Number.isInteger(revision) && Number.isInteger(currentRevision)) {
    return revision < currentRevision
  }
  const incomingRun = Number(job.params?.resume_count || 0)
  const currentRun = Number(current.params?.resume_count || 0)
  return incomingRun < currentRun || (
    incomingRun === currentRun && (JOB_RUN_ORDER[job.status] ?? 3) < (JOB_RUN_ORDER[current.status] ?? 3)
  )
}

function isOlderItemUpdate(item: JobItemRecord, current: JobItemRecord | undefined) {
  return item.revision !== undefined && current?.revision !== undefined && item.revision < current.revision
}

function touchJobItems(jobId: number, status = jobItemAccessOrder.get(jobId)) {
  jobItemAccessOrder.delete(jobId)
  jobItemAccessOrder.set(jobId, status)
}

function updateCachedJobStatus(store: JobDetailCache, job: JobRecord, previousJob: JobRecord | undefined) {
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

function evictJobItems(store: JobDetailCache, jid: number) {
  delete store.jobItems[jid]
  delete store.jobItemMeta[jid]
  jobItemAccessOrder.delete(jid)
  jobItemIndexMap.delete(jid)
  jobDetailQueries.delete(jid)
  jobDetailInflightQuery.delete(jid)
  detailUpdates.delete(jid)
  detailProgressVersions.delete(jid)
  inflight.delete(`jobDetail:${jid}`)
  pendingItemUpdates?.delete(jid)
  clearTimeout(jobItemsReloadTimers.get(jid))
  jobItemsReloadTimers.delete(jid)
  jobItemsReloadDeadlines.delete(jid)
  clearLiveProgressForJob(store, jid)
}

function trimJobItemsCache(store: JobDetailCache) {
  const inactive = []
  for (const [jid, status] of jobItemAccessOrder) {
    if (!isActiveJobStatus(status) && !jobItemPins.has(jid)) inactive.push(jid)
  }
  for (let i = 0; i < inactive.length - JOB_DETAILS_HISTORY_LIMIT; i++) {
    evictJobItems(store, inactive[i])
  }
}

function indexJobItems(jobId: number, items: JobItemRecord[]) {
  const jid = Number(jobId)
  if (!jid || !Array.isArray(items)) return
  const map = new Map<number, number>()
  for (let i = 0; i < items.length; i++) {
    map.set(items[i].id, i)
  }
  jobItemIndexMap.set(jid, map)
  return map
}

function clearPendingItemUpdates() {
  if (itemUpdateFlushTimer !== null) {
    clearTimeout(itemUpdateFlushTimer)
    itemUpdateFlushTimer = null
  }
  pendingItemUpdates = null
  detailUpdates.clear()
  detailProgressVersions.clear()
  jobItemIndexMap.clear()
  jobDetailInflightQuery.clear()
  for (const timer of jobItemsReloadTimers.values()) {
    clearTimeout(timer)
  }
  jobItemsReloadTimers.clear()
  jobItemsReloadDeadlines.clear()
}

function flushPendingItemUpdates(store: JobDetailCache) {
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
      idxMap = indexJobItems(jid, list)
    }
    if (!idxMap) continue
    const meta = store.jobItemMeta[jid]
    const isPaginated = Boolean(meta?.paginated)
    const nextList = list.slice()
    let listChanged = false
    let nextCounts: JobStatusCounts | null = null
    let needsFastReload = false
    let needsSoftReload = false

    const adjustStatusCount = (prevStatus: string | undefined, nextStatus: string | undefined) => {
      if (!meta || prevStatus === nextStatus) return
      if (!nextCounts) nextCounts = { ...meta.status_counts }
      if (prevStatus) {
        const dec = Math.max(0, Number(nextCounts[prevStatus] || 0) - 1)
        if (dec > 0) nextCounts[prevStatus] = dec
        else delete nextCounts[prevStatus]
      }
      if (nextStatus) {
        nextCounts[nextStatus] = Number(nextCounts[nextStatus] || 0) + 1
      }
    }

    for (const [itemId, item] of updatesById) {
      const idx = idxMap.get(itemId)
      if (idx !== undefined && idx < nextList.length) {
        const current = nextList[idx]
        if (!isOlderItemUpdate(item, current)) {
          const prevStatus = current.status
          nextList[idx] = { ...current, ...item }
          listChanged = true
          adjustStatusCount(prevStatus, nextList[idx].status)
          if (isPaginated && meta && meta.status !== 'all' && nextList[idx].status !== meta.status) {
            needsFastReload = true
          }
        }
      } else if (!isPaginated) {
        idxMap.set(itemId, nextList.length)
        nextList.push(item)
        listChanged = true
        adjustStatusCount(undefined, item.status)
      } else if (meta) {
        if (
          (meta.status !== 'all' && item.status === meta.status) ||
          (meta.status === 'all' && meta.page === 1 && nextList.length < meta.page_size)
        ) {
          needsFastReload = true
        } else if (jobItemPins.has(jid)) {
          needsSoftReload = true
        }
      }
    }

    if (listChanged) {
      store.jobItems[jid] = markRaw(nextList)
    }
    if (meta && nextCounts) {
      const counts = nextCounts as JobStatusCounts
      const totalAll = Object.values(counts).reduce<number>((sum, n) => sum + Number(n || 0), 0)
      store.jobItemMeta[jid] = {
        ...meta,
        total_all: isPaginated ? Math.max(meta.total_all, totalAll) : nextList.length,
        total: meta.status === 'all'
          ? (isPaginated ? Math.max(meta.total_all, totalAll) : nextList.length)
          : Number(counts[meta.status] || 0),
        status_counts: counts,
      }
    }
    if (needsFastReload) {
      scheduleJobItemsReload(store, jid, JOB_ITEMS_RELOAD_DEBOUNCE_MS)
    } else if (needsSoftReload) {
      scheduleJobItemsReload(store, jid, JOB_ITEMS_SOFT_RELOAD_DEBOUNCE_MS)
    }
  }
}

function queueItemUpdate(store: JobDetailCache, item: JobItemRecord) {
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

function scheduleJobItemsReload(store: JobDetailCache, jobId: number | undefined, delayMs = JOB_ITEMS_RELOAD_DEBOUNCE_MS) {
  const jid = Number(jobId)
  if (!jid || !store.authenticated || (!Array.isArray(store.jobItems[jid]) && !jobItemPins.has(jid))) return
  const targetDeadline = Date.now() + delayMs
  const existingDeadline = jobItemsReloadDeadlines.get(jid)
  if (jobItemsReloadTimers.has(jid)) {
    if (existingDeadline !== undefined && existingDeadline <= targetDeadline) return
    clearTimeout(jobItemsReloadTimers.get(jid))
    jobItemsReloadTimers.delete(jid)
    jobItemsReloadDeadlines.delete(jid)
  }
  const timer = setTimeout(async () => {
    const pending = inflight.get(`jobDetail:${jid}`)
    if (pending) await pending.catch(() => {})
    if (jobItemsReloadTimers.get(jid) !== timer) return
    jobItemsReloadTimers.delete(jid)
    jobItemsReloadDeadlines.delete(jid)
    if (!store.authenticated || (!Array.isArray(store.jobItems[jid]) && !jobItemPins.has(jid))) return
    const query = jobDetailQueries.get(jid)
    ;(query ? store.loadJobDetail(jid, query) : store.loadJobDetail(jid)).catch(() => {})
  }, delayMs)
  jobItemsReloadTimers.set(jid, timer)
  jobItemsReloadDeadlines.set(jid, targetDeadline)
}

function clearPendingLiveProgress() {
  if (liveProgressFlushTimer !== null) {
    clearTimeout(liveProgressFlushTimer)
    liveProgressFlushTimer = null
  }
  pendingLiveProgress = null
}

function clearLiveProgressForJob(store: Pick<AppState, 'liveProgress'>, jobId: number) {
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

function flushPendingLiveProgress(store: Pick<AppState, 'liveProgress'>) {
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

function queueLiveProgress(store: Pick<AppState, 'liveProgress'>, payload: LiveProgressEntry) {
  if (!payload || payload.item_id == null) return
  detailProgressVersions.get(payload.job_id)?.set(payload.item_id, ++progressVersion)
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
  pendingPersist = null
}

function clearSessionCache() {
  lastSnapshot = null
  cancelScheduledPersist()
  if (typeof window === 'undefined') return
  try {
    window.sessionStorage.removeItem(SESSION_CACHE_KEY)
  } catch {}
}

function flushScheduledPersist() {
  const store = pendingPersist
  cancelScheduledPersist()
  if (!store?.authenticated || store._cacheSuspended || typeof window === 'undefined') return
  if (readLogoutTimestamp() > store._logoutTimestamp) {
    clearSessionCache()
    return
  }
  try {
    const { raw, payload } = serializeSessionCache(store, window.location?.pathname || '/')
    if (payload === lastSnapshot) return
    window.sessionStorage.setItem(SESSION_CACHE_KEY, raw)
    lastSnapshot = payload
  } catch {}
}

function schedulePersistSessionCache(store: AppState) {
  if (!store.authenticated || store._cacheSuspended || typeof window === 'undefined') return
  if (persistWindow !== window && typeof window.addEventListener === 'function') {
    persistWindow?.removeEventListener('pagehide', flushScheduledPersist)
    window.addEventListener('pagehide', flushScheduledPersist)
    persistWindow = window
  }
  pendingPersist = store
  if (persistTimer === null) persistTimer = setTimeout(flushScheduledPersist, SESSION_PERSIST_MS)
}

function resetInflight() {
  sessionEpoch += 1
  inflight.clear()
}

function getInflight<K extends keyof RequestResults>(key: K) {
  // Each request key is written through runRequest with its corresponding result.
  return inflight.get(key) as Promise<RequestResults[K]> | undefined
}

function runRequest<K extends keyof RequestResults, T extends RequestResults[K]>(key: K, fn: RequestHandler<T>): Promise<T> {
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

function ensureRequest<K extends keyof RequestResults>(
  key: K,
  isLoaded: () => boolean,
  fn: RequestHandler<RequestResults[K]>,
): Promise<RequestResults[K] | void> {
  if (isLoaded()) return Promise.resolve()
  const existing = getInflight(key)
  if (existing) return existing
  return runRequest(key, fn)
}

export const useAppStore = defineStore('app', {
  state: (): AppState => {
    const cached = readSessionCache()
    const hasCache = cached !== null
    return {
      authChecked: false,
      hydratedFromCache: hasCache,
      authenticated: hasCache,
      _cacheSuspended: false,
      _logoutTimestamp: readLogoutTimestamp(),
      wsConnected: hasCache,
      overview: cached?.overview ?? null,
      overviewLoaded: Boolean(cached?.overview),
      jobs: hasCache && Array.isArray(cached.jobs) ? cached.jobs : [],
      jobsLoaded: Boolean(hasCache && Array.isArray(cached.jobs)),
      jobsLoadedAt: 0,
      jobsLimit: readListPreference('jobs', JOB_LIMITS),
      jobsFetchedLimit: Array.isArray(cached?.jobs) ? Number(cached.jobsFetchedLimit || 0) : 0,
      _jobsRequestedLimit: 0,
      tempFiles: hasCache && Array.isArray(cached.tempFiles) ? cached.tempFiles : [],
      tempFilesLoaded: Boolean(hasCache && Array.isArray(cached.tempFiles)),
      tempFilesLoadedAt: 0,
      targetsCache: hasCache && Array.isArray(cached.targetsCache) ? cached.targetsCache : null,
      subscriptionsCache: {},
      subscriptionsLastSource: '',
      targetsLoadedAt: 0,
      settingsCache: hasCache && cached.settingsCache ? cached.settingsCache : null,
      settingsLoadedAt: 0,
      rcloneRemotesCache:
        hasCache && Array.isArray(cached.rcloneRemotesCache) ? cached.rcloneRemotesCache : null,
      historyCache: hasCache && cached.historyCache ? cached.historyCache : null,
      historyLoadedAt: 0,
      historyPageSize: readListPreference('history', HISTORY_PAGE_SIZES),
      jobItems: {},
      jobItemMeta: {},
      liveProgress: {},
      schedulerStatus: {},
      scanStatus: {},
      cancellingJobIds: cached?.overview?.cancelling_job_id ? { [cached.overview.cancelling_job_id]: true } : {},
      logs: hasCache && Array.isArray(cached.logs) ? cached.logs : [],
      logsLoaded: Boolean(hasCache && Array.isArray(cached.logs)),
      _wsClient: null,
      _wsOpenedOnce: false,
      _wsGraceTimer: null,
      _overviewTimer: null,
    }
  },

  getters: {
    isJobCancelling: (state) => (jobId?: number | null): boolean =>
      jobId != null && state.cancellingJobIds[jobId] === true,

    accountUsername(state): string {
      return String(
        state.settingsCache?.ph_logged_in_username ||
        state.overview?.account?.username ||
        state.settingsCache?.ph_username ||
        ''
      ).trim()
    },
    xhAccountUsername(state): string {
      return String(
        state.settingsCache?.xh_logged_in_username ||
        state.overview?.xh_cookie?.username ||
        ''
      ).trim()
    },
    accountAvatarUrl(state): string {
      if (state.settingsCache && typeof state.settingsCache.account_avatar_version === 'string') {
        return state.settingsCache.account_avatar_version
          ? `/api/settings/avatar?v=${state.settingsCache.account_avatar_version}`
          : ''
      }
      return String(state.overview?.account?.avatar_url || '')
    },
  },

  actions: {
    hasRouteCache(routeName: RouteName): boolean {
      if (!this.overviewLoaded) return false
      if (routeName === 'download') {
        return this.tempFilesLoaded && this.jobsLoaded && this.targetsCache !== null
      }
      if (routeName === 'jobs') {
        return this.jobsLoaded && this.jobsFetchedLimit >= this.jobsLimit && this.targetsCache !== null
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

    setJobsLimit(value: string | number | null): boolean {
      const limit = Number(value)
      if (!JOB_LIMITS.includes(limit)) return false
      this.jobsLimit = limit
      saveListPreference('jobs', limit)
      this.persistCache()
      return true
    },

    setHistoryPageSize(value: string | number | null): boolean {
      const size = Number(value)
      if (!HISTORY_PAGE_SIZES.includes(size)) return false
      this.historyPageSize = size
      saveListPreference('history', size)
      return true
    },

    defaultHistoryFilters(): VideoFilters {
      return { ...DEFAULT_HISTORY_FILTERS, page_size: this.historyPageSize }
    },

    notify(message: string | undefined, type: NoticeType = 'info') {
      if (!message) return
      if (type === 'success') toast.success(String(message))
      else if (type === 'error') toast.error(String(message))
      else if (type === 'warning') toast.warning(String(message))
      else toast(String(message))
    },

    async checkAuth(initialRouteName: RouteName = null): Promise<boolean> {
      const epoch = sessionEpoch
      let authenticated = false
      try {
        const res = await api.me()
        authenticated = Boolean(res?.authenticated)
      } catch {}
      if (epoch !== sessionEpoch || this.handleCrossTabLogout(readLogoutTimestamp())) return false
      this.authenticated = authenticated
      this.authChecked = true
      this.hydratedFromCache = false
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

    revalidateSession(routeName: RouteName = null): Promise<boolean> {
      const existing = getInflight('revalidate')
      if (existing) return existing
      return runRequest('revalidate', async ({ isLatest }) => {
        let ok = false
        try {
          const res = await api.me()
          ok = Boolean(res?.authenticated)
        } catch {
          ok = false
        }
        if (!isLatest() || this.handleCrossTabLogout(readLogoutTimestamp())) return false
        this.authChecked = true
        if (!ok) {
          this.handleUnauthorized()
          return false
        }
        this.authenticated = true
        this.startRealtime()
        const tasks: Promise<unknown>[] = [this.refreshOverview()]
        if (routeName === 'download') {
          tasks.push(this.refreshTempFiles(), this.refreshJobs(), this.refreshTargets())
        } else if (routeName === 'jobs') {
          tasks.push(this.refreshJobs(), this.refreshTargets())
        } else if (routeName === 'targets') {
          tasks.push(this.refreshTargets(), this.refreshSettings())
        } else if (routeName === 'history') {
          tasks.push(this.refreshHistory())
        } else if (routeName === 'logs') {
          tasks.push(this.refreshLogs())
        } else if (routeName === 'settings') {
          tasks.push(this.refreshSettings(), this.refreshRcloneRemotes())
        }
        await Promise.allSettled(tasks)
        if (!isLatest()) return this.authenticated
        this.hydratedFromCache = false
        if (
          (routeName === 'download' || routeName === 'jobs') &&
          this.overview?.current_job?.id
        ) {
          const curId = this.overview.current_job.id
          const query = jobDetailQueries.get(curId) || { page: 1, page_size: DEFAULT_JOB_ITEMS_PAGE_SIZE, status: 'all' }
          this.loadJobDetail(curId, query).catch(() => {})
        }
        this.persistCache()
        this.prefetchAll()
        return true
      })
    },

    async login(password: string): Promise<void> {
      await api.login(password)
      this._cacheSuspended = false
      this._logoutTimestamp = readLogoutTimestamp()
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
      jobDetailQueries.clear()
      jobDetailInflightQuery.clear()
      deletedJobVersions.clear()
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
      this.jobItemMeta = {}
      this.liveProgress = {}
      this.schedulerStatus = {}
      this.scanStatus = {}
      this.cancellingJobIds = {}
      this.logs = []
      this.logsLoaded = false
    },

    async logout() {
      this._cacheSuspended = true
      clearSessionCache()
      const result = await api.logout()
      if (result?.authenticated !== false) throw new Error('服务端未确认退出登录')
      this.handleUnauthorized()
      this._logoutTimestamp = broadcastSessionLogout()
    },

    handleCrossTabLogout(timestamp: number): boolean {
      if (!Number.isSafeInteger(timestamp) || timestamp <= this._logoutTimestamp) return false
      this._logoutTimestamp = timestamp
      this.handleUnauthorized()
      return true
    },

    handleUnauthorized() {
      this.stopRealtime()
      resetInflight()
      this._resetSessionState()
      this.authChecked = true
    },

    _scheduleOverviewRefresh(delayMs = 350) {
      if (!this.authenticated || this._overviewTimer) return
      this._overviewTimer = setTimeout(() => {
        this._overviewTimer = null
        if (this.authenticated) this.refreshOverview()
      }, delayMs)
    },

    _clearJobTelemetry(jobId: number) {
      clearLiveProgressForJob(this, jobId)
      delete this.schedulerStatus[jobId]
      delete this.scanStatus[jobId]
    },

    markJobCancelling(jobId: number, confirmed = false) {
      this.cancellingJobIds[jobId] = true
      this._clearJobTelemetry(jobId)
      // A reload should restore the server's state even if the original request failed in flight.
      if (confirmed && this.overview?.current_job?.id === jobId && this.overview.cancelling_job_id !== jobId) {
        this.overview = { ...this.overview, cancelling_job_id: jobId }
        this.persistCache()
      }
    },

    clearJobCancelling(jobId: number) {
      delete this.cancellingJobIds[jobId]
      if (this.overview?.cancelling_job_id === jobId) {
        this.overview = { ...this.overview, cancelling_job_id: null }
        this.persistCache()
      }
    },

    _applyOverview(data: OverviewResponse) {
      const previousJobId = this.overview?.current_job?.id
      if (data.cancelling_job_id != null) {
        this.markJobCancelling(data.cancelling_job_id, true)
      }
      const jobId = data.current_job?.id
      this.overview = data.running && jobId != null && this.overview?.cancelling_job_id === jobId
        ? { ...data, cancelling_job_id: jobId }
        : data
      this.overviewLoaded = true
      this.persistCache()
      if (previousJobId !== jobId && this.isJobCancelling(previousJobId)) void this.refreshJobs()
    },

    _clearFinishedJobState(jobId: number) {
      this._clearJobTelemetry(jobId)
      this.clearJobCancelling(jobId)
      if (this.overview?.current_job?.id !== jobId) return false
      this.overview = { ...this.overview, running: false, current_job: null, cancelling_job_id: null }
      this.persistCache()
      if (this._overviewTimer !== null) {
        clearTimeout(this._overviewTimer)
        this._overviewTimer = null
      }
      void this.refreshOverview()
      return true
    },

    getCachedSubscriptions(username = '', site: SiteKey = 'pornhub'): SubscriptionsCacheEntry | null {
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

    async loadSubscriptions(username = '', force = false, site: SiteKey = 'pornhub'): Promise<SubscriptionsResult> {
      const normSite = String(site || 'pornhub').toLowerCase() === 'xhamster' ? 'xhamster' : 'pornhub'
      if (normSite === 'pornhub' && !username.trim() && this.settingsCache === null) await this.ensureSettings()
      const source =
        normSite === 'xhamster'
          ? ''
          : (username.trim() || this.settingsCache?.ph_logged_in_username || this.subscriptionsLastSource)
      const cached = this.getCachedSubscriptions(source, normSite)
      if (cached && !force) return { ...cached, fromCache: true }
      const cacheKey = normSite === 'xhamster' ? 'xhamster:self' : source.toLowerCase()
      const key = `subscriptions:${normSite}:${cacheKey}` as const
      const existing = getInflight(key)
      if (existing) return existing
      return runRequest(key, async ({ isLatest }) => {
        const res = await api.subscriptions(source, normSite)
        const entry: SubscriptionsCacheEntry = {
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

    async refreshOverview(): Promise<OverviewResponse | null> {
      if (!this.authenticated) return null
      return runRequest('overview', async ({ isLatest }) => {
        try {
          const data = await api.overview()
          if (isLatest()) {
            this._applyOverview(data)
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
            this._applyOverview(data)
          }
        } catch {
          if (isLatest()) {
            this.overviewLoaded = true
          }
        }
      })
    },

    async refreshJobs(limit?: number): Promise<JobRecord[]> {
      if (limit === undefined) limit = this.jobsLimit
      if (!this.authenticated) return []
      limit = Math.max(1, Math.min(200, Math.trunc(Number(limit)) || this.jobsLimit))
      const existing = getInflight('jobs')
      if (existing && this._jobsRequestedLimit === limit) return existing
      this._jobsRequestedLimit = limit
      return runRequest('jobs', async ({ isLatest }) => {
        try {
          const before = new Map(this.jobs.map((job) => [job.id, job]))
          const version = deletionVersion
          const res = await api.listJobs(limit)
          const items = (res.items || []).filter((job) => (deletedJobVersions.get(job.id) ?? 0) <= version)
          if (isLatest()) {
            const current = new Map(this.jobs.map((job) => [job.id, job]))
            const merged = new Map(items.map((job) => {
              const latest = current.get(job.id)
              return [job.id, latest && isOlderJobUpdate(job, latest) ? latest : job]
            }))
            for (const job of current.values()) {
              if (!merged.has(job.id) && before.get(job.id) !== job) merged.set(job.id, job)
            }
            this.jobs = [...merged.values()].sort(compareJobListOrder).slice(0, limit)
            for (const job of this.jobs) {
              updateCachedJobStatus(this, job, current.get(job.id))
              if (!isActiveJobStatus(job.status) && (this.isJobCancelling(job.id) || this.overview?.current_job?.id === job.id)) {
                this._clearFinishedJobState(job.id)
              }
            }
            trimJobItemsCache(this)
            this.jobsLoaded = true
            this.jobsLoadedAt = Date.now()
            this.jobsFetchedLimit = limit
            for (const [id, deletedAt] of deletedJobVersions) {
              if (deletedAt <= version) deletedJobVersions.delete(id)
            }
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

    ensureJobs(limit?: number): Promise<JobRecord[] | void> {
      if (limit === undefined) limit = this.jobsLimit
      if (!this.authenticated) return Promise.resolve()
      if (this.jobsLoadedAt > 0 && this.jobsFetchedLimit >= limit) return Promise.resolve(this.jobs)
      const existing = getInflight('jobs')
      if (existing && this._jobsRequestedLimit === limit) return existing
      return this.refreshJobs(limit)
    },

    async refreshTempFiles(): Promise<TempFileEntry[]> {
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

    setTargetsCache(items: TargetRecord[]) {
      if (!Array.isArray(items)) return
      this.targetsCache = [...items]
      this.targetsLoadedAt = Date.now()
      this.persistCache()
    },

    async refreshTargets(): Promise<TargetRecord[]> {
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

    setSettingsCache(settings: RuntimeSettings) {
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

    async uploadAccountAvatar(dataUrl: string) {
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

    async refreshSettings(): Promise<RuntimeSettings | null> {
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

    setRcloneRemotesCache(remotes: RcloneRemote[]) {
      this.rcloneRemotesCache = Array.isArray(remotes) ? remotes : []
      this.persistCache()
    },

    async refreshRcloneRemotes(): Promise<RcloneRemote[]> {
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

    setHistoryCache(data: VideoListResponse) {
      if (!data || data.page !== 1) return
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

    async refreshHistory(filters?: VideoFilters): Promise<VideoListResponse | null> {
      const query = filters === undefined ? this.defaultHistoryFilters() : filters
      if (!this.authenticated) return null
      return runRequest('history', async ({ isLatest }) => {
        const res = await api.listVideos(query)
        if (isLatest() && Object.entries(this.defaultHistoryFilters()).every(([key, value]) =>
          query[key as keyof VideoFilters] === value)) {
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

    _applyLogsSnapshot(incoming: LogEntry[]) {
      if (!Array.isArray(incoming)) return
      const merged = mergeLogsSnapshot(this.logs, incoming, 500)
      if (merged !== this.logs) {
        this.logs = merged
      }
      this.logsLoaded = true
      this.persistCache()
    },

    async refreshLogs(): Promise<LogEntry[]> {
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

    async ensureRouteData(routeName: RouteName): Promise<void> {
      if (!this.authenticated) return
      const tasks: Promise<unknown>[] = [this.ensureOverview()]
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
        const curId = this.overview.current_job.id
        const query = jobDetailQueries.get(curId) || { page: 1, page_size: DEFAULT_JOB_ITEMS_PAGE_SIZE, status: 'all' }
        await this.ensureJobDetail(curId, query)
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

    removeJobs(jobIds: number[]) {
      const idSet = new Set(jobIds.map((x) => Number(x)))
      if (!idSet.size) return
      deletionVersion++
      this.jobs = this.jobs.filter((j) => !idSet.has(Number(j.id)))
      for (const jid of idSet) {
        deletedJobVersions.set(jid, deletionVersion)
        evictJobItems(this, jid)
        inflight.delete(`jobDetail:${jid}`)
        this._clearJobTelemetry(jid)
        this.clearJobCancelling(jid)
      }
      this.persistCache()
    },

    async loadJobDetail(jobId: number, query?: JobDetailQuery): Promise<JobDetailResponse> {
      const jid = Number(jobId)
      const normalizedQuery = normalizeJobDetailQuery(query)
      const querySig = jobDetailQuerySignature(normalizedQuery)
      const key = `jobDetail:${jid}` as const
      const existing = getInflight(key)
      if (existing && jobDetailInflightQuery.get(jid) === querySig) return existing
      clearTimeout(jobItemsReloadTimers.get(jid))
      jobItemsReloadTimers.delete(jid)
      jobItemsReloadDeadlines.delete(jid)
      if (normalizedQuery) jobDetailQueries.set(jid, normalizedQuery)
      else jobDetailQueries.delete(jid)
      jobDetailInflightQuery.set(jid, querySig)
      return runRequest(key, async ({ isLatest }) => {
        const version = progressVersion
        const progressUpdates = new Map<number, number>()
        const updates = new Map<number, JobItemRecord>()
        detailUpdates.set(jid, updates)
        detailProgressVersions.set(jid, progressUpdates)
        let res
        try {
          res = normalizedQuery ? await api.getJob(jid, normalizedQuery) : await api.getJob(jid)
        } finally {
          if (detailUpdates.get(jid) === updates) detailUpdates.delete(jid)
          if (detailProgressVersions.get(jid) === progressUpdates) detailProgressVersions.delete(jid)
          if (isLatest() && jobDetailInflightQuery.get(jid) === querySig) {
            jobDetailInflightQuery.delete(jid)
          }
        }
        if (!isLatest()) return res
        const current = this.jobs.find((job) => job.id === jid)
        if (res?.job && Number(res.job.params?.resume_count || 0) < Number(current?.params?.resume_count || 0)) {
          scheduleJobItemsReload(this, jid)
          return res
        }
        if (res?.job && this._upsertJob(res.job) !== false && !isActiveJobStatus(res.job.status) && this.isJobCancelling(jid)) {
          this._clearFinishedJobState(jid)
        }
        if (Array.isArray(res?.items)) {
          pendingItemUpdates?.delete(jid)
          const isPaginated = Boolean(res.paginated)
          const rows = new Map(res.items.map((item) => [item.id, item]))
          const statusCounts: JobStatusCounts = res.status_counts
            ? { ...res.status_counts }
            : computeStatusCounts(res.items)
          const resStatus = String(res.status || normalizedQuery?.status || 'all').trim() || 'all'
          const resPage = Math.max(1, Number(res.page || normalizedQuery?.page || 1))
          const resPageSize = Math.max(1, Number(res.page_size || normalizedQuery?.page_size || res.items.length || DEFAULT_JOB_ITEMS_PAGE_SIZE))
          let needsFollowupReload = false

          for (const item of updates.values()) {
            const existingRow = rows.get(item.id)
            if (existingRow) {
              if (!isOlderItemUpdate(item, existingRow)) {
                const prevStatus = existingRow.status
                const merged = { ...existingRow, ...item }
                rows.set(item.id, merged)
                if (prevStatus !== merged.status) {
                  if (prevStatus) {
                    const dec = Math.max(0, Number(statusCounts[prevStatus] || 0) - 1)
                    if (dec > 0) statusCounts[prevStatus] = dec
                    else delete statusCounts[prevStatus]
                  }
                  if (merged.status) {
                    statusCounts[merged.status] = Number(statusCounts[merged.status] || 0) + 1
                  }
                  if (isPaginated && resStatus !== 'all' && merged.status !== resStatus) {
                    needsFollowupReload = true
                  }
                }
              }
            } else if (!isPaginated) {
              rows.set(item.id, item)
              if (item.status) {
                statusCounts[item.status] = Number(statusCounts[item.status] || 0) + 1
              }
            } else if (
              (resStatus !== 'all' && item.status === resStatus) ||
              (resStatus === 'all' && resPage === 1 && rows.size < resPageSize)
            ) {
              needsFollowupReload = true
            }
          }

          const items = [...rows.values()]
          const totalAll = typeof res.total_all === 'number'
            ? res.total_all
            : isPaginated
              ? Object.values(statusCounts).reduce<number>((sum, n) => sum + Number(n || 0), 0)
              : items.length
          const filteredTotal = typeof res.total === 'number'
            ? res.total
            : resStatus === 'all'
              ? totalAll
              : Number(statusCounts[resStatus] || 0)

          this.jobItems[jid] = markRaw(items)
          this.jobItemMeta[jid] = {
            paginated: isPaginated,
            page: resPage,
            page_size: resPageSize,
            status: resStatus,
            total: filteredTotal,
            total_all: totalAll,
            status_counts: statusCounts,
          }
          if (isPaginated) {
            jobDetailQueries.set(jid, { page: resPage, page_size: resPageSize, status: resStatus })
          }
          indexJobItems(jid, items)
          const status = this.jobs.find((job) => job.id === jid)?.status || res.job?.status
          touchJobItems(jid, status)
          const canHydrateLive = isActiveJobStatus(status) && !this.isJobCancelling(jid)
          if (canHydrateLive) {
            if (res.live_progress && typeof res.live_progress === 'object') {
              for (const prog of Object.values(res.live_progress)) {
                if (!prog || Number(prog.job_id) !== jid) continue
                const itemId = Number(prog.item_id)
                if (!itemId || (progressUpdates.get(itemId) ?? 0) > version) continue
                this.liveProgress[itemId] = {
                  job_id: jid,
                  item_id: itemId,
                  bytes_downloaded: Number(prog.bytes_downloaded || 0),
                  bytes_total: Number(prog.bytes_total || 0),
                  speed_bps: Number(prog.speed_bps || 0),
                  phase: prog.phase || 'downloading',
                  ...(prog.estimated_total !== undefined ? { estimated_total: Boolean(prog.estimated_total) } : {}),
                }
              }
            }
            for (const it of items) {
              if (!it.speed_bps && it.status !== 'downloading' && it.status !== 'uploading') continue
              if ((progressUpdates.get(it.id) ?? 0) > version) continue
              const isUploading = it.status === 'uploading' || it.progress_phase === 'uploading' || it.progress_phase === 'verifying'
              this.liveProgress[it.id] = {
                job_id: jid,
                item_id: it.id,
                bytes_downloaded: isUploading ? Number(it.upload_bytes || 0) : Number(it.bytes_downloaded || 0),
                bytes_total: Number(it.bytes_total || 0),
                speed_bps: Number(it.speed_bps || 0),
                phase: it.progress_phase || (isUploading ? 'uploading' : 'downloading'),
              }
            }
          }
          trimJobItemsCache(this)
          if (needsFollowupReload) {
            scheduleJobItemsReload(this, jid, JOB_ITEMS_RELOAD_DEBOUNCE_MS)
          }
          if (jid === Number(this.overview?.current_job?.id)) this.persistCache()
        }
        return res
      })
    },

    pinJobDetail(jobId: number): () => void {
      const jid = Number(jobId)
      jobItemPins.set(jid, (jobItemPins.get(jid) || 0) + 1)
      this.touchJobDetail(jid)
      return () => {
        const count = jobItemPins.get(jid)
        if (count !== undefined && count > 1) jobItemPins.set(jid, count - 1)
        else jobItemPins.delete(jid)
        trimJobItemsCache(this)
      }
    },

    touchJobDetail(jobId: number) {
      const jid = Number(jobId)
      if (Array.isArray(this.jobItems[jid])) touchJobItems(jid)
    },

    ensureJobDetail(jobId: number, query?: JobDetailQuery): Promise<JobDetailResponse | void> {
      const jid = Number(jobId)
      if (!this.authenticated || !jid) return Promise.resolve()
      const normalizedQuery = normalizeJobDetailQuery(query)
      if (Array.isArray(this.jobItems[jid]) && matchesJobDetailMeta(this.jobItemMeta[jid], normalizedQuery)) {
        this.touchJobDetail(jid)
        return Promise.resolve()
      }
      const querySig = jobDetailQuerySignature(normalizedQuery)
      const existing = getInflight(`jobDetail:${jid}`)
      if (existing && jobDetailInflightQuery.get(jid) === querySig) return existing
      return (normalizedQuery ? this.loadJobDetail(jid, normalizedQuery) : this.loadJobDetail(jid)).catch(() => {})
    },

    prefetchJobDetail(jobId: number, query?: JobDetailQuery): Promise<JobDetailResponse | void> {
      return this.ensureJobDetail(jobId, query)
    },

    _upsertJob(job: JobRecord): false | void {
      if (deletedJobVersions.has(job.id)) return false
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

    _upsertItem(item: JobItemRecord) {
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
          if ((reconnected || (ok && Object.keys(this.cancellingJobIds).length > 0)) && this.authenticated) {
            this.refreshOverview()
            if (this.jobsLoaded || Object.keys(this.cancellingJobIds).length > 0) this.refreshJobs()
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
            for (const jobId of msg.data?.cancelling_jobs || []) this.markJobCancelling(jobId, true)
            for (const jobId of Object.keys(this.cancellingJobIds)) this._clearJobTelemetry(Number(jobId))
          } else if (msg.type === 'log') {
            const entry = msg.data
            const nextLogs = appendLiveLog(this.logs, entry, 500)
            if (nextLogs !== this.logs) {
              this.logs = nextLogs
            }
            this.logsLoaded = true
            this.persistCache()
          } else if (msg.type === 'job_updated') {
            if (this._upsertJob(msg.data) === false) return
            if (isActiveJobStatus(msg.data.status)) {
              this._scheduleOverviewRefresh()
            } else {
              flushPendingItemUpdates(this)
              if (!this._clearFinishedJobState(msg.data.id)) this._scheduleOverviewRefresh()
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
          } else if (msg.type === 'job_cancelling') {
            const { job_id, cancelling } = msg.data
            if (cancelling) {
              this.markJobCancelling(job_id, true)
            } else if (this.isJobCancelling(job_id)) {
              // The server clears its runtime flag immediately before the terminal job update.
              // Keep the stopping UI latched until that update, avoiding a frame of running state.
              this._scheduleOverviewRefresh()
            }
          } else if (msg.type === 'item_updated') {
            this._upsertItem(msg.data)
          } else if (msg.type === 'job_items_changed') {
            scheduleJobItemsReload(this, msg.data?.job_id)
          } else if (msg.type === 'jobs_deleted') {
            this.removeJobs(msg.data.job_ids)
          } else if (msg.type === 'resync_required') {
            const epoch = sessionEpoch
            void Promise.allSettled([getInflight('jobs'), getInflight('overview')]).then(() => {
              if (this.authenticated && epoch === sessionEpoch) {
                void this.refreshJobs()
                void this.refreshOverview()
              }
            })
            for (const jid of new Set([...jobItemPins.keys(), ...jobItemAccessOrder.keys()])) {
              scheduleJobItemsReload(this, jid)
            }
          } else if (msg.type === 'item_progress') {
            if (!this.isJobCancelling(msg.data.job_id)) queueLiveProgress(this, msg.data)
          } else if (msg.type === 'job_scheduler') {
            const jid = Number(msg.data?.job_id)
            if (jid > 0) {
              if (msg.data?.cleared) {
                delete this.schedulerStatus[jid]
              } else if (!this.isJobCancelling(jid)) {
                this.schedulerStatus[jid] = msg.data
              }
            }
          } else if (msg.type === 'job_scan') {
            const jid = Number(msg.data?.job_id)
            if (jid > 0) {
              if (msg.data?.cleared) {
                delete this.scanStatus[jid]
              } else if (!this.isJobCancelling(jid)) {
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
