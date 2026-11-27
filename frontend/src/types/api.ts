export type SiteKey = 'pornhub' | 'xhamster'
export type TargetType = 'model' | 'pornstar' | 'favorites' | 'liked' | 'creator' | 'users' | 'channel'
export type TargetTypeInput = TargetType | 'auto' | 'user'

export type JobStatus = 'queued' | 'scanning' | 'running' | 'completed' | 'partial_failed' | 'cancelled' | 'failed'
export type JobKind =
  | 'manual_target' | 'manual_urls' | 'auto_update' | 'check_updates'
  | 'baseline_sync' | 'resume_temp' | 'retry_items' | 'enrich_metadata'

export interface JobParams {
  site?: SiteKey
  usernames?: string[]
  target_type?: TargetTypeInput
  urls?: string[]
  url_count?: number
  skipped_non_video?: number
  group_by_uploader?: boolean
  batch_name?: string
  quality?: string | null
  pages?: string
  videos?: string
  keywords?: string
  exclude_keywords?: string
  redownload?: boolean
  auto_upload?: boolean
  export_only?: boolean
  add_to_auto_track?: boolean
  subscribe_creator?: boolean
  target_id?: number
  target_ids?: number[] | null
  profile_names?: Record<string, string>
  profile_avatars?: Record<string, string>
  origin_label?: string
  source_job_id?: number
  resume_count?: number
  resume_mode?: string
}

export interface JobRecord {
  id: number
  kind: JobKind
  trigger: 'manual' | 'schedule'
  status: JobStatus
  label: string
  params: JobParams
  created_at: string
  total_items: number
  done_items: number
  uploaded_items: number
  failed_items: number
  skipped_items: number
  revision?: number
  started_at?: string | null
  finished_at?: string | null
  // Session summaries omit these fields.
  params_json?: string
  export_text?: string
  error_message?: string
}

export type JobItemStatus =
  | 'pending' | 'downloading' | 'downloaded' | 'uploading' | 'done' | 'skipped' | 'failed' | 'cancelled'

export interface JobItemRecord {
  id: number
  job_id: number
  status: JobItemStatus
  site: SiteKey
  viewkey: string
  title: string
  uploader: string
  target_type: TargetType | ''
  page_url: string
  download_url: string
  referer_url: string
  quality: string
  temp_path: string
  local_path: string
  bytes_downloaded: number
  bytes_total: number
  retry_count: number
  error_message: string
  updated_at: string
  upload_requested: 0 | 1 | null
  published_at: string
  duration_seconds: number
  revision?: number
  progress_phase?: string
  upload_bytes?: number
  speed_bps?: number
}

export interface JobDetailResponse {
  job: JobRecord
  items: JobItemRecord[]
}

export interface LiveProgressEntry {
  job_id: number
  item_id: number
  bytes_downloaded: number
  bytes_total: number
  speed_bps: number
  phase: string
  estimated_total?: boolean
}

export type ScanProgress = { current_target?: string } & (
  | { mode: 'targets'; total: number; scanned: number; found_new?: number }
  | { mode: 'pages' | 'baseline'; page: number; found_videos?: number; target_index?: number; total_targets?: number }
)

export type ScanStatusEntry = ScanProgress & { job_id: number; cleared?: false }

export interface SchedulerStatus {
  job_id: number
  cleared?: false
  level: 'green' | 'yellow' | 'orange' | 'red'
  free_bytes: number
  waiting_for_space?: number
  active_downloads?: number
  allowed_downloads?: number
  max_downloads?: number
}

export interface LogEntry {
  id: number
  boot_id?: string
  ts: string
  level: string
  job_id: number | null
  message: string
}

export interface TargetRecord {
  id: number
  site: SiteKey
  username: string
  target_type: TargetType
  display_name: string
  avatar_url: string
  enabled: boolean
  preferred_quality: string
  created_at: string
  last_checked_at: string | null
  last_new_count: number
  last_error: string
  today_new_date?: string
  today_new_count?: number
  // Counts are computed by the list endpoint.
  video_total_count?: number
  video_saved_count?: number
  video_baseline_count?: number
}

export interface SubscriptionItem {
  site?: SiteKey
  type: TargetType | 'user'
  username: string
  display_name: string
  avatar_url: string
  bound_username?: string
  bound_display_name?: string
}

