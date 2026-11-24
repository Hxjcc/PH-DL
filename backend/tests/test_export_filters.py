import asyncio
import json
from collections import Counter
from urllib.parse import parse_qs, urlparse

import pytest

from app.config import EnvSettings
from app.db.database import Database
from app.db.repo import Repository
from app.engine.events import EventBus
from app.engine.task_manager import TaskManager
from app.parsers.http_session import PowFetchResult, TaskHttpContext
from app.parsers.list_scanner import ListedVideo
from app.parsers.video import extract_viewkey_from_url
from app.services.cookie import CookieCheckResult


@pytest.mark.parametrize('source_mode', ['urls', 'target'])
@pytest.mark.parametrize('filters,expected', [
    ({'quality': '2160', 'keywords': 'demo', 'exclude_keywords': 'skip'}, {'vkgood'}),
    ({'quality': '2160,1080', 'keywords': 'DeMo', 'exclude_keywords': 'SKIP'}, {'vkgood', 'vklow'}),
    ({'quality': '2160', 'keywords': 'demo,other', 'exclude_keywords': 'skip'}, {'vkgood', 'vkother'}),
    ({'keywords': 'absent'}, set()),
    ({}, {'vkgood', 'vkexcluded', 'vkother', 'vklow'}),
])
async def test_export_only_applies_real_parser_filters_without_downloads(tmp_path, monkeypatch, source_mode, filters, expected):
    from app.engine import task_manager as engine

    env = EnvSettings(data_dir=tmp_path / 'data')
    env.ensure_dirs()
    db = Database(env.db_path)
    await db.connect()
    repo = Repository(db)
    manager = TaskManager(env=env, repo=repo, events=EventBus())
    titles = {'vkgood': 'Demo accepted', 'vkexcluded': 'Demo SKIP', 'vkother': 'Other title', 'vklow': 'Demo low quality', 'vkbad': 'Broken'}
    urls = [f'https://www.pornhub.com/view_video.php?viewkey={key}' for key in titles]
    urls += ['https://www.pornhub.com/embed/vkgood', urls[0]]
    fetched_pages = Counter()

    async def fake_check_cookie(*args, **kwargs):
        return CookieCheckResult(valid=True, message='ok')

    async def fake_fetch(self, url, **kwargs):
        if urlparse(url).hostname == 'media.example':
            key = parse_qs(urlparse(url).query)['key'][0]
            options = [{'format': 'mp4', 'quality': '1080' if key == 'vklow' else '2160', 'videoUrl': f'https://cdn.example/{key}.mp4'}]
            return PowFetchResult(json.dumps(options), url, 200, 0, False), None
        key = extract_viewkey_from_url(url)
        fetched_pages[key] += 1
        if key == 'vkbad':
            return PowFetchResult('', url, 404, 0, False), None
        assert key in titles
        data = {'video_title': titles[key], 'mediaDefinitions': [{'remote': True, 'format': 'mp4', 'videoUrl': f'https://media.example/get?key={key}'}]}
        html = f'<script>var flashvars_1 = {json.dumps(data)};</script><div class="userInfo"><a href="/model/alice">Alice</a></div>'
        return PowFetchResult(html, url, 200, 0, False), None

    async def fake_scan(*args, **kwargs):
        return [ListedVideo(extract_viewkey_from_url(url), url, '', 1, index + 1, len(urls)) for index, url in enumerate(urls)]

    async def forbid_media_work(*args, **kwargs):
        raise AssertionError('Export-only tasks must not download or upload media')

    monkeypatch.setattr(engine, 'check_cookie_valid', fake_check_cookie)
    monkeypatch.setattr(TaskHttpContext, 'fetch_with_pow', fake_fetch)
    monkeypatch.setattr(engine, 'scan_list_pages', fake_scan)
    monkeypatch.setattr(engine, 'download_mp4_with_resume', forbid_media_work)
    monkeypatch.setattr(engine, 'upload_file_batch', forbid_media_work)
    try:
        await repo.update_settings_patch({'ph_cookie': 'sess=test', 'preferred_quality': ''})
        manager.start()
        params = {'urls': urls, 'usernames': ['alice'], 'target_type': 'model', 'export_only': True, 'auto_upload': True, 'redownload': True, **filters}
        job = await manager.enqueue_job(f'manual_{source_mode}', 'Filtered export', params)
        for _ in range(100):
            result = await repo.get_job(job['id'])
            if result['status'] in {'completed', 'partial_failed', 'failed'}:
                break
            await asyncio.sleep(0.05)
        assert result['status'] == 'partial_failed'  # vkbad is a real 404, not a filter rejection
        links = result['export_text'].splitlines()
        assert {extract_viewkey_from_url(url) for url in links} == expected
        assert len(links) == len(expected)
        assert result['done_items'] == len(expected)
        assert result['failed_items'] == 1
        assert result['total_items'] == len(expected) + 1
        if source_mode == 'urls':
            assert fetched_pages == {key: 1 for key in titles}
        assert not await repo.list_job_items(job['id'])
        assert all('cdn.example' not in url for url in links)
        assert not list(env.downloads_dir.rglob('*.mp4'))
    finally:
        await manager.stop()
        await db.close()
