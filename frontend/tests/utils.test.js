import test from 'node:test'
import assert from 'node:assert/strict'

import { parseVideoLinks } from '../src/utils/videoLinks.js'
import { buildExportFilename } from '../src/utils/exportFilename.js'
import { formatBytes, formatSpeed } from '../src/utils/format.js'
import { parseSpecificVideoDraft, serializeSpecificVideoSpecs } from '../src/utils/specificVideos.js'
import {
  buildItemStatusPills,
  computeJobLiveMetrics,
  filterJobItemsByStatus,
  resolveJobVisual,
} from '../src/utils/jobMeta.js'

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
