import { targetTypeMeta } from './format'
import { isXhamsterUrlText } from './targetInput'
import type { JobItemRecord, JobRecord, LiveProgressEntry, ScanProgress, SiteKey, TargetRecord } from '../types/api'

type VisualJob = Partial<Pick<JobRecord, 'id' | 'kind' | 'params' | 'export_text' | 'label' | 'trigger'>>
type VisualTarget = Partial<Pick<TargetRecord, 'id' | 'username' | 'site' | 'target_type' | 'display_name' | 'avatar_url' | 'enabled'>>
type TargetLookup = {
  byId: Map<number, VisualTarget>
  byName: Map<string, VisualTarget>
  bySiteName: Map<string, VisualTarget>
}
type Capsule = { text: string; handle?: boolean; mono?: boolean; tone?: 'emerald' | 'amber' | 'brand' }
type JobMetricInput = Partial<Pick<JobRecord, 'id' | 'kind' | 'status' | 'total_items' | 'done_items' | 'uploaded_items' |
  'skipped_items' | 'failed_items' | 'started_at' | 'finished_at'>>
type ItemMetricInput = Pick<JobItemRecord, 'id' | 'status' | 'progress_phase'>
type ProgressMetricInput = Pick<LiveProgressEntry, 'job_id'> & Partial<LiveProgressEntry>

export function jobItemErrorClass(item: Pick<JobItemRecord, 'status' | 'error_message'>) {
  const message = item.error_message.trim()
  if (item.status === 'cancelled') {
    if (/^(?:已取消(?:（断点已保留）)?|上传已取消，保留本地文件|用户取消排队任务)$/.test(message)) {
      return 'text-muted-foreground'
    }
    if (/限额|配额|磁盘/.test(message)) return 'text-amber-700 dark:text-amber-400'
  }
  return 'text-red-600 dark:text-red-400'
}

const CATEGORY_MAP = {
  target_download: {
    key: 'target_download',
    group: 'download',
    label: '目标下载',
    icon: 'download',
    pillClass: 'border-sky-500/25 bg-sky-500/10 text-sky-700 dark:text-sky-300',
    iconClass: 'bg-sky-500/12 text-sky-600 dark:text-sky-400 ring-1 ring-sky-500/20',
  },
  url_download: {
    key: 'url_download',
    group: 'download',
    label: '链接下载',
    icon: 'link',
    pillClass: 'border-indigo-500/25 bg-indigo-500/10 text-indigo-700 dark:text-indigo-300',
    iconClass: 'bg-indigo-500/12 text-indigo-600 dark:text-indigo-400 ring-1 ring-indigo-500/20',
  },
  export: {
    key: 'export',
    group: 'download',
    label: '链接导出',
    icon: 'file',
    pillClass: 'border-violet-500/25 bg-violet-500/10 text-violet-700 dark:text-violet-300',
    iconClass: 'bg-violet-500/12 text-violet-600 dark:text-violet-400 ring-1 ring-violet-500/20',
  },
  auto_update: {
    key: 'auto_update',
    group: 'update',
    label: '自动追更',
    icon: 'refresh',
    pillClass: 'border-emerald-500/25 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
    iconClass: 'bg-emerald-500/12 text-emerald-600 dark:text-emerald-400 ring-1 ring-emerald-500/20',
  },
  targeted_update: {
    key: 'targeted_update',
    group: 'update',
    label: '指定追更',
    icon: 'zap',
    pillClass: 'border-emerald-500/25 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
    iconClass: 'bg-emerald-500/12 text-emerald-600 dark:text-emerald-400 ring-1 ring-emerald-500/20',
  },
  check_updates: {
    key: 'check_updates',
    group: 'update',
    label: '更新检查',
    icon: 'search',
    pillClass: 'border-teal-500/25 bg-teal-500/10 text-teal-700 dark:text-teal-300',
    iconClass: 'bg-teal-500/12 text-teal-600 dark:text-teal-400 ring-1 ring-teal-500/20',
  },
  baseline: {
    key: 'baseline',
    group: 'maintenance',
    label: '基线同步',
    icon: 'database',
    pillClass: 'border-amber-500/25 bg-amber-500/10 text-amber-700 dark:text-amber-300',
    iconClass: 'bg-amber-500/12 text-amber-600 dark:text-amber-400 ring-1 ring-amber-500/20',
  },
  resume: {
    key: 'resume',
    group: 'download',
    label: '断点续传',
    icon: 'rotate',
    pillClass: 'border-orange-500/25 bg-orange-500/10 text-orange-700 dark:text-orange-300',
    iconClass: 'bg-orange-500/12 text-orange-600 dark:text-orange-400 ring-1 ring-orange-500/20',
  },
  retry: {
    key: 'retry',
    group: 'download',
    label: '失败重试',
    icon: 'rotate',
    pillClass: 'border-orange-500/25 bg-orange-500/10 text-orange-700 dark:text-orange-300',
    iconClass: 'bg-orange-500/12 text-orange-600 dark:text-orange-400 ring-1 ring-orange-500/20',
  },
  metadata: {
    key: 'metadata',
    group: 'maintenance',
    label: '元数据补全',
    icon: 'sparkles',
    pillClass: 'border-purple-500/25 bg-purple-500/10 text-purple-700 dark:text-purple-300',
    iconClass: 'bg-purple-500/12 text-purple-600 dark:text-purple-400 ring-1 ring-purple-500/20',
  },
} as const

