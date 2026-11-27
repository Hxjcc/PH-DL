import type { SiteKey, TargetType } from '../types/api'

interface TargetInput {
  site: SiteKey
  target_type: TargetType
  username: string
}

const PH_PROFILE_PATH_RE = /\/(?:model|pornstar|users|user)\/([^/?#]+)/i
const XH_PROFILE_PATH_RE = /\/(creators|pornstars|users|channels)\/([^/?#]+)/i
const XH_HOST_RE = /(?:^|\.)xhamster\d*\.(?:com|desi|one|tv|xxx|live)$/i
const INVALID_USERNAME_RE = /[/\\:?#\s]/

export function isXhamsterUrlText(raw: string): boolean {
  const text = String(raw || '').trim()
  if (!text) return false
  if (/^https?:\/\//i.test(text)) {
    try {
      const host = new URL(text).hostname.replace(/\.+$/, '').toLowerCase()
      return XH_HOST_RE.test(host)
    } catch {
      return false
    }
  }
  return /\bxhamster\d*\.(?:com|desi|one|tv|xxx|live)\b/i.test(text)
}

export function extractTargetFromInput(raw: string): TargetInput | null {
  const text = String(raw || '').trim()
  if (!text) return null

  if (isXhamsterUrlText(text)) {
    if (/\/my\/favorites/i.test(text)) {
      return { site: 'xhamster', target_type: 'favorites', username: '' }
    }
    const match = text.match(XH_PROFILE_PATH_RE)
    if (!match?.[2]) return null
    const section = match[1].toLowerCase()
    let slug = match[2].trim()
    try {
      slug = decodeURIComponent(slug).trim()
    } catch {}
    if (!slug || INVALID_USERNAME_RE.test(slug)) return null
    const typeMap: Partial<Record<string, TargetType>> = {
      creators: 'creator',
      pornstars: 'pornstar',
      users: 'users',
      channels: 'channel',
    }
    return {
      site: 'xhamster',
      target_type: typeMap[section] || 'creator',
      username: slug,
    }
  }

  if (/pornhub\./i.test(text)) {
    if (/\/likedvideos/i.test(text)) {
      return { site: 'pornhub', target_type: 'liked', username: '' }
    }
    const favMatch = text.match(/\/(?:users|user)\/([^/?#]+)\/videos\/favorites/i)
    if (favMatch?.[1]) {
      let slug = favMatch[1].trim()
      try {
        slug = decodeURIComponent(slug).trim()
      } catch {}
      if (slug && !INVALID_USERNAME_RE.test(slug)) {
        return { site: 'pornhub', target_type: 'favorites', username: slug }
      }
    }
    const phMatch = text.match(/\/(model|pornstar)\/([^/?#]+)/i)
    if (phMatch?.[2]) {
      let slug = phMatch[2].trim()
      try {
        slug = decodeURIComponent(slug).trim()
      } catch {}
      if (slug && !INVALID_USERNAME_RE.test(slug)) {
        return { site: 'pornhub', target_type: phMatch[1].toLowerCase() as 'model' | 'pornstar', username: slug }
      }
    }
  }

  return null
}

export function normalizeTargetUsername(token: string): string {
  const raw = String(token || '').trim()
  if (!raw) return ''
  if (
    /^https?:\/\//i.test(raw) ||
    raw.includes('pornhub.') ||
    isXhamsterUrlText(raw) ||
    raw.includes('/') ||
    raw.includes('?')
  ) {
    if (isXhamsterUrlText(raw)) {
      const xh = extractTargetFromInput(raw)
      return xh?.username || ''
    }
    const match = raw.match(PH_PROFILE_PATH_RE)
    const picked = match?.[1]
    if (!picked) return ''
    let slug = picked.trim()
    try {
      slug = decodeURIComponent(slug).trim()
    } catch {}
    return slug && !INVALID_USERNAME_RE.test(slug) ? slug : ''
  }
  const cleaned = raw.replace(/^@+/, '').trim()
  return cleaned && !INVALID_USERNAME_RE.test(cleaned) ? cleaned : ''
}

export function parseTargetUsernames(raw: string): string[] {
  const tokens = String(raw || '')
    .split(/[\s,，;；]+/)
    .map(normalizeTargetUsername)
    .filter(Boolean)
  const seen = new Set<string>()
  const unique = []
  for (const name of tokens) {
    const key = name.toLowerCase()
    if (seen.has(key)) continue
    seen.add(key)
    unique.push(name)
  }
  return unique
}

export function buildTargetProfileVideosUrl(target: { site?: string; target_type?: string; username?: string } | null): string {
  const site = String(target?.site || 'pornhub').trim().toLowerCase()
  const type = String(target?.target_type || (site === 'xhamster' ? 'creator' : 'model')).trim().toLowerCase()

  if (site === 'xhamster') {
    if (type === 'favorites') return 'https://xhamster.com/my/favorites/videos'
    const username = String(target?.username || '').trim()
    if (!username || username === '_currentUser_') return ''
    const slug = encodeURIComponent(username)
    if (type === 'users' || type === 'user') return `https://xhamster.com/users/${slug}/videos`
    if (type === 'pornstar') return `https://xhamster.com/pornstars/${slug}/newest`
    if (type === 'channel') return `https://xhamster.com/channels/${slug}/newest`
    return `https://xhamster.com/creators/${slug}/newest`
  }

  if (type === 'liked') return 'https://www.pornhub.com/likedvideos'
  if (type === 'user' || type === 'users') return ''
  const username = String(target?.username || '').trim()
  if (!username || username === '_currentUser_') return ''
  if (type === 'favorites') {
    return `https://www.pornhub.com/users/${encodeURIComponent(username.toLowerCase())}/videos/favorites`
  }
  const slug = encodeURIComponent(username)
  if (type === 'pornstar') return `https://www.pornhub.com/pornstar/${slug}/videos`
  return `https://www.pornhub.com/${encodeURIComponent(type || 'model')}/${slug}/videos`
}
