import type { JobItemRecord, JobRecord, JobStatus } from '../src/types/api'

export function makeJob(id: number, status: JobStatus, params: JobRecord['params']): JobRecord {
  return {
    id,
    kind: 'manual_urls',
    trigger: 'manual',
    status,
    label: '',
    params,
    created_at: '',
    total_items: 0,
    done_items: 0,
    uploaded_items: 0,
    failed_items: 0,
    skipped_items: 0,
  }
}

export function makeJobItem(id: number, jobId: number, overrides: Partial<JobItemRecord> = {}): JobItemRecord {
  return {
    id,
    job_id: jobId,
    status: 'pending',
    site: 'pornhub',
    viewkey: '',
    title: '',
    uploader: '',
    target_type: '',
    page_url: '',
    download_url: '',
    referer_url: '',
    quality: '',
    temp_path: '',
    local_path: '',
    bytes_downloaded: 0,
    bytes_total: 0,
    retry_count: 0,
    error_message: '',
    updated_at: '',
    upload_requested: null,
    published_at: '',
    duration_seconds: 0,
    ...overrides,
  }
}