export interface SubscriptionsResponse {
  site: SiteKey
  username: string
  subscriptions: SubscriptionItem[]
}

export type VideoStatus = 'baseline' | 'downloaded' | 'uploaded'

export interface VideoRecord {
  viewkey: string
  site: SiteKey
  title: string
  uploader: string
  uploader_display_name?: string
  target_type: TargetType | ''
  video_page_url: string
  quality: string
  status: VideoStatus
  file_size: number
  duration_seconds: number
  published_at: string
  created_at: string
  downloaded_at: string | null
  uploaded_at: string | null
  local_path: string
  remote_path: string
  upload_requested: 0 | 1
}

export interface VideoFilterFacets {
  sites: SiteKey[]
  uploaders: string[]
  uploader_labels: Record<string, string>
  qualities: string[]
}

export interface VideoStats {
  total: number
  uploaded: number
  downloaded: number
  baseline: number
  published_7d: number
  baseline_missing_metadata: number
}

export interface VideoListResponse {
  items: VideoRecord[]
  total: number
  page: number
  page_size: number
  facets: VideoFilterFacets | null
  stats: VideoStats | null
}

export interface VideoFilters {
  site: SiteKey | ''
  search: string
  uploader: string
  status: VideoStatus | ''
  quality: string
  time_range: '' | 'published_7d' | 'published_30d' | 'activity_7d'
  sort_by: 'activity' | 'published'
  page: number
  page_size: number
  include_meta?: boolean
}

export interface PlaybackResolution {
  format: 'mp4' | 'hls'
  sources: { quality: string; relay_url: string }[]
}

export interface SelectorSettings {
  list_page_main_containers: string
  list_page_li_in_container: string
  list_page_li_fallback: string
  list_page_video_link_tag_primary: string
  list_page_video_link_tag_fallback_href: string
  list_page_video_image_for_title: string
  list_page_video_span_div_for_title: string
  detail_page_h1_titles: string
  detail_page_og_title_meta: string
  detail_page_title_tag: string
  detail_page_uploader_container: string
  detail_page_uploader_link_in_container: string
  subs_page_main_container: string
  subs_page_user_link_in_container: string
  pornstar_page_video_container_check: string
}

/** Settings JSON sent to the frontend, including the redaction and cookie indicators. */
export interface RuntimeSettings {
  ph_username: string
  ph_password: string
  ph_logged_in_username: string
  ph_cookie: string
  xh_username: string
  xh_password: string
  xh_cookie: string
  xh_logged_in_username: string
  account_avatar_version: string
  has_cookie: boolean
  has_xh_cookie: boolean
  yescaptcha_enabled: boolean
  yescaptcha_client_key: string
  yescaptcha_ph_task_type: string
  yescaptcha_xh_task_type: string
  yescaptcha_timeout_seconds: number
  proxy_url: string
  request_timeout: number
  max_retries_page_fetch: number
  retry_delay_base: number
  retry_delay_max: number
  page_delay_min: number
  page_delay_max: number
  max_concurrent_downloads: number
  max_concurrent_video_details: number
  max_concurrent_list_pages: number
  download_attempts: number
  min_file_size_bytes: number
  preferred_quality: string
  xh_preferred_codec: 'h264' | 'av1'
  upload_batch_size: number
  upload_workers: number
  max_pending_upload_files: number
  min_free_disk_gb: number
  uploader_enabled: boolean
  rclone_bin: string
  rclone_remote: string
  rclone_backup_remotes: string
  rclone_remote_strategy: 'failover' | 'round_robin'
  rclone_remote_base_dir: string
  rclone_xh_remote_base_dir: string
  rclone_transfers: number
  rclone_checkers: number
  rclone_pikpak_upload_concurrency: number
  rclone_pikpak_chunk_size: string
  rclone_drive_chunk_size: string
  rclone_drive_stop_on_upload_limit: boolean
  rclone_extra_flags: string
  rclone_ignore_checksum: boolean
  rclone_ignore_size: boolean
  rclone_verify_download: boolean
  rclone_tpslimit: number
  rclone_tpslimit_burst: number
  rclone_log_level: string
  rclone_timeout_seconds: number
  schedule_enabled: boolean
  schedule_time: string
  update_target_concurrency: number
  update_scan_mode: 'catch_up' | 'full'
  notify_if_no_updates: boolean
  serverchan_enabled: boolean
  serverchan_send_key: string
  serverchan_api_url: string
  selectors: SelectorSettings
}

