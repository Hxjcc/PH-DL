import type {
  JobRecord,
  LogEntry,
  OverviewResponse,
  RcloneRemote,
  RuntimeSettings,
  TargetRecord,
  TempFileEntry,
  VideoFilterFacets,
  VideoListResponse,
  VideoStats,
} from '../types/api'
import { resolveManualUrlSite } from '../utils/jobMeta'
import { redactedSettingsFields } from '../utils/settingsRedaction'
import { parseVideoLinks } from '../utils/videoLinks'

export type HistoryCache = Pick<VideoListResponse, 'items' | 'total' | 'page_size'> & {
  facets: Pick<VideoFilterFacets, 'uploaders' | 'qualities'> & Partial<VideoFilterFacets>
  stats: Omit<VideoStats, 'baseline_missing_metadata'> & Partial<Pick<VideoStats, 'baseline_missing_metadata'>>
}

interface SessionSummaries {
  overview?: OverviewResponse
  jobs?: JobRecord[]
  targetsCache?: TargetRecord[]
  settingsCache?: RuntimeSettings
  tempFiles?: TempFileEntry[]
  rcloneRemotesCache?: RcloneRemote[]
  historyCache?: HistoryCache
  logs?: LogEntry[]
}

interface SessionCache extends SessionSummaries {
  v: 3
  ts: number
  authenticated: true
  jobsFetchedLimit?: number
}

interface SessionCacheSource {
  overview: OverviewResponse | null
  jobs: JobRecord[]
  jobsLoaded: boolean
  jobsLimit: number
  jobsFetchedLimit: number
  targetsCache: TargetRecord[] | null
  settingsCache: RuntimeSettings | null
  tempFiles: TempFileEntry[]
  tempFilesLoaded: boolean
  rcloneRemotesCache: RcloneRemote[] | null
  historyCache: HistoryCache | null
  historyPageSize: number
  logs: LogEntry[]
  logsLoaded: boolean
}

export const SESSION_CACHE_KEY = 'phdl-session-cache-v1'
export const SESSION_CACHE_MAX_CHARS = 256 * 1024
const SESSION_CACHE_TTL_MS = 6 * 60 * 60 * 1000
const SESSION_CACHE_VERSION = 3
const SESSION_LOG_LIMIT = 100

type SummaryKey = keyof SessionSummaries
const CORE_SUMMARIES = ['targetsCache', 'jobs', 'tempFiles', 'settingsCache'] as const
const SECONDARY_SUMMARIES = ['rcloneRemotesCache', 'historyCache', 'logs'] as const
const ROUTE_SUMMARIES: Partial<Record<string, readonly SummaryKey[]>> = {
  '/': ['targetsCache', 'jobs', 'tempFiles'],
  '/jobs': ['targetsCache', 'jobs'],
  '/targets': ['targetsCache', 'settingsCache'],
  '/settings': ['settingsCache', 'rcloneRemotesCache'],
  '/history': ['historyCache'],
  '/logs': ['logs'],
}

function pick<T, K extends keyof T>(source: T, keys: readonly K[]): Pick<T, K> {
  const result = {} as Pick<T, K>
  for (const key of keys) result[key] = source[key]
  return result
}

