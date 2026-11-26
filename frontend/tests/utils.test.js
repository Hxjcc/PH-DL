import test from 'node:test'
import assert from 'node:assert/strict'

import { parseVideoLinks } from '../src/utils/videoLinks.js'
import { buildExportFilename } from '../src/utils/exportFilename.js'
import { formatBytes, formatPublishedDate, formatSpeed, formatVideoDuration, siteMeta, targetTypeMeta } from '../src/utils/format.js'
import { parseSpecificVideoDraft, serializeSpecificVideoSpecs } from '../src/utils/specificVideos.js'
import {
  buildItemStatusPills,
  computeJobLiveMetrics,
  filterJobItemsByStatus,
  resolveJobVisual,
} from '../src/utils/jobMeta.js'
import { appendLiveLog, mergeLogsSnapshot } from '../src/utils/logBuffer.js'
import {
  buildTargetProfileVideosUrl,
  extractTargetFromInput,
  isXhamsterUrlText,
  normalizeTargetUsername,
  parseTargetUsernames,
} from '../src/utils/targetInput.js'
import {
  JOB_LIMITS,
  TARGETS_PAGE_SIZES,
  readListPreference,
  saveListPreference,
} from '../src/utils/listPreferences.js'

test('parseVideoLinks deduplicates by viewkey and counts invalid rows', () => {
  const input = [
    '# comment line',
    'https://www.pornhub.com/view_video.php?viewkey=abc123',
    'https://www.pornhub.com/embed/abc123',
    'https://de.pornhub.com/shorties/xyz789',
    'https://evil.example/view_video.php?viewkey=bad1',
    'not-a-url',
  ].join('\n')

  const res = parseVideoLinks(input)
  assert.deepEqual(res.urls, [
    'https://www.pornhub.com/view_video.php?viewkey=abc123',
    'https://de.pornhub.com/shorties/xyz789',
  ])
  assert.equal(res.duplicates, 1)
  assert.equal(res.invalid, 2)
})

test('parseSpecificVideoDraft and serializeSpecificVideoSpecs support single and multi-page specs', () => {
  let step1 = parseSpecificVideoDraft('1, 3-5', [], 1)
  assert.equal(step1.invalidFound, false)
  assert.deepEqual(step1.tags, ['1页:1,3-5'])

  let step2 = parseSpecificVideoDraft('2, 4', step1.tags, 3)
  assert.equal(step2.invalidFound, false)
  assert.deepEqual(step2.tags, ['1页:1,3-5', '3页:2,4'])

  let step3 = parseSpecificVideoDraft('2:6-8; 1-9', step2.tags, 1)
  assert.equal(step3.invalidFound, false)
  assert.equal(serializeSpecificVideoSpecs(step3.tags, 1), '1-1,3-5,9;2-6-8;3-2,4')

  const bad = parseSpecificVideoDraft('8-3', [], 1)
  assert.equal(bad.invalidFound, true)
  assert.deepEqual(bad.tags, [])
})

test('buildExportFilename sanitizes illegal characters and appends active filters', () => {
  const filename = buildExportFilename({
    id: 12,
    kind: 'manual_target',
    params: {
      usernames: ['alice', 'bob'],
      target_type: 'favorites',
      pages: '1-3',
      quality: '2160,1080',
      keywords: 'cat:dog*',
    },
  })
  assert.equal(filename, 'alice+bob_favorites_P1-3_Q2160,1080_KWcatdog.txt')
})

test('formatBytes and formatSpeed format human readable sizes', () => {
  assert.equal(formatBytes(0), '0 B')
  assert.equal(formatBytes(1536), '1.5 KB')
  assert.equal(formatSpeed(10 * 1024 * 1024), '10.0 MB/s')
})

