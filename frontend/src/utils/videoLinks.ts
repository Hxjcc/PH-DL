import type { SiteKey } from '../types/api'

const phVideoKeyPattern = /(?:viewkey=|\/embed\/|\/shorties\/)([a-zA-Z0-9]+)/
const xhHostPattern = /^(?:[a-z0-9-]+\.)*xhamster\d*\.(?:com|desi|one|tv|xxx|live)$/i

function extractXhamsterVideoKey(pathname: string): string {
  const parts = String(pathname || '')
    .split('/')
    .filter(Boolean)
  if (parts.length < 2) return ''
  const head = parts[0].toLowerCase()
  const slug = parts[1].trim()
  if (head === 'videos' || head === 'movies') {
    const legacy = slug.match(/^video-([0-9]+)$/)
    if (legacy?.[1]) return `xh_${legacy[1]}`
    const hashMatch = slug.match(/(?:^|-)(xh[a-zA-Z0-9]{4,})$/)
    if (hashMatch?.[1]) return hashMatch[1]
    const numMatch = slug.match(/(?:^|-)(\d{4,})$/)
    if (numMatch?.[1]) return `xh_${numMatch[1]}`
    return ''
  }
  if (head === 'embed') {
    if (/^xh[a-zA-Z0-9]{4,}$/.test(slug)) return slug
    if (/^\d{4,}$/.test(slug)) return `xh_${slug}`
  }
  return ''
}

export function safeExternalVideoUrl(rawUrl: string | undefined, site: SiteKey, viewkey: string): string | undefined {
  const raw = (rawUrl || '').trim()
  if (raw && !/[\s\\]/.test(raw)) {
    try {
      const url = new URL(raw)
      const host = url.hostname.replace(/\.$/, '').toLowerCase()
      const allowedHost = site === 'xhamster' ? xhHostPattern.test(host) :
        host === 'pornhub.com' || host.endsWith('.pornhub.com')
      if ((url.protocol === 'https:' || url.protocol === 'http:') && allowedHost &&
          !url.username && !url.password && !url.port) {
        const key = site === 'xhamster' ? extractXhamsterVideoKey(url.pathname) : raw.match(phVideoKeyPattern)?.[1]
        if (key && (!viewkey || key === viewkey)) return url.href
      }
    } catch {}
  }
  if (!viewkey) return undefined
  if (site === 'xhamster') {
    const legacy = viewkey.match(/^xh_([0-9]+)$/)
    return `https://xhamster.com/videos/${legacy ? `video-${legacy[1]}` : encodeURIComponent(viewkey)}`
  }
  return `https://www.pornhub.com/view_video.php?viewkey=${encodeURIComponent(viewkey)}`
}

export function parseVideoLinks(text: string): { urls: string[]; invalid: number; duplicates: number } {
  const videos = new Map<string, string>()
  let invalid = 0
  let duplicates = 0
  for (const row of String(text || '').split(/\r?\n/)) {
    const line = row.trim()
    if (!line || line.startsWith('#')) continue

    let key = ''
    let valid = false
    if (/^https?:\/\//i.test(line) && !/[\s\\]/.test(line)) {
      try {
        const parsed = new URL(line)
        const host = parsed.hostname.replace(/\.+$/, '').toLowerCase()
        if (host === 'pornhub.com' || host.endsWith('.pornhub.com')) {
          key = line.match(phVideoKeyPattern)?.[1] || ''
          valid = Boolean(key)
        } else if (xhHostPattern.test(host)) {
          key = extractXhamsterVideoKey(parsed.pathname)
          valid = Boolean(key)
        }
      } catch {}
    }
    if (!valid) {
      invalid += 1
    } else if (videos.has(key)) {
      duplicates += 1
    } else {
      videos.set(key, line)
    }
  }
  return { urls: [...videos.values()], invalid, duplicates }
}