function isPublicHttpUrl(value: string): boolean {
  if (!/^https?:\/\//i.test(value) || /[#\s\\]/.test(value)) return false
  try {
    const url = new URL(value)
    return !url.username && !url.password
  } catch {
    return false
  }
}

function cachedAvatarUrl(value: string): string {
  return isPublicHttpUrl(value) && !value.includes('?') ? value : ''
}

function cachedVideoPageUrl(value: string): string {
  if (!isPublicHttpUrl(value)) return ''
  const queryStart = value.indexOf('?')
  if (queryStart !== -1 && !/^\?viewkey=[\w-]{1,128}$/.test(value.slice(queryStart))) return ''
  return parseVideoLinks(value).urls[0] || ''
}

function cachedProfileMap(values: Record<string, string> | undefined, sanitize?: (value: string) => string) {
  if (!values) return undefined
  return Object.fromEntries(Object.entries(values)
    .filter(([, value]) => typeof value === 'string')
    .map(([name, value]) => [name, sanitize ? sanitize(value) : value]))
}

function cachedJobSummary(job: JobRecord): JobRecord {
  const params = job.params || {}
  const urls = Array.isArray(params.urls) ? params.urls : undefined
  const count = urls?.length ?? params.url_count
  return {
    ...pick(job, [
      'id', 'kind', 'trigger', 'status', 'created_at', 'started_at', 'finished_at', 'revision',
      'total_items', 'done_items', 'uploaded_items', 'failed_items', 'skipped_items',
    ]),
    label: String(job.label || '').slice(0, 512),
    params: {
      ...pick(params, [
        'target_type', 'target_id', 'quality', 'pages', 'videos', 'redownload', 'auto_upload', 'group_by_uploader',
        'batch_name', 'origin_label', 'source_job_id', 'resume_count',
      ]),
      usernames: Array.isArray(params.usernames) ? params.usernames.filter((name) => typeof name === 'string') : undefined,
      target_ids: Array.isArray(params.target_ids)
        ? params.target_ids.filter(Number.isSafeInteger)
        : params.target_ids === null ? null : undefined,
      export_only: job.export_text ? true : params.export_only,
      profile_names: cachedProfileMap(params.profile_names),
      profile_avatars: cachedProfileMap(params.profile_avatars, cachedAvatarUrl),
      site: job.kind === 'manual_urls' ? resolveManualUrlSite(urls || [], params.site) || undefined : params.site,
      url_count: job.kind === 'manual_urls' && typeof count === 'number' && Number.isSafeInteger(count) && count >= 0
        ? count : undefined,
    },
  }
}

function cachedOverview(overview: OverviewResponse): OverviewResponse {
  const account = overview.account
  return {
    ...pick(overview, [
      'running', 'cancelling_job_id', 'queued_count', 'uploader_enabled', 'targets_total', 'targets_enabled', 'temp_files_count',
    ]),
    current_job: overview.current_job ? cachedJobSummary(overview.current_job) : null,
    account: {
      username: account.username,
      avatar_url: /^\/api\/settings\/avatar\?v=[\w-]+$/.test(account.avatar_url) ? account.avatar_url : '',
    },
    cookie: pick(overview.cookie, ['configured', 'username', 'valid', 'message', 'checked_at']),
    xh_cookie: pick(overview.xh_cookie, ['configured', 'username', 'valid', 'message', 'checked_at']),
    schedule: pick(overview.schedule, ['enabled', 'time', 'scan_mode', 'next_run_at']),
    event_loop: pick(overview.event_loop, ['current_ms', 'avg_ms', 'max_ms', 'warning_count', 'warning_threshold_ms']),
    videos: pick(overview.videos, ['total', 'uploaded', 'downloaded', 'baseline', 'published_7d', 'baseline_missing_metadata']),
    disk: pick(overview.disk, ['free_bytes', 'total_bytes']),
  }
}

function cachedTarget(target: TargetRecord): TargetRecord {
  return {
    ...pick(target, [
      'id', 'site', 'username', 'target_type', 'display_name', 'enabled', 'preferred_quality', 'created_at',
      'last_checked_at', 'last_new_count', 'today_new_date', 'today_new_count', 'video_total_count',
      'video_saved_count', 'video_baseline_count',
    ]),
    avatar_url: cachedAvatarUrl(target.avatar_url),
    last_error: '',
  }
}

function cachedSettings(settings: RuntimeSettings): RuntimeSettings {
  return {
    ...pick(settings, [
      'ph_username', 'ph_logged_in_username', 'xh_username', 'xh_logged_in_username', 'account_avatar_version',
      'has_cookie', 'has_xh_cookie', 'yescaptcha_enabled', 'yescaptcha_ph_task_type', 'yescaptcha_xh_task_type',
      'yescaptcha_timeout_seconds', 'request_timeout', 'max_retries_page_fetch', 'retry_delay_base',
      'retry_delay_max', 'page_delay_min', 'page_delay_max', 'max_concurrent_downloads',
      'max_concurrent_video_details', 'max_concurrent_list_pages', 'download_attempts', 'min_file_size_bytes',
      'preferred_quality', 'xh_preferred_codec', 'upload_batch_size', 'upload_workers', 'max_pending_upload_files',
      'min_free_disk_gb', 'uploader_enabled', 'rclone_bin', 'rclone_remote', 'rclone_backup_remotes',
      'rclone_remote_strategy', 'rclone_remote_base_dir', 'rclone_xh_remote_base_dir', 'rclone_transfers',
      'rclone_checkers', 'rclone_pikpak_upload_concurrency', 'rclone_pikpak_chunk_size', 'rclone_drive_chunk_size',
      'rclone_drive_stop_on_upload_limit', 'rclone_ignore_checksum', 'rclone_ignore_size', 'rclone_verify_download',
      'rclone_tpslimit', 'rclone_tpslimit_burst', 'rclone_log_level', 'rclone_timeout_seconds', 'schedule_enabled',
      'schedule_time', 'update_target_concurrency', 'update_scan_mode', 'notify_if_no_updates', 'serverchan_enabled',
    ]),
    ...redactedSettingsFields(settings),
    selectors: settings.selectors && pick(settings.selectors, [
      'list_page_main_containers', 'list_page_li_in_container', 'list_page_li_fallback',
      'list_page_video_link_tag_primary', 'list_page_video_link_tag_fallback_href', 'list_page_video_image_for_title',
      'list_page_video_span_div_for_title', 'detail_page_h1_titles', 'detail_page_og_title_meta', 'detail_page_title_tag',
      'detail_page_uploader_container', 'detail_page_uploader_link_in_container', 'subs_page_main_container',
      'subs_page_user_link_in_container', 'pornstar_page_video_container_check',
    ]),
  }
}

function cachedHistory(history: HistoryCache): HistoryCache {
  return {
    total: history.total,
    page_size: history.page_size,
    items: history.items.slice(0, history.page_size).map((item) => ({
      ...pick(item, [
        'viewkey', 'site', 'title', 'uploader', 'uploader_display_name', 'target_type', 'quality', 'status',
        'file_size', 'duration_seconds', 'published_at', 'created_at', 'downloaded_at', 'uploaded_at', 'upload_requested',
      ]),
      video_page_url: cachedVideoPageUrl(item.video_page_url),
      local_path: '',
      remote_path: '',
    })),
    facets: pick(history.facets, ['sites', 'uploaders', 'uploader_labels', 'qualities']),
    stats: pick(history.stats, ['total', 'uploaded', 'downloaded', 'baseline', 'published_7d', 'baseline_missing_metadata']),
  }
}

export function readSessionCache(): SessionCache | null {
  if (typeof window === 'undefined') return null
  try {
    const raw = window.sessionStorage.getItem(SESSION_CACHE_KEY)
    if (!raw) return null
    const cached = raw.length < SESSION_CACHE_MAX_CHARS ? JSON.parse(raw) as SessionCache | null : null
    if (cached?.v === SESSION_CACHE_VERSION && cached.authenticated === true &&
        Number.isFinite(cached.ts) && Date.now() - cached.ts <= SESSION_CACHE_TTL_MS) return cached
  } catch {}
  try { window.sessionStorage.removeItem(SESSION_CACHE_KEY) } catch {}
  return null
}

export function serializeSessionCache(store: SessionCacheSource, pathname: string) {
  const history = store.historyCache
  const summaries: SessionSummaries = {
    overview: store.overview ? cachedOverview(store.overview) : undefined,
    jobs: store.jobsLoaded ? store.jobs.slice(0, store.jobsLimit).map(cachedJobSummary) : undefined,
    targetsCache: store.targetsCache?.map(cachedTarget),
    settingsCache: store.settingsCache ? cachedSettings(store.settingsCache) : undefined,
    tempFiles: store.tempFilesLoaded ? store.tempFiles.map((file) =>
      pick(file, ['name', 'folder', 'site', 'viewkey', 'size_bytes', 'verified_checkpoint'])) : undefined,
    rcloneRemotesCache: store.rcloneRemotesCache?.map((remote) => pick(remote, ['name', 'type'])),
    historyCache: history?.page_size === store.historyPageSize ? cachedHistory(history) : undefined,
    // The API and event stream redact diagnostics before they reach this store.
    logs: store.logsLoaded ? store.logs.slice(-SESSION_LOG_LIMIT).map((entry) =>
      pick(entry, ['id', 'boot_id', 'ts', 'level', 'job_id', 'message'])) : undefined,
  }
  const active = ROUTE_SUMMARIES[pathname.replace(/\/+$/, '') || '/'] || []
  const orderedKeys: SummaryKey[] = [
    'overview',
    ...CORE_SUMMARIES.filter((key) => active.includes(key)),
    ...CORE_SUMMARIES.filter((key) => !active.includes(key)),
    ...SECONDARY_SUMMARIES.filter((key) => active.includes(key)),
    ...SECONDARY_SUMMARIES.filter((key) => !active.includes(key)),
  ]

  // Budget the complete serialized value, including the envelope and field names.
  const header = `{"v":${SESSION_CACHE_VERSION},"ts":${Date.now()},"authenticated":true`
  let size = header.length + 1
  const parts: Partial<Record<SummaryKey, string>> = {}
  for (const key of orderedKeys) {
    if (summaries[key] === undefined) continue
    const limit = key === 'jobs' ? `,"jobsFetchedLimit":${Math.min(store.jobsFetchedLimit, store.jobsLimit)}` : ''
    let part = `,"${key}":${JSON.stringify(summaries[key])}${limit}`
    if (size + part.length >= SESSION_CACHE_MAX_CHARS && key === 'overview' && summaries.overview?.current_job) {
      part = `,"overview":${JSON.stringify({ ...summaries.overview, current_job: null })}`
    }
    if (size + part.length >= SESSION_CACHE_MAX_CHARS) continue
    parts[key] = part
    size += part.length
  }
  const payload = (['overview', ...CORE_SUMMARIES, ...SECONDARY_SUMMARIES] as const)
    .map((key) => parts[key] || '').join('')
  return { payload, raw: `${header}${payload}}` }
}