function stripKnownPrefix(rawLabel = '') {
  let text = String(rawLabel || '').trim()
  const sitePrefix = /^\[(xHamster|PornHub)\]\s*/i
  text = text.replace(sitePrefix, '').replace(/^(重试\/续传|续传|重试)\s*·\s*/, '').replace(sitePrefix, '')
  text = text.replace(/\s*\(\d+\s*[项条个]\)\s*$/, '')
  text = text.replace(
    /^(下载目标|导出页面链接|链接下载|指定追更|指定检查更新|初始化基线|仅续传临时文件)\s*[:：]\s*/,
    ''
  )
  return text.trim()
}

const targetLookupCache = new WeakMap<readonly VisualTarget[], TargetLookup>()
const EMPTY_TARGET_LOOKUP = Object.freeze<TargetLookup>({ byId: new Map(), byName: new Map(), bySiteName: new Map() })

function normalizeSiteKey(site: string | undefined): SiteKey | '' {
  const s = String(site || '').trim().toLowerCase()
  if (s === 'xhamster' || s === 'xh') return 'xhamster'
  if (s === 'pornhub' || s === 'ph') return 'pornhub'
  return ''
}

export function resolveManualUrlSite(urls: readonly string[], site?: SiteKey): SiteKey | '' {
  const fallback = normalizeSiteKey(site)
  const links = urls.filter(Boolean)
  if (links.length === 0) return fallback
  if (links.every(isXhamsterUrlText)) return 'xhamster'
  if (!links.some(isXhamsterUrlText)) return 'pornhub'
  return fallback
}

function buildTargetLookup(targetsList: readonly VisualTarget[] = []): TargetLookup {
  if (!Array.isArray(targetsList) || targetsList.length === 0) {
    return EMPTY_TARGET_LOOKUP
  }
  const cached = targetLookupCache.get(targetsList)
  if (cached) return cached

  const byId = new Map<number, VisualTarget>()
  const byName = new Map<string, VisualTarget>()
  const bySiteName = new Map<string, VisualTarget>()
  for (const t of targetsList) {
    if (!t) continue
    if (t.id != null) byId.set(Number(t.id), t)
    const site = normalizeSiteKey(t.site) || 'pornhub'
    const uname = String(t.username || '').trim().toLowerCase()
    const dname = String(t.display_name || '').trim().toLowerCase()
    if (uname) {
      if (!byName.has(uname)) byName.set(uname, t)
      const sk = `${site}:${uname}`
      if (!bySiteName.has(sk)) bySiteName.set(sk, t)
    }
    if (dname) {
      if (!byName.has(dname)) byName.set(dname, t)
      const sk = `${site}:${dname}`
      if (!bySiteName.has(sk)) bySiteName.set(sk, t)
    }
  }
  const lookup = { byId, byName, bySiteName }
  targetLookupCache.set(targetsList, lookup)
  return lookup
}

function lookupProfileField(map: Record<string, string>, ...keys: (string | undefined)[]) {
  if (!map || typeof map !== 'object') return ''
  for (const rawKey of keys) {
    const key = String(rawKey || '').trim()
    if (!key) continue
    if (map[key]) return String(map[key]).trim()
  }
  const wanted = new Set(
    keys
      .map((k) => String(k || '').trim().toLowerCase())
      .filter(Boolean)
  )
  if (wanted.size === 0) return ''
  for (const [k, v] of Object.entries(map)) {
    if (v && wanted.has(String(k).trim().toLowerCase())) {
      return String(v).trim()
    }
  }
  return ''
}