test('resolveJobVisual extracts category, display title, avatars, and capsules', () => {
  const targets = [
    {
      id: 3,
      username: 'kkfox-baby',
      display_name: 'KKFox baby',
      target_type: 'model',
      avatar_url: 'https://cdn.example/kkfox.jpg',
      enabled: true,
    },
  ]

  const dlVisual = resolveJobVisual(
    {
      id: 18,
      kind: 'manual_target',
      label: '下载目标: kkfox-baby',
      params: { usernames: ['kkfox-baby'], target_type: 'model', pages: '1-2', quality: '1080', auto_upload: true },
    },
    targets
  )
  assert.equal(dlVisual.category.label, '目标下载')
  assert.equal(dlVisual.category.group, 'download')
  assert.equal(dlVisual.title, 'KKFox baby')
  assert.equal(dlVisual.avatars.length, 1)
  assert.equal(dlVisual.avatars[0].avatarUrl, 'https://cdn.example/kkfox.jpg')
  assert.ok(dlVisual.capsules.some((c) => c.text === '@kkfox-baby'))
  assert.ok(dlVisual.capsules.some((c) => c.text === '第 1-2 页'))
  assert.ok(dlVisual.capsules.some((c) => c.text === '1080p'))

  const updVisual = resolveJobVisual(
    {
      id: 15,
      kind: 'auto_update',
      label: '指定追更: KKFox baby',
      params: { target_ids: [3] },
    },
    targets
  )
  assert.equal(updVisual.category.label, '指定追更')
  assert.equal(updVisual.category.group, 'update')
  assert.equal(updVisual.title, 'KKFox baby')
  assert.ok(updVisual.capsules.some((c) => c.text === '@kkfox-baby' && c.handle === true))

  // Also shows @username uniformly even when display_name only differs by case
  const sameCaseVisual = resolveJobVisual(
    {
      id: 49,
      kind: 'manual_target',
      label: '下载目标: ariavix',
      params: { usernames: ['ariavix'], target_type: 'model', auto_upload: true },
    },
    [{ id: 9, username: 'ariavix', display_name: 'AriaVix', target_type: 'model', avatar_url: '', enabled: true }]
  )
  assert.equal(sameCaseVisual.title, 'AriaVix')
  assert.ok(sameCaseVisual.capsules.some((c) => c.text === '@ariavix' && c.handle === true))

  // Baseline sync single-target also shows @username uniformly
  const baseVisual = resolveJobVisual(
    {
      id: 48,
      kind: 'baseline_sync',
      label: '初始化基线: AriaVix (model)',
      params: { target_id: 9, pages: 'all' },
    },
    [{ id: 9, username: 'ariavix', display_name: 'AriaVix', target_type: 'model', avatar_url: '', enabled: true }]
  )
  assert.ok(baseVisual.capsules.some((c) => c.text === '@ariavix' && c.handle === true))
})

test('computeJobLiveMetrics includes fractional byte progress and speed while done_items is 0', () => {
  const job = { id: 18, kind: 'manual_target', status: 'running', total_items: 9, done_items: 0 }
  const liveProgress = {
    101: { job_id: 18, item_id: 101, bytes_downloaded: 50 * 1024 * 1024, bytes_total: 100 * 1024 * 1024, speed_bps: 12 * 1024 * 1024 },
    102: { job_id: 18, item_id: 102, bytes_downloaded: 40 * 1024 * 1024, bytes_total: 100 * 1024 * 1024, speed_bps: 8 * 1024 * 1024 },
  }
  const metrics = computeJobLiveMetrics(job, liveProgress, null)
  assert.equal(metrics.percent, 10)
  assert.equal(metrics.speedBps, 20 * 1024 * 1024)
  assert.equal(metrics.downloadingCount, 2)
})

test('buildItemStatusPills groups items by fine-grained status in workflow order', () => {
  const items = [
    { id: 1, status: 'done' },
    { id: 2, status: 'uploading' },
    { id: 3, status: 'pending' },
    { id: 4, status: 'downloading' },
    { id: 5, status: 'downloaded' },
    { id: 6, status: 'failed' },
    { id: 7, status: 'skipped' },
    { id: 8, status: 'pending' },
    { id: 9, status: 'cancelled' },
  ]

  const pills = buildItemStatusPills(items)
  assert.deepEqual(
    pills.map((p) => [p.value, p.label, p.count]),
    [
      ['all', '全部', 9],
      ['downloading', '下载中', 1],
      ['uploading', '上传中', 1],
      ['downloaded', '已落盘', 1],
      ['pending', '等待中', 2],
      ['done', '已上传', 1],
      ['skipped', '已跳过', 1],
      ['failed', '失败', 1],
      ['cancelled', '已取消', 1],
    ]
  )
  assert.equal(pills[0].dot, '')
  assert.equal(pills[1].dot, 'bg-brand')
  assert.equal(pills[2].dot, 'bg-amber-500')

  assert.deepEqual(
    filterJobItemsByStatus(items, 'pending').map((x) => x.id),
    [3, 8]
  )
  assert.equal(filterJobItemsByStatus(items, 'all').length, 9)
})

test('buildItemStatusPills supports metadata labels and retains selected zero-count filter', () => {
  const items = [
    { id: 1, status: 'done' },
    { id: 2, status: 'pending' },
  ]
  const metaPills = buildItemStatusPills(items, { isMetadataJob: true, activeFilter: 'failed' })
  assert.deepEqual(
    metaPills.map((p) => [p.value, p.label, p.count]),
    [
      ['all', '全部', 2],
      ['pending', '等待补全', 1],
      ['done', '已补全', 1],
      ['failed', '失败', 0],
    ]
  )
})

