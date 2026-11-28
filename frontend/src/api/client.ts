import type {
  BatchCreateTargetsRequest,
  CookieCheckResult,
  CreateJobRequest,
  CreateTargetRequest,
  JobDetailQuery,
  JobDetailResponse,
  JobRecord,
  LoginOpStatus,
  LogEntry,
  OverviewResponse,
  PlaybackResolution,
  RcloneRemote,
  RuntimeSettings,
  SettingsPatch,
  SettingsResponse,
  SiteKey,
  SubscriptionsResponse,
  TargetRecord,
  TempFileEntry,
  UpdateTargetRequest,
  VideoFilters,
  VideoListResponse,
} from '../types/api'

type RequestOptions = Omit<RequestInit, 'body'> & { body?: object | string | null }
type ItemsResponse<T> = { items: T[] }
type JobResponse = { job: JobRecord }
type AuthResponse = { authenticated: boolean }
type AvatarResponse = { avatar_url: string; account_avatar_version: string; settings: RuntimeSettings }
type ToolResult = { ok: boolean; message: string }

let unauthorizedHandler: (() => void) | null = null

export function setUnauthorizedHandler(fn: (() => void) | null): void {
  unauthorizedHandler = fn
}

const GATEWAY_HINTS: Partial<Record<number, string>> = {
  502: '网关错误（502）',
  503: '服务暂不可用（503）',
  504: '网关超时（504）',
}

function formatHttpError(status: number, text: unknown): string {
  const body = String(text || '').trim()
  const looksLikeHtml = /^<!doctype html/i.test(body) || /^<html[\s>]/i.test(body) || /<center>/i.test(body)
  if (looksLikeHtml) {
    const hint = GATEWAY_HINTS[status]
    if (hint) {
      return `${hint}：反向代理等待后端响应超时。登录可能仍在后台继续，请稍后刷新状态；如频繁出现，请把反向代理的 proxy_read_timeout 调到 600s 以上。`
    }
    return `服务器返回了网页而不是接口数据（HTTP ${status}），请检查反向代理是否正确转发 /api 请求。`
  }
  return body || `请求失败 (${status})`
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, ...init } = options
  const opts: RequestInit = {
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
    ...init,
  }
  if ('body' in options) {
    opts.body = body && typeof body !== 'string' ? JSON.stringify(body) : body
  }

  const resp = await fetch(path, opts)
  let data: unknown = null
  const text = await resp.text()
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = { detail: text }
    }
  }

  if (resp.status === 401) {
    if (path === '/api/auth/logout') return { authenticated: false } as T
    if (unauthorizedHandler && !path.startsWith('/api/auth/')) {
      unauthorizedHandler()
    }
    const error = data as { detail?: unknown } | null
    throw new Error(formatHttpError(resp.status, error?.detail || text) || '未登录或会话已过期')
  }

  if (!resp.ok) {
    const error = data as { detail?: unknown; message?: unknown } | null
    throw new Error(formatHttpError(resp.status, error?.detail || error?.message || text))
  }
  return data as T
}