function makeAvatarEntry(
  target: VisualTarget | null | undefined,
  fallbackName: string | undefined,
  profileAvatars: Record<string, string> = {},
  profileNames: Record<string, string> = {},
  hint: { site?: SiteKey | ''; targetType?: string } = {},
) {
  if (target) {
    const site = normalizeSiteKey(target.site || hint.site) || 'pornhub'
    const tType = String(target.target_type || hint.targetType || '').trim().toLowerCase()
    const isFavorites = site === 'xhamster' && tType === 'favorites'
    const isLiked = !isFavorites && (tType === 'liked' || target.username === '_currentUser_')
    const name = isLiked
      ? '我的点赞'
      : isFavorites
        ? String(target.display_name || '').trim() || '我的收藏'
        : String(
            target.display_name ||
              lookupProfileField(profileNames, target.username, fallbackName) ||
              target.username ||
              fallbackName ||
              ''
          ).trim()
    const username = isLiked || isFavorites ? '' : String(target.username || '').trim()
    const avatarUrl =
      isLiked || isFavorites
        ? ''
        : String(target.avatar_url || lookupProfileField(profileAvatars, target.username, fallbackName) || '').trim()
    return {
      key: `target-${target.id || `${site}:${username || name}`}`,
      name: name || username || '未知目标',
      username,
      site,
      targetType: target.target_type || '',
      avatarUrl,
      isLiked,
      isFavorites,
    }
  }
  const raw = String(fallbackName || '').trim()
  const site = normalizeSiteKey(hint.site) || (raw.toLowerCase().includes('xhamster') ? 'xhamster' : '')
  const tType = String(hint.targetType || '').trim().toLowerCase()
  const isFavorites = (site === 'xhamster' && tType === 'favorites') || raw.toLowerCase() === 'xhamster favorites'
  const isLiked = !isFavorites && (tType === 'liked' || raw.toLowerCase() === 'liked videos' || raw === '_currentUser_')
  const displayName = isLiked
    ? '我的点赞'
    : isFavorites
      ? '我的收藏'
      : String(lookupProfileField(profileNames, raw) || raw).trim()
  const avatarUrl = isLiked || isFavorites ? '' : String(lookupProfileField(profileAvatars, raw) || '').trim()
  return {
    key: `raw-${site || 'any'}-${raw}`,
    name: displayName || '未知目标',
    username: isLiked || isFavorites ? '' : raw,
    site: site || 'pornhub',
    targetType: isLiked ? 'liked' : isFavorites ? 'favorites' : tType,
    avatarUrl,
    isLiked,
    isFavorites,
  }
}

export function resolveItemUploaderLabel(
  item: Partial<Pick<JobItemRecord, 'site' | 'uploader'>>,
  targetsList: readonly VisualTarget[] = [],
  profileNames: Record<string, string> = {},
) {
  const raw = String(item?.uploader || '').trim()
  if (!raw) return { label: '', isHandle: false }
  const low = raw.toLowerCase()
  if (low === 'my_liked') return { label: '我的点赞', isHandle: false }
  if (low === 'my_favorites') return { label: '我的收藏', isHandle: false }
  const { byName, bySiteName } = buildTargetLookup(targetsList)
  const site = normalizeSiteKey(item?.site)
  const matched = (site && bySiteName.get(`${site}:${low}`)) || byName.get(low) || null
  const displayName = String(
    matched?.display_name || lookupProfileField(profileNames, raw, matched?.username) || ''
  ).trim()
  if (displayName) {
    return { label: displayName, isHandle: false }
  }
  return { label: raw, isHandle: true }
}

export function resolveJobCategory(job: VisualJob | null | undefined) {
  const kind = job?.kind || ''
  const params = job?.params || {}
  const isExport = Boolean(params.export_only || job?.export_text)
  if (isExport) return CATEGORY_MAP.export
  if (kind === 'manual_target') return CATEGORY_MAP.target_download
  if (kind === 'manual_urls') return CATEGORY_MAP.url_download
  if (kind === 'auto_update') {
    return Array.isArray(params.target_ids) && params.target_ids.length > 0
      ? CATEGORY_MAP.targeted_update
      : CATEGORY_MAP.auto_update
  }
  if (kind === 'check_updates') return CATEGORY_MAP.check_updates
  if (kind === 'baseline_sync') return CATEGORY_MAP.baseline
  if (kind === 'resume_temp') return CATEGORY_MAP.resume
  if (kind === 'retry_items') {
    const label = String(job?.label || '')
    return label.startsWith('续传') ? CATEGORY_MAP.resume : CATEGORY_MAP.retry
  }
  if (kind === 'enrich_metadata') return CATEGORY_MAP.metadata
  return CATEGORY_MAP.target_download
}

