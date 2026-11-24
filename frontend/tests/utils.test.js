import test from 'node:test'
import assert from 'node:assert/strict'

import { parseVideoLinks } from '../src/utils/videoLinks.js'
import { buildExportFilename } from '../src/utils/exportFilename.js'
import { formatBytes, formatSpeed } from '../src/utils/format.js'
import { parseSpecificVideoDraft, serializeSpecificVideoSpecs } from '../src/utils/specificVideos.js'
import { computeJobLiveMetrics, resolveJobVisual } from '../src/utils/jobMeta.js'

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