export const api = {
  // Auth
  me: (): Promise<AuthResponse> => request('/api/auth/me'),
  login: (password: string): Promise<AuthResponse> => request('/api/auth/login', { method: 'POST', body: { password } }),
  logout: (): Promise<AuthResponse> => request('/api/auth/logout', { method: 'POST' }),

  // Overview & Subscriptions
  overview: (): Promise<OverviewResponse> => request('/api/overview'),
  subscriptions: (username = '', site: SiteKey = 'pornhub'): Promise<SubscriptionsResponse> => {
    const sp = new URLSearchParams()
    if (username) sp.set('username', username)
    if (site && site !== 'pornhub') sp.set('site', site)
    const qs = sp.toString() ? `?${sp.toString()}` : ''
    return request(`/api/subscriptions${qs}`, { cache: 'no-store' })
  },

  // Targets
  listTargets: (): Promise<ItemsResponse<TargetRecord>> => request('/api/targets'),
  createTarget: (payload: CreateTargetRequest): Promise<{ target: TargetRecord; baseline_job: JobRecord | null }> =>
    request('/api/targets', { method: 'POST', body: payload }),
  batchCreateTargets: (payload: BatchCreateTargetsRequest): Promise<{
    created_count: number; targets: TargetRecord[]; baseline_jobs: JobRecord[]; skipped_channels: string[]
  }> => request('/api/targets/batch', { method: 'POST', body: payload }),
  updateTarget: (id: number, payload: UpdateTargetRequest): Promise<{ target: TargetRecord }> =>
    request(`/api/targets/${id}`, { method: 'PATCH', body: payload }),
  deleteTarget: (id: number): Promise<{ deleted: true }> => request(`/api/targets/${id}`, { method: 'DELETE' }),
  syncTargetBaseline: (id: number, pages = ''): Promise<JobResponse> => {
    const qs = pages ? `?pages=${encodeURIComponent(pages)}` : ''
    return request(`/api/targets/${id}/sync-baseline${qs}`, { method: 'POST' })
  },
  runUpdate: (payload: { target_ids?: number[] | null; auto_download?: boolean }): Promise<JobResponse> =>
    request('/api/targets/run-update', { method: 'POST', body: payload }),

  // Jobs
  listJobs: (limit = 30): Promise<ItemsResponse<JobRecord>> => request(`/api/jobs?limit=${limit}`),
  createJob: (payload: CreateJobRequest): Promise<JobResponse> => request('/api/jobs', { method: 'POST', body: payload }),
  getJob: (id: number, params?: JobDetailQuery): Promise<JobDetailResponse> => {
    if (!params || (params.page == null && params.page_size == null && (!params.status || params.status === 'all'))) {
      return request(`/api/jobs/${id}`)
    }
    const sp = new URLSearchParams()
    if (params.page != null) sp.set('page', String(params.page))
    if (params.page_size != null) sp.set('page_size', String(params.page_size))
    if (params.status && params.status !== 'all') sp.set('status', params.status)
    const qs = sp.toString()
    return request(`/api/jobs/${id}${qs ? `?${qs}` : ''}`)
  },
  cancelJob: (id: number): Promise<{ cancelled: true }> => request(`/api/jobs/${id}/cancel`, { method: 'POST' }),
  retryFailedJob: (id: number): Promise<JobResponse> => request(`/api/jobs/${id}/retry-failed`, { method: 'POST' }),
  deleteJob: (id: number): Promise<{ deleted: true; id: number }> => request(`/api/jobs/${id}`, { method: 'DELETE' }),
  batchDeleteJobs: (jobIds: number[]): Promise<{
    deleted_count: number; deleted_ids: number[]; skipped_active_ids: number[]
  }> =>
    request('/api/jobs/batch-delete', { method: 'POST', body: { job_ids: jobIds } }),
  listTempFiles: (): Promise<ItemsResponse<TempFileEntry>> => request('/api/jobs/temp-files'),
  resumeTempFiles: (): Promise<JobResponse> => request('/api/jobs/temp-files/resume', { method: 'POST' }),
  clearTempFiles: (): Promise<{ removed: number }> => request('/api/jobs/temp-files', { method: 'DELETE' }),

  // Videos History
  listVideos: (params: Partial<VideoFilters> = {}, options: { signal?: AbortSignal } = {}): Promise<VideoListResponse> => {
    const sp = new URLSearchParams()
    for (const [k, v] of Object.entries(params)) {
      if (v !== '' && v !== null && v !== undefined) sp.set(k, String(v))
    }
    const reqOpts = options?.signal ? { signal: options.signal } : {}
    return request(`/api/videos?${sp.toString()}`, reqOpts)
  },
  videoPlayback: (viewkey: string, signal?: AbortSignal): Promise<PlaybackResolution> =>
    request(`/api/videos/${encodeURIComponent(viewkey)}/playback`, { signal, cache: 'no-store' }),
  deleteVideo: (viewkey: string, deleteLocalFile = false): Promise<{ deleted: true; deleted_local_file: boolean }> =>
    request(
      `/api/videos/${encodeURIComponent(viewkey)}${deleteLocalFile ? '?delete_local_file=true' : ''}`,
      { method: 'DELETE' }
    ),
  batchDeleteVideos: (viewkeys: string[], deleteLocalFiles = false): Promise<{
    deleted_count: number; deleted_viewkeys: string[]; deleted_files_count: number
  }> =>
    request('/api/videos/batch-delete', {
      method: 'POST',
      body: { viewkeys, delete_local_files: Boolean(deleteLocalFiles) },
    }),
  redownloadVideo: (viewkey: string): Promise<JobResponse> =>
    request(`/api/videos/${encodeURIComponent(viewkey)}/redownload`, { method: 'POST' }),
  enrichBaselineMetadata: (site: SiteKey | '' = ''): Promise<JobResponse & { count: number }> =>
    request('/api/videos/metadata/enrich', { method: 'POST', body: { site } }),

  // Settings & Tools
  getSettings: (): Promise<SettingsResponse> => request('/api/settings'),
  saveSettings: (patch: SettingsPatch): Promise<SettingsResponse> => request('/api/settings', { method: 'PUT', body: patch }),
  uploadAccountAvatar: (dataUrl: string): Promise<AvatarResponse> =>
    request('/api/settings/avatar', { method: 'POST', body: { data_url: dataUrl } }),
  deleteAccountAvatar: (): Promise<AvatarResponse> => request('/api/settings/avatar', { method: 'DELETE' }),
  testCookie: (): Promise<CookieCheckResult> => request('/api/settings/cookie/test', { method: 'POST' }),
  importCookie: (cookie: string): Promise<CookieCheckResult & { saved: true }> =>
    request('/api/settings/cookie/import', { method: 'POST', body: { cookie } }),
  refreshCookie: (): Promise<LoginOpStatus> => request('/api/settings/cookie/refresh', { method: 'POST' }),
  cookieRefreshStatus: (signal?: AbortSignal): Promise<LoginOpStatus> =>
    request('/api/settings/cookie/refresh/status', { signal }),
  testXhCookie: (): Promise<CookieCheckResult> => request('/api/settings/xh-cookie/test', { method: 'POST' }),
  importXhCookie: (cookie: string): Promise<CookieCheckResult & { saved: true }> =>
    request('/api/settings/xh-cookie/import', { method: 'POST', body: { cookie } }),
  refreshXhCookie: (): Promise<LoginOpStatus> => request('/api/settings/xh-cookie/refresh', { method: 'POST' }),
  xhCookieRefreshStatus: (signal?: AbortSignal): Promise<LoginOpStatus> =>
    request('/api/settings/xh-cookie/refresh/status', { signal }),
  testYescaptcha: (): Promise<ToolResult & { balance?: number }> => request('/api/settings/yescaptcha/test', { method: 'POST' }),
  listRcloneRemotes: (): Promise<{ remotes: RcloneRemote[] }> => request('/api/settings/rclone/remotes'),
  testRclone: (): Promise<ToolResult & { results: (ToolResult & { remote: string })[] }> =>
    request('/api/settings/rclone/test', { method: 'POST' }),
  testNotify: (): Promise<ToolResult> => request('/api/settings/notify/test', { method: 'POST' }),
  getLogs: (): Promise<ItemsResponse<LogEntry>> => request('/api/settings/logs'),
  clearLogs: (): Promise<{ cleared: true }> => request('/api/settings/logs', { method: 'DELETE' }),
}