export function resolveJobVisual(job: VisualJob | null | undefined, targetsList: readonly VisualTarget[] = []) {
  const category = resolveJobCategory(job)
  const params = job?.params || {}
  const profileAvatars = params.profile_avatars || {}
  const profileNames = params.profile_names || {}
  const { byId, byName, bySiteName } = buildTargetLookup(targetsList)

  const avatars: ReturnType<typeof makeAvatarEntry>[] = []
  const capsules: Capsule[] = []
  let title = ''
  let site = normalizeSiteKey(params.site)

  const lookupByName = (name: string, preferredSite: SiteKey | '' = '') => {
    const key = String(name || '').trim().toLowerCase()
    if (!key) return null
    const norm = normalizeSiteKey(preferredSite)
    if (norm && bySiteName.has(`${norm}:${key}`)) return bySiteName.get(`${norm}:${key}`)
    return byName.get(key) || null
  }

  const pushUniqueAvatar = (entry: ReturnType<typeof makeAvatarEntry>) => {
    if (!entry || !entry.name) return
    if (avatars.some((x) => x.name.toLowerCase() === entry.name.toLowerCase())) return
    avatars.push(entry)
  }

  const kind = job?.kind || ''

  if (kind === 'manual_target') {
    const reqSite = normalizeSiteKey(params.site) || 'pornhub'
    site = reqSite
    if (params.target_type === 'liked') {
      const likedTarget = Array.from(byId.values()).find((t) => t.target_type === 'liked')
      pushUniqueAvatar(makeAvatarEntry(likedTarget, 'Liked Videos', profileAvatars, profileNames, { site: 'pornhub', targetType: 'liked' }))
      title = '我的点赞视频'
    } else if (reqSite === 'xhamster' && params.target_type === 'favorites') {
      const favTarget = Array.from(byId.values()).find(
        (t) => normalizeSiteKey(t.site) === 'xhamster' && t.target_type === 'favorites'
      )
      pushUniqueAvatar(makeAvatarEntry(favTarget, 'xHamster Favorites', profileAvatars, profileNames, { site: 'xhamster', targetType: 'favorites' }))
      title = '我的收藏视频'
    } else {
      const usernames = Array.isArray(params.usernames) ? params.usernames.filter(Boolean) : []
      for (const u of usernames) {
        const matched = lookupByName(u, reqSite)
        pushUniqueAvatar(makeAvatarEntry(matched, u, profileAvatars, profileNames, { site: reqSite, targetType: params.target_type }))
      }
      if (avatars.length === 1) {
        title = avatars[0].name
        if (avatars[0].username && !avatars[0].isLiked && !avatars[0].isFavorites) {
          capsules.push({ text: `@${avatars[0].username}`, handle: true })
        }
        const tType = avatars[0].targetType || (params.target_type !== 'auto' ? params.target_type : '')
        if (tType && tType !== 'auto') {
          capsules.push({ text: targetTypeMeta(tType) })
        }
      } else if (avatars.length > 1) {
        title = avatars.slice(0, 2).map((a) => a.name).join('、')
        if (avatars.length > 2) {
          capsules.push({ text: `共 ${avatars.length} 个目标` })
        }
      } else {
        title = stripKnownPrefix(job?.label) || '目标下载'
      }
    }
    if (params.pages) {
      const p = String(params.pages).trim()
      capsules.push({ text: p.toLowerCase() === 'all' ? '全部页' : `第 ${p} 页`, mono: true })
    } else if (params.videos) {
      capsules.push({ text: `指定序号 ${params.videos}`, mono: true })
    }
  } else if (kind === 'manual_urls') {
    const urls = Array.isArray(params.urls) ? params.urls.filter(Boolean) : []
    const urlCount = urls.length || Number(params.url_count || 0)
    site = resolveManualUrlSite(urls, params.site)
    title = params.group_by_uploader === false && params.batch_name
      ? params.batch_name
      : '直链批量队列'
    if (urlCount > 0) {
      capsules.push({ text: `${urlCount} 条链接`, mono: true })
    }
    if (params.group_by_uploader) {
      capsules.push({ text: '按博主归档' })
    }
  } else if (kind === 'auto_update' || kind === 'check_updates') {
    const targetIds = Array.isArray(params.target_ids) ? params.target_ids : []
    if (targetIds.length > 0) {
      const matchedSites = new Set<SiteKey>()
      for (const tid of targetIds) {
        const matched = byId.get(Number(tid))
        if (matched) {
          matchedSites.add(normalizeSiteKey(matched.site) || 'pornhub')
          pushUniqueAvatar(makeAvatarEntry(matched, matched.username, profileAvatars, profileNames))
        }
      }
      if (matchedSites.size === 1) {
        site = Array.from(matchedSites)[0]
      }
      if (avatars.length === 1) {
        title = avatars[0].name
        if (avatars[0].username && !avatars[0].isLiked && !avatars[0].isFavorites) {
          capsules.push({ text: `@${avatars[0].username}`, handle: true })
        }
        if (avatars[0].targetType) {
          capsules.push({ text: targetTypeMeta(avatars[0].targetType) })
        }
      } else if (avatars.length > 1) {
        title = avatars.slice(0, 2).map((a) => a.name).join('、')
        if (targetIds.length > 2) capsules.push({ text: `共 ${targetIds.length} 个目标` })
      } else {
        title = stripKnownPrefix(job?.label) || `${targetIds.length} 个指定目标`
      }
    } else {
      const enabledTargets = (targetsList || []).filter((t) => t && t.enabled)
      for (const t of enabledTargets.slice(0, 3)) {
        pushUniqueAvatar(makeAvatarEntry(t, t.username, profileAvatars, profileNames))
      }
      title = kind === 'auto_update' ? '全量监控目标' : '全量目标更新检查'
      if (enabledTargets.length > 0) {
        capsules.push({ text: `${enabledTargets.length} 个启用目标` })
      }
    }
  } else if (kind === 'baseline_sync') {
    const matched = params.target_id != null ? byId.get(Number(params.target_id)) : null
    const rawClean = stripKnownPrefix(job?.label).replace(/\s*\([^)]+\)\s*$/, '').trim()
    const fallbackMatch = !matched && rawClean ? lookupByName(rawClean, site) : null
    const targetObj = matched || fallbackMatch
    if (targetObj) {
      site = normalizeSiteKey(targetObj.site) || site || 'pornhub'
    }
    if (targetObj || rawClean) {
      pushUniqueAvatar(makeAvatarEntry(targetObj, rawClean, profileAvatars, profileNames, { site }))
    }
    title = avatars[0]?.name || rawClean || '基线初始化'
    if (avatars[0]?.username && !avatars[0]?.isLiked && !avatars[0]?.isFavorites) {
      capsules.push({ text: `@${avatars[0].username}`, handle: true })
    }
    if (avatars[0]?.targetType) {
      capsules.push({ text: targetTypeMeta(avatars[0].targetType) })
    }
    const pages = String(params.pages || '').trim()
    capsules.push({ text: !pages || pages.toLowerCase() === 'all' ? '全量页码' : `第 ${pages} 页`, mono: true })
  } else if (kind === 'retry_items' || kind === 'resume_temp') {
    const baseLabel = stripKnownPrefix(params.origin_label || job?.label || '')
    const candidateName = baseLabel.split(',')[0]?.trim() || ''
    const matched = candidateName ? lookupByName(candidateName, site) : null
    if (matched) {
      site = normalizeSiteKey(matched.site) || site
    }
    if (matched || (candidateName && kind === 'retry_items' && !candidateName.includes('条链接'))) {
      pushUniqueAvatar(makeAvatarEntry(matched, candidateName, profileAvatars, profileNames, { site }))
    }
    title = avatars[0]?.name || baseLabel || category.label
  } else if (kind === 'enrich_metadata') {
    title = '基线画质与大小补全'
  } else {
    title = stripKnownPrefix(job?.label) || job?.kind || '任务'
  }

  if (params.quality) {
    const qText = String(params.quality)
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)
      .map((s) => (s.endsWith('p') ? s : `${s}p`))
      .join('/')
    if (qText) capsules.push({ text: qText, mono: true })
  }

  if (params.auto_upload === true && !params.export_only && !['baseline_sync', 'check_updates', 'enrich_metadata'].includes(kind)) {
    capsules.push({ text: '云端上传', tone: 'emerald' })
  }
  if (params.redownload) {
    capsules.push({ text: '强制重下', tone: 'amber' })
  }
  if (job?.trigger === 'schedule') {
    capsules.push({ text: '定时触发', tone: 'brand' })
  }
  if (Number(params.resume_count || 0) > 0) {
    capsules.push({ text: `已续传 ${params.resume_count} 次` })
  }
  if (params.source_job_id) {
    capsules.push({ text: `源自 #${params.source_job_id}`, mono: true })
  }

  return {
    category,
    title: title || job?.label || job?.kind || '未命名任务',
    avatars,
    capsules,
    site,
  }
}