test('resolveJobVisual target lookup cache reflects refreshed target arrays', () => {
  const job = {
    id: 21,
    kind: 'manual_target',
    label: '下载目标: alice',
    params: { usernames: ['alice'], target_type: 'model' },
  }
  const targetsV1 = [{ id: 1, username: 'alice', display_name: 'Alice V1', target_type: 'model', avatar_url: '' }]
  const targetsV2 = [{ id: 1, username: 'alice', display_name: 'Alice V2', target_type: 'model', avatar_url: '' }]

  assert.equal(resolveJobVisual(job, targetsV1).title, 'Alice V1')
  assert.equal(resolveJobVisual(job, targetsV1).title, 'Alice V1')
  assert.equal(resolveJobVisual(job, targetsV2).title, 'Alice V2')
})

test('computeJobLiveMetrics separates download and upload speeds by progress phase', () => {
  const job = { id: 42, kind: 'manual_target', status: 'running', total_items: 10, done_items: 2 }
  const liveProgress = {
    201: { job_id: 42, item_id: 201, bytes_downloaded: 50, bytes_total: 100, speed_bps: 60 * 1024 * 1024, phase: 'downloading' },
    202: { job_id: 42, item_id: 202, bytes_downloaded: 70, bytes_total: 100, speed_bps: 95 * 1024 * 1024, phase: 'uploading' },
    203: { job_id: 42, item_id: 203, bytes_downloaded: 100, bytes_total: 100, speed_bps: 0, phase: 'verifying' },
  }
  const metrics = computeJobLiveMetrics(job, liveProgress, null)
  assert.equal(metrics.downloadingCount, 1)
  assert.equal(metrics.uploadingCount, 2)
  assert.equal(metrics.downloadSpeedBps, 60 * 1024 * 1024)
  assert.equal(metrics.uploadSpeedBps, 95 * 1024 * 1024)
  assert.equal(metrics.speedBps, 60 * 1024 * 1024)
  assert.equal(metrics.percent, 25)
})

test('logBuffer discards stale pre-restart logs when backend boot_id resets log ids', () => {
  const preRestart = [
    { id: 1, boot_id: 'boot-old', ts: '2026-11-25T07:40:00+00:00', message: 'old 1' },
    { id: 2, boot_id: 'boot-old', ts: '2026-11-25T07:40:01+00:00', message: 'old 2' },
    { id: 3, boot_id: 'boot-old', ts: '2026-11-25T07:40:02+00:00', message: 'old 3' },
  ]
  const afterRestartSnapshot = [
    { id: 1, boot_id: 'boot-new', ts: '2026-11-25T08:02:00+00:00', message: 'PH-DL 后端已启动' },
  ]
  const merged = mergeLogsSnapshot(preRestart, afterRestartSnapshot)
  assert.deepEqual(
    merged.map((x) => `${x.boot_id}:${x.id}`),
    ['boot-new:1']
  )

  // Live WS log with id=2 from new boot must NOT be dropped as duplicate
  const afterLive = appendLiveLog(merged, {
    id: 2,
    boot_id: 'boot-new',
    ts: '2026-11-25T08:05:00+00:00',
    message: '开始检查 34 个追更目标的更新...',
  })
  assert.equal(afterLive.length, 2)
  assert.equal(afterLive[1].message, '开始检查 34 个追更目标的更新...')

  // Even if live WS log arrives directly before snapshot with a new boot_id, reset cleanly
  const directReset = appendLiveLog(preRestart, {
    id: 2,
    boot_id: 'boot-new',
    ts: '2026-11-25T08:05:00+00:00',
    message: 'fresh log',
  })
  assert.deepEqual(
    directReset.map((x) => `${x.boot_id}:${x.id}`),
    ['boot-new:2']
  )

  // Legacy cached logs without boot_id are cleanly replaced by new boot_id snapshot
  const legacyCached = [
    { id: 1, ts: '2026-11-25T07:40:00+00:00', message: 'legacy 1' },
    { id: 50, ts: '2026-11-25T07:40:47+00:00', message: 'legacy 50' },
  ]
  const upgraded = mergeLogsSnapshot(legacyCached, afterRestartSnapshot)
  assert.deepEqual(
    upgraded.map((x) => `${x.boot_id}:${x.id}`),
    ['boot-new:1']
  )
})