// Number inputs emit an empty string while cleared. The server validates the submitted value.
export type SettingsInput = {
  [K in keyof RuntimeSettings]: RuntimeSettings[K] extends number ? number | '' : RuntimeSettings[K]
}

export type SettingsPatch = Partial<Omit<SettingsInput, 'has_cookie' | 'has_xh_cookie' | 'selectors'>> & {
  selectors?: Partial<SelectorSettings>
}

export interface RcloneRemote {
  name: string
  type: string
}

export interface CookieStatus {
  valid: boolean | null
  message: string
  checked_at?: string | null
}

export interface SettingsResponse {
  settings: RuntimeSettings
  cookie_status: CookieStatus
  xh_cookie_status: CookieStatus
}

export interface OverviewResponse {
  running: boolean
  current_job: JobRecord | null
  queued_count: number
  account: { username: string; avatar_url: string }
  cookie: CookieStatus & { configured: boolean; username: string }
  xh_cookie: CookieStatus & { configured: boolean; username: string }
  schedule: {
    enabled: boolean
    time: string
    scan_mode: RuntimeSettings['update_scan_mode']
    next_run_at: string | null
  }
  uploader_enabled: boolean
  event_loop: { current_ms: number; avg_ms: number; max_ms: number; warning_count: number; warning_threshold_ms: number }
  targets_total: number
  targets_enabled: number
  videos: VideoStats
  temp_files_count: number
  disk: { free_bytes: number; total_bytes: number }
}

export interface TempFileEntry {
  name?: string
  folder: string
  site?: SiteKey
  viewkey?: string
  size_bytes?: number
  verified_checkpoint?: boolean
}

export interface CookieCheckResult {
  valid: boolean
  message: string
  logged_in_username: string
}

export interface LoginOpStatus {
  running: boolean
  has_result: boolean
  success?: boolean
  message?: string
  logged_in_username?: string
  started_at?: string | null
  finished_at?: string | null
}

export interface CreateJobRequest extends Pick<JobParams,
  'site' | 'usernames' | 'target_type' | 'urls' | 'group_by_uploader' | 'batch_name' | 'quality' |
  'pages' | 'videos' | 'keywords' | 'exclude_keywords' | 'redownload' | 'export_only' | 'add_to_auto_track' | 'subscribe_creator'
> {
  source_mode?: 'target' | 'urls'
  auto_upload?: boolean | null
}

export interface CreateTargetRequest {
  site?: SiteKey
  username?: string
  target_type?: TargetTypeInput
  display_name?: string
  enabled?: boolean
  preferred_quality?: string
  init_baseline?: boolean
  baseline_pages?: string
}

export interface BatchCreateTargetsRequest {
  items: (Pick<CreateTargetRequest, 'site' | 'target_type' | 'display_name' | 'preferred_quality'> & {
    username: string
    avatar_url?: string
  })[]
  init_baseline?: boolean
  baseline_pages?: string
}

export type UpdateTargetRequest = {
  [K in 'enabled' | 'username' | 'target_type' | 'preferred_quality' | 'display_name']?: CreateTargetRequest[K] | null
}

interface WsSnapshot {
  logs: LogEntry[]
  live_progress: Record<number, LiveProgressEntry>
  scheduler_status: Record<number, SchedulerStatus>
  scan_status: Record<number, ScanStatusEntry>
}

interface WsEventData {
  log: LogEntry
  job_updated: JobRecord
  item_updated: JobItemRecord
  job_items_changed: { job_id: number }
  item_progress: LiveProgressEntry
  job_scheduler: SchedulerStatus | { job_id: number; cleared: true }
  job_scan: ScanStatusEntry | { job_id: number; cleared: true }
  overview_changed: Record<string, never>
  targets_changed: Record<string, unknown>
  subscriptions_changed: Record<string, unknown>
}

type WsEvent = {
  [K in keyof WsEventData]: { type: K; ts: string; data: WsEventData[K] }
}[keyof WsEventData]

export type WsMessage = WsEvent | { type: 'snapshot'; data: WsSnapshot } | { type: 'ping' }