export function formatDurationCompact(startedAt: string | null | undefined, finishedAt: string | null | undefined): string {
  if (!startedAt || !finishedAt) return ''
  const s = new Date(startedAt).getTime()
  const e = new Date(finishedAt).getTime()
  if (Number.isNaN(s) || Number.isNaN(e) || e <= s) return ''
  const sec = Math.max(1, Math.round((e - s) / 1000))
  if (sec < 60) return `${sec}s`
  const min = Math.floor(sec / 60)
  const rem = sec % 60
  if (min < 60) return rem > 0 ? `${min}m ${rem}s` : `${min}m`
  const hr = Math.floor(min / 60)
  return `${hr}h ${min % 60}m`
}

export function computeJobLiveMetrics(
  job: JobMetricInput | null | undefined,
  liveProgressMap: Partial<Record<number, ProgressMetricInput>> | null = {},
  jobItemsList: ItemMetricInput[] | null | undefined = null,
  scanStatusMap: Partial<Record<number, ScanProgress>> | null = null,
) {
  let total = Number(job?.total_items || 0)
  let done = Number(job?.done_items || 0)
  let uploaded = Number(job?.uploaded_items || 0)
  let skipped = Number(job?.skipped_items || 0)
  let failed = Number(job?.failed_items || 0)
  let downloadingCount = 0
  let remuxingCount = 0
  let uploadingCount = 0

  if (Array.isArray(jobItemsList) && jobItemsList.length > 0) {
    let iDone = 0
    let iUploaded = 0
    let iSkipped = 0
    let iFailed = 0
    for (const it of jobItemsList) {
      if (it.status === 'done') {
        iDone += 1
        if (job?.kind !== 'enrich_metadata') iUploaded += 1
      } else if (it.status === 'downloaded' || it.status === 'uploading') {
        iDone += 1
        if (it.status === 'uploading') uploadingCount += 1
      } else if (it.status === 'downloading') {
        const livePhase = String(liveProgressMap?.[it.id]?.phase || it.progress_phase || 'downloading').toLowerCase()
        if (livePhase === 'remuxing') {
          remuxingCount += 1
        } else {
          downloadingCount += 1
        }
      } else if (it.status === 'skipped') {
        iSkipped += 1
      } else if (it.status === 'failed') {
        iFailed += 1
      }
    }
    total = Math.max(total, jobItemsList.length)
    done = Math.max(done, iDone)
    uploaded = Math.max(uploaded, iUploaded)
    skipped = Math.max(skipped, iSkipped)
    failed = Math.max(failed, iFailed)
  }

  let downloadSpeedBps = 0
  let uploadSpeedBps = 0
  let fractional = 0
  let liveDlCount = 0
  let liveRemuxCount = 0
  let liveUpCount = 0
  const jobIdNum = Number(job?.id)
  if (liveProgressMap && jobIdNum > 0) {
    for (const prog of Object.values(liveProgressMap)) {
      if (!prog || Number(prog.job_id) !== jobIdNum) continue
      const phase = String(prog.phase || 'downloading').toLowerCase()
      const spd = Number(prog.speed_bps || 0)
      if (phase === 'uploading' || phase === 'verifying') {
        liveUpCount += 1
        if (spd > 0) {
          uploadSpeedBps += spd
        }
        continue
      }
      if (phase === 'remuxing') {
        liveRemuxCount += 1
        fractional += 0.99
        continue
      }
      liveDlCount += 1
      if (spd > 0) {
        downloadSpeedBps += spd
      }
      const bDone = Number(prog.bytes_downloaded || 0)
      const bTotal = Number(prog.bytes_total || 0)
      if (bTotal > 0 && bDone > 0) {
        fractional += Math.min(0.99, Math.max(0, bDone / bTotal))
      }
    }
  }
  downloadingCount = Math.max(downloadingCount, liveDlCount)
  remuxingCount = Math.max(remuxingCount, liveRemuxCount)
  uploadingCount = Math.max(uploadingCount, liveUpCount)
  const speedBps = downloadSpeedBps

  const finishedCount = done + skipped + failed
  const isActive = job?.status === 'queued' || job?.status === 'scanning' || job?.status === 'running'
  const scanState =
    isActive && scanStatusMap && jobIdNum > 0 && typeof scanStatusMap === 'object'
      ? scanStatusMap[jobIdNum] || null
      : null
  const useScanOverlay =
    scanState !== null &&
    (job?.status === 'scanning' || (total === 0 && downloadingCount === 0 && remuxingCount === 0 && uploadingCount === 0))

  let percent = 0
  let hasScanProgress = false
  if (useScanOverlay && scanState.mode === 'targets' && Number(scanState.total || 0) > 0) {
    const scanTotal = Number(scanState.total)
    const scanned = Math.min(scanTotal, Math.max(0, Number(scanState.scanned || 0)))
    percent = Math.min(100, Math.round((scanned / scanTotal) * 100))
    hasScanProgress = true
  } else if (total > 0) {
    const effective = Math.min(total, finishedCount + (isActive ? fractional : 0))
    const rawPct = (effective / total) * 100
    percent = isActive && rawPct > 0 && rawPct < 99.5
      ? Math.round(rawPct * 10) / 10
      : Math.min(100, Math.round(rawPct))
  } else if (job?.status === 'completed') {
    percent = 100
  }

  const allRemainingRemuxing =
    isActive &&
    total > 0 &&
    remuxingCount > 0 &&
    downloadingCount === 0 &&
    finishedCount + remuxingCount >= total
  const allRemainingUploading =
    isActive &&
    total > 0 &&
    uploadingCount > 0 &&
    downloadingCount === 0 &&
    remuxingCount === 0 &&
    finishedCount >= total

  const indeterminate =
    !hasScanProgress && (
      (job?.status === 'scanning' && total === 0) ||
      (useScanOverlay && (scanState.mode === 'pages' || scanState.mode === 'baseline')) ||
      allRemainingRemuxing ||
      allRemainingUploading
    )

  let phaseText = ''
  if (job?.status === 'queued') {
    phaseText = '排队等待执行...'
  } else if (useScanOverlay && scanState.mode === 'targets' && Number(scanState.total || 0) > 0) {
    const scanTotal = Number(scanState.total)
    const scanned = Math.min(scanTotal, Math.max(0, Number(scanState.scanned || 0)))
    const currentName = String(scanState.current_target || '').trim()
    const foundNew = Math.max(0, Number(scanState.found_new || 0))
    const foundSuffix = foundNew > 0 ? ` · 已发现 ${foundNew} 新` : ''
    if (scanned < scanTotal) {
      phaseText = `正在扫描目标 (${scanned}/${scanTotal})${currentName ? ` · ${currentName}` : ''}${foundSuffix}`
    } else {
      phaseText = `目标扫描完成 (${scanTotal}/${scanTotal})${foundSuffix}`
    }
  } else if (useScanOverlay && scanState.mode === 'baseline') {
    const page = Math.max(1, Number(scanState.page || 1))
    const found = Math.max(0, Number(scanState.found_videos || 0))
    const targetName = String(scanState.current_target || '').trim()
    phaseText = `正在扫描基线${targetName ? `「${targetName}」` : ''}第 ${page} 页${found > 0 ? `（已收集 ${found} 个）` : '...'}`
  } else if (useScanOverlay && scanState.mode === 'pages') {
    const page = Math.max(1, Number(scanState.page || 1))
    const found = Math.max(0, Number(scanState.found_videos || 0))
    const targetName = String(scanState.current_target || '').trim()
    const tIdx = Number(scanState.target_index || 0)
    const tTot = Number(scanState.total_targets || 0)
    const prefix = tTot > 1 && tIdx > 0 ? `(${tIdx}/${tTot}) ` : ''
    phaseText = `${prefix}正在扫描${targetName ? `「${targetName}」` : ''}第 ${page} 页${found > 0 ? `（已收集 ${found} 个待处理）` : '...'}`
  } else if (job?.status === 'scanning' && total === 0) {
    phaseText = '正在扫描页面并解析视频信息...'
  } else if (isActive) {
    if (job?.kind === 'enrich_metadata') {
      phaseText =
        total > 0
          ? `正在补全基线元数据 (${finishedCount}/${total})...`
          : '正在解析基线视频元数据...'
    } else if (downloadingCount > 0 || remuxingCount > 0 || uploadingCount > 0) {
      const parts = []
      if (downloadingCount > 0) parts.push(`下载 (${downloadingCount})`)
      if (remuxingCount > 0) parts.push(`封装 MP4 (${remuxingCount})`)
      if (uploadingCount > 0) parts.push(`云端上传 (${uploadingCount})`)
      if (parts.length > 1) {
        phaseText = `正在并发${parts.join(' · ')}`
      } else if (downloadingCount > 0) {
        phaseText = `正在下载 ${downloadingCount} 个视频`
      } else if (remuxingCount > 0) {
        phaseText = `正在封装 MP4 (${remuxingCount} 个视频)...`
      } else {
        phaseText = `正在校验上传 ${uploadingCount} 个视频至云端...`
      }
    } else {
      phaseText = '正在准备下载队列...'
    }
  }

  return {
    total,
    done,
    uploaded,
    skipped,
    failed,
    finishedCount,
    percent,
    hasScanProgress,
    speedBps,
    downloadSpeedBps,
    uploadSpeedBps,
    downloadingCount,
    remuxingCount,
    uploadingCount,
    indeterminate,
    phaseText,
    durationText: formatDurationCompact(job?.started_at, job?.finished_at),
  }
}