test('computeJobLiveMetrics renders determinate scan bar for target updates and page text for single-target scans', () => {
  const updateJob = { id: 77, kind: 'check_updates', status: 'scanning', total_items: 0, done_items: 0 }
  const targetScan = computeJobLiveMetrics(updateJob, {}, [], {
    77: { mode: 'targets', scanned: 17, total: 34, current_target: 'Kuporovaa Krupa', found_new: 2 },
  })
  assert.equal(targetScan.indeterminate, false)
  assert.equal(targetScan.hasScanProgress, true)
  assert.equal(targetScan.percent, 50)
  assert.match(targetScan.phaseText, /17\/34/)
  assert.match(targetScan.phaseText, /Kuporovaa Krupa/)
  assert.match(targetScan.phaseText, /已发现 2 新/)

  const manualJob = { id: 78, kind: 'manual_target', status: 'scanning', total_items: 0, done_items: 0 }
  const pageScan = computeJobLiveMetrics(manualJob, {}, [], {
    78: { mode: 'pages', target_index: 1, total_targets: 1, current_target: 'alice', page: 3, found_videos: 42 },
  })
  assert.equal(pageScan.indeterminate, true)
  assert.equal(pageScan.hasScanProgress, false)
  assert.match(pageScan.phaseText, /alice/)
  assert.match(pageScan.phaseText, /第 3 页/)
  assert.match(pageScan.phaseText, /42/)
})

test('parseTargetUsernames splits commas/newlines, strips profile URLs, ignores non-profile URLs, and deduplicates', () => {
  const input = [
    'alice, bob，ALICE',
    'https://www.pornhub.com/model/candy-love/videos @charlie; https://www.pornhub.com/pornstar/dana',
    'https://www.pornhub.com/channels/unsupported-chan',
    'https://www.pornhub.com/view_video.php?viewkey=ph12345',
    'bad/path',
    'has?query',
  ].join('\n')
  assert.deepEqual(parseTargetUsernames(input), ['alice', 'bob', 'candy-love', 'charlie', 'dana'])
})

test('buildTargetProfileVideosUrl builds /videos profile links for all target types', () => {
  assert.equal(
    buildTargetProfileVideosUrl({ username: 'amatemure', target_type: 'model' }),
    'https://www.pornhub.com/model/amatemure/videos'
  )
  assert.equal(
    buildTargetProfileVideosUrl({ username: 'aria', target_type: 'pornstar' }),
    'https://www.pornhub.com/pornstar/aria/videos'
  )
  assert.equal(
    buildTargetProfileVideosUrl({ username: 'bob', target_type: 'users' }),
    ''
  )
  assert.equal(
    buildTargetProfileVideosUrl({ username: 'Hxiay', target_type: 'favorites' }),
    'https://www.pornhub.com/users/hxiay/videos/favorites'
  )
  assert.equal(
    buildTargetProfileVideosUrl({ username: '_currentUser_', target_type: 'liked' }),
    'https://www.pornhub.com/likedvideos'
  )
})

test('formatPublishedDate, formatVideoDuration, and enrich_metadata phase text', () => {
  assert.equal(formatPublishedDate('2026-10-05T10:35:49+00:00'), '2026-10-05')
  assert.equal(formatPublishedDate('2026-10-05'), '2026-10-05')
  assert.equal(formatPublishedDate(''), '')

  assert.equal(formatVideoDuration(759), '12:39')
  assert.equal(formatVideoDuration(3920), '1:05:20')
  assert.equal(formatVideoDuration(0), '')

  const runningMeta = computeJobLiveMetrics(
    { id: 89, kind: 'enrich_metadata', status: 'running', total_items: 100, done_items: 12 },
    {},
    null
  )
  assert.equal(runningMeta.phaseText, '正在补全基线元数据 (12/100)...')
})

test('listPreferences supports 10-item job limit and target page sizes including 0 (all)', () => {
  assert.ok(JOB_LIMITS.includes(10))
  assert.ok(TARGETS_PAGE_SIZES.includes(0))
  const backing = new Map()
  const prevStorage = globalThis.localStorage
  globalThis.localStorage = {
    getItem: (k) => (backing.has(k) ? backing.get(k) : null),
    setItem: (k, v) => backing.set(k, String(v)),
  }
  try {
    // Missing key must return fallback (24), not 0 via Number(null)
    assert.equal(readListPreference('targets', TARGETS_PAGE_SIZES, 24), 24)
    saveListPreference('targets', 0)
    assert.equal(readListPreference('targets', TARGETS_PAGE_SIZES, 24), 0)
    saveListPreference('jobs', 10)
    assert.equal(readListPreference('jobs', JOB_LIMITS, 30), 10)
  } finally {
    globalThis.localStorage = prevStorage
  }
})

