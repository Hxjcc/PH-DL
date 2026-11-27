"""ServerChan notification service."""

from __future__ import annotations

from typing import Dict, List, Tuple

import aiohttp

from ..config import RuntimeSettings


async def send_serverchan(
    title: str,
    content: str,
    settings: RuntimeSettings,
    *,
    force: bool = False,
) -> Tuple[bool, str]:
    if not force and not settings.serverchan_enabled:
        return False, "ServerChan 未启用"
    if not (settings.serverchan_send_key or "").strip():
        return False, "未配置 ServerChan SendKey"

    api_url = f"{settings.serverchan_api_url.rstrip('/')}/{settings.serverchan_send_key.strip()}.send"
    is_turbo = "sctapi.ftqq.com" in api_url
    payload = {"title": title, "desp": content.replace("\n", "\n\n")}

    try:
        timeout = aiohttp.ClientTimeout(total=15.0)
        async with aiohttp.ClientSession(timeout=timeout) as client:
            if is_turbo:
                async with client.post(api_url, data=payload, proxy=settings.normalized_proxy, allow_redirects=False) as resp:
                    if resp.status < 200 or resp.status >= 300:
                        return False, f"ServerChan 返回 HTTP {resp.status}"
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
                ok = data.get("code") == 0 and data.get("data", {}).get("errno") == 0
            else:
                async with client.get(
                    api_url,
                    params={"text": title, "desp": content},
                    proxy=settings.normalized_proxy,
                    allow_redirects=False,
                ) as resp:
                    if resp.status < 200 or resp.status >= 300:
                        return False, f"ServerChan 返回 HTTP {resp.status}"
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
                ok = data.get("errno") == 0

            if ok:
                return True, "通知发送成功"
            err_msg = data.get("message") or data.get("errmsg") or "未知错误"
            return False, settings.redact_diagnostic(f"ServerChan 返回错误: {err_msg}")
    except Exception as err:
        return False, f"通知发送异常 ({type(err).__name__})"


def build_auto_update_report(
    new_by_target: Dict[str, int],
    uploaded_by_target: Dict[str, int],
    failed_urls_by_target: Dict[str, List[str]],
) -> Tuple[str, str]:
    title = "PH-DL 自动追更汇总"
    targets = list(dict.fromkeys([*new_by_target.keys(), *uploaded_by_target.keys(), *failed_urls_by_target.keys()]))
    if not targets:
        return title, "本次自动追更未发现新视频。"

    lines = ["本次自动追更结果："]
    for idx, name in enumerate(targets, start=1):
        n_new = int(new_by_target.get(name, 0))
        n_up = int(uploaded_by_target.get(name, 0))
        f_urls = failed_urls_by_target.get(name, [])
        parts = [f"新增 {n_new}", f"已上传 {n_up}"]
        if f_urls:
            parts.append(f"失败 {len(f_urls)}")
        lines.append(f"{idx}. **{name}**: {', '.join(parts)}")
        for u in f_urls:
            lines.append(f"   - 失败链接: {u}")

    return title, "\n".join(lines)
