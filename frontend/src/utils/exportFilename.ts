import type { JobRecord } from '../types/api'

function safeFilePart(value: string, maxLength = 150): string {
  return String(value || '')
    .replace(/[\\/:*?"<>|\x00-\x1f]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/[. ]+$/g, '')
    .slice(0, maxLength)
}

export function buildExportFilename(job: Partial<Pick<JobRecord, 'id' | 'kind' | 'params'>> | null): string {
  const params = job?.params || {}
  const usernames = Array.isArray(params.usernames)
    ? params.usernames.map((name) => String(name || '').trim()).filter(Boolean)
    : []

  let base = ''
  if (usernames.length > 0) {
    base = usernames.join('+')
  } else if (params.target_type === 'liked') {
    base = 'Liked_Videos'
  } else if (params.site === 'xhamster' && params.target_type === 'favorites') {
    base = 'xHamster_Favorites'
  } else if (params.batch_name) {
    base = String(params.batch_name)
  } else {
    base = job?.kind || 'export'
  }
  if (params.target_type === 'favorites' && !base.toLowerCase().endsWith('_favorites')) {
    base += '_favorites'
  }

  const suffixes = []
  if (params.pages) suffixes.push(`P${String(params.pages).trim()}`)
  if (params.videos) suffixes.push(`V${String(params.videos).trim()}`)
  if (params.quality) suffixes.push(`Q${String(params.quality).trim()}`)
  if (params.keywords) suffixes.push(`KW${String(params.keywords).trim()}`)
  if (params.exclude_keywords) suffixes.push(`EX${String(params.exclude_keywords).trim()}`)

  const filename = safeFilePart(`${base}${suffixes.length ? `_${suffixes.join('_')}` : ''}`)
  return `${filename || `export_job_${job?.id || 'unknown'}`}.txt`
}