test('parseVideoLinks supports xHamster modern/legacy URLs and mixed PornHub deduplication', () => {
  const input = [
    'https://www.pornhub.com/view_video.php?viewkey=ph66aa11',
    'https://xhamster.com/videos/some-video-title-xhOwQIZ',
    'https://zh.xhamster.com/videos/other-slug-xhOwQIZ?foo=1',
    'https://xhamster42.desi/movies/old-movie-12345678',
    'https://xhamster.com/embed/12345678',
    'https://xhamster.com/embed/xhAbCd99',
    'https://xhamster.com/creators/webtolove',
  ].join('\n')

  const res = parseVideoLinks(input)
  assert.deepEqual(res.urls, [
    'https://www.pornhub.com/view_video.php?viewkey=ph66aa11',
    'https://xhamster.com/videos/some-video-title-xhOwQIZ',
    'https://xhamster42.desi/movies/old-movie-12345678',
    'https://xhamster.com/embed/xhAbCd99',
  ])
  assert.equal(res.duplicates, 2)
  assert.equal(res.invalid, 1)
})

test('xHamster targetInput helpers detect site, extract targets, normalize usernames, and build profile URLs', () => {
  assert.equal(isXhamsterUrlText('https://xhamster.com/creators/luna-star'), true)
  assert.equal(isXhamsterUrlText('https://zh.xhamster42.desi/users/webtolove/videos'), true)
  assert.equal(isXhamsterUrlText('https://www.pornhub.com/model/alice'), false)

  assert.deepEqual(extractTargetFromInput('https://xhamster.com/creators/luna-star/newest'), {
    site: 'xhamster',
    target_type: 'creator',
    username: 'luna-star',
  })
  assert.deepEqual(extractTargetFromInput('https://xhamster.com/pornstars/eva-elfie'), {
    site: 'xhamster',
    target_type: 'pornstar',
    username: 'eva-elfie',
  })
  assert.deepEqual(extractTargetFromInput('https://xhamster.com/channels/brazzers-network/newest'), {
    site: 'xhamster',
    target_type: 'channel',
    username: 'brazzers-network',
  })
  assert.deepEqual(extractTargetFromInput('https://xhamster.com/users/webtolove/videos'), {
    site: 'xhamster',
    target_type: 'users',
    username: 'webtolove',
  })
  assert.deepEqual(extractTargetFromInput('https://xhamster.com/my/favorites/videos'), {
    site: 'xhamster',
    target_type: 'favorites',
    username: '',
  })

  assert.equal(normalizeTargetUsername('https://xhamster.com/creators/luna-star/newest'), 'luna-star')
  assert.equal(normalizeTargetUsername('https://xhamster.com/channels/brazzers-network'), 'brazzers-network')
  assert.equal(normalizeTargetUsername('https://xhamster.com/users/webtolove/videos'), 'webtolove')

  assert.equal(
    buildTargetProfileVideosUrl({ site: 'xhamster', username: 'luna-star', target_type: 'creator' }),
    'https://xhamster.com/creators/luna-star/newest'
  )
  assert.equal(
    buildTargetProfileVideosUrl({ site: 'xhamster', username: 'eva-elfie', target_type: 'pornstar' }),
    'https://xhamster.com/pornstars/eva-elfie/newest'
  )
  assert.equal(
    buildTargetProfileVideosUrl({ site: 'xhamster', username: 'brazzers-network', target_type: 'channel' }),
    'https://xhamster.com/channels/brazzers-network/newest'
  )
  assert.equal(
    buildTargetProfileVideosUrl({ site: 'xhamster', username: 'webtolove', target_type: 'users' }),
    'https://xhamster.com/users/webtolove/videos'
  )
  assert.equal(
    buildTargetProfileVideosUrl({ site: 'xhamster', username: '_currentUser_', target_type: 'favorites' }),
    'https://xhamster.com/my/favorites/videos'
  )
})

test('siteMeta and targetTypeMeta format xHamster and PornHub labels', () => {
  assert.equal(siteMeta('xhamster').code, 'XH')
  assert.equal(siteMeta('xhamster').label, 'xHamster')
  assert.equal(siteMeta('pornhub').code, 'PH')
  assert.equal(siteMeta('').label, 'PornHub')
  assert.equal(targetTypeMeta('creator'), 'Creator')
  assert.equal(targetTypeMeta('channel'), 'Channel')
})