const ITEM_STATUS_ORDER = [
  'downloading',
  'uploading',
  'downloaded',
  'pending',
  'done',
  'skipped',
  'failed',
  'cancelled',
]

const ITEM_STATUS_PILL_META: Partial<Record<string, { label: string; dot: string }>> = {
  downloading: { label: '下载中', dot: 'bg-brand' },
  uploading: { label: '上传中', dot: 'bg-amber-500' },
  downloaded: { label: '已落盘', dot: 'bg-sky-500' },
  pending: { label: '等待中', dot: 'bg-zinc-400' },
  done: { label: '已上传', dot: 'bg-emerald-500' },
  skipped: { label: '已跳过', dot: 'bg-zinc-400' },
  failed: { label: '失败', dot: 'bg-red-500' },
  cancelled: { label: '已取消', dot: 'bg-zinc-400' },
}

const METADATA_ITEM_STATUS_LABELS: Partial<Record<string, string>> = {
  pending: '等待补全',
  done: '已补全',
  failed: '失败',
  cancelled: '已取消',
}

function getItemStatusPillMeta(status: string, isMetadataJob = false) {
  const base = ITEM_STATUS_PILL_META[status] || { label: status || '未知', dot: 'bg-zinc-400' }
  const metadataLabel = METADATA_ITEM_STATUS_LABELS[status]
  if (isMetadataJob && metadataLabel) {
    return { label: metadataLabel, dot: base.dot }
  }
  return base
}

export function buildItemStatusPills(
  items: Pick<JobItemRecord, 'status'>[] = [],
  { isMetadataJob = false, activeFilter = 'all' } = {},
) {
  const list = Array.isArray(items) ? items : []
  const counts: Partial<Record<string, number>> = Object.create(null)
  const extraStatuses: string[] = []

  for (const it of list) {
    const status = String(it?.status || '').trim()
    if (!status) continue
    const count = counts[status]
    if (count == null) {
      counts[status] = 0
      if (!ITEM_STATUS_ORDER.includes(status)) {
        extraStatuses.push(status)
      }
    }
    counts[status] = (count ?? 0) + 1
  }

  const pills = [{ value: 'all', label: '全部', count: list.length, dot: '' }]
  const orderedStatuses = [...ITEM_STATUS_ORDER, ...extraStatuses]

  if (activeFilter && activeFilter !== 'all' && !orderedStatuses.includes(activeFilter)) {
    orderedStatuses.push(activeFilter)
  }

  for (const status of orderedStatuses) {
    const count = counts[status] || 0
    if (count > 0 || activeFilter === status) {
      const meta = getItemStatusPillMeta(status, isMetadataJob)
      pills.push({
        value: status,
        label: meta.label,
        count,
        dot: meta.dot,
      })
    }
  }

  return pills
}

export function filterJobItemsByStatus<T extends Pick<JobItemRecord, 'status'>>(items: T[] = [], filter = 'all'): T[] {
  const list = Array.isArray(items) ? items : []
  if (!filter || filter === 'all') return list
  return list.filter((it) => it?.status === filter)
}
