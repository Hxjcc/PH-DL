"""Environment settings and typed runtime configuration."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REDACTED_PLACEHOLDER = "********"

SENSITIVE_RUNTIME_FIELDS = frozenset(
    {
        "ph_password",
        "ph_cookie",
        "xh_cookie",
        "serverchan_send_key",
        "yescaptcha_client_key",
    }
)


def default_data_dir() -> Path:
    """Prefer the repository's root ``data/`` directory when running from source.

    This keeps local development consistent with Docker, where ``PHDL_DATA_DIR``
    is explicitly set to ``/data``. Installed/packaged runs fall back to the
    current working directory.
    """
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "docker-compose.yml").is_file() and (source_root / "backend").is_dir():
        return source_root / "data"
    return Path("./data")


def default_env_files() -> tuple[str, ...]:
    """Load root ``.env`` first, allowing ``backend/.env`` to override locally."""
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "docker-compose.yml").is_file() and (source_root / "backend").is_dir():
        return (str(source_root / ".env"), ".env")
    return (".env",)


class EnvSettings(BaseSettings):
    """Startup environment variables (Docker / .env)."""

    model_config = SettingsConfigDict(
        env_prefix="PHDL_",
        env_file=default_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Field(default_factory=default_data_dir)
    admin_password: str = Field(default="admin")
    secret_key: str = Field(default="")
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000)
    cookie_secure: bool = Field(default=False)
    allowed_origins: str = Field(default="")
    enable_docs: bool = Field(default=False)
    session_ttl_hours: int = Field(default=168)  # 7 days

    @property
    def resolved_data_dir(self) -> Path:
        return self.data_dir.expanduser().resolve()

    @property
    def db_path(self) -> Path:
        return self.resolved_data_dir / "phdl.db"

    @property
    def downloads_dir(self) -> Path:
        return self.resolved_data_dir / "downloads"

    @property
    def temp_dir(self) -> Path:
        return self.resolved_data_dir / "temp"

    @property
    def logs_dir(self) -> Path:
        return self.resolved_data_dir / "logs"

    def ensure_dirs(self) -> None:
        self.resolved_data_dir.mkdir(parents=True, exist_ok=True)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        try:
            # Keep the unified SQLite database and settings (including secrets)
            # private to the service account on POSIX systems.
            self.resolved_data_dir.chmod(0o700)
        except OSError:
            pass

    def get_or_create_secret_key(self) -> str:
        if self.secret_key.strip():
            return self.secret_key.strip()
        self.ensure_dirs()
        secret_file = self.resolved_data_dir / ".secret_key"
        if secret_file.exists():
            stored = secret_file.read_text(encoding="utf-8").strip()
            if len(stored) >= 32:
                return stored
        generated = secrets.token_urlsafe(48)
        secret_file.write_text(generated, encoding="utf-8")
        try:
            secret_file.chmod(0o600)
        except OSError:
            pass
        return generated

    def parsed_allowed_origins(self) -> List[str]:
        return [o.strip().rstrip("/") for o in self.allowed_origins.split(",") if o.strip()]


class SelectorSettings(BaseModel):
    list_page_main_containers: str = (
        'ul.videos#videoSearchResult, div#videoCategory ul.videos, '
        'ul#searchResults.videos.video-listing.clear, ul.videos[data-type="pornstar"], '
        'ul.videos.row-5-thumbs#mostRecentVideosSection, ul#mostRecentVideosSection, '
        'ul.videos.section-listing, ul#showAllChanelVideos, '
        'ul.videos.video-listing.clear.profileContent, ul.videos.video-listing.clear.playlistContent, '
        'div.container > ul.videos, ul#moreData.videos, ul#likedVideosSection.videos'
    )
    list_page_li_in_container: str = (
        "li.videoBox[data-video-vkey], li.pcVideoListItem[data-video-vkey], "
        "li.js-pop.videoblock.videoBox, li.pcVideoListItem.videoBox, li.videoBox, li.pcVideoListItem"
    )
    list_page_li_fallback: str = (
        "li[data-video-vkey], ul.videos > li[data-video-vkey], "
        "div.videoUList > ul > li.videoBox[data-video-vkey], ul.videos > li.videoBox, "
        "ul.videos > li.pcVideoListItem.videoblock, li.videoPreviewBg"
    )
    list_page_video_link_tag_primary: str = (
        'a[href*="view_video.php?viewkey="], a[href*="/video/show?viewkey="]'
    )
    list_page_video_link_tag_fallback_href: str = 'a[href*="viewkey="]'
    list_page_video_image_for_title: str = "img[alt]"
    list_page_video_span_div_for_title: str = "span.title, div.title, var.video_title"
    detail_page_h1_titles: str = (
        "h1.title span.inlineFree, h1.video-title span.inlineFree, "
        "h1.title.straightTitle, h1.title, h1.video-title, span.inlineFree"
    )
    detail_page_og_title_meta: str = 'meta[property="og:title"]'
    detail_page_title_tag: str = "title"
    detail_page_uploader_container: str = "div.userInfo, div.video-detailed-info, div.p_cnt"
    detail_page_uploader_link_in_container: str = (
        "a[href*='/model/'], a[href*='/pornstar/'], a[href*='/users/'], a[href*='/user/']"
    )
    subs_page_main_container: str = "ul#moreData, ul.userWidgetWrapperGrid"
    subs_page_user_link_in_container: str = "div.user-flag a.userLink[href]"
    pornstar_page_video_container_check: str = "div.profileVids"

    def split_list(self, field_name: str) -> List[str]:
        val = getattr(self, field_name, "") or ""
        return [s.strip() for s in val.split(",") if s.strip()]


class RuntimeSettings(BaseModel):
    """Dynamic settings stored in SQLite and editable from WebUI."""

    # Authentication & Cookie
    ph_username: str = ""
    ph_password: str = ""
    ph_logged_in_username: str = ""
    ph_cookie: str = ""
    xh_cookie: str = ""
    xh_logged_in_username: str = ""
    account_avatar_version: str = ""

    # YesCaptcha (reCAPTCHA solver used by auto-login)
    yescaptcha_enabled: bool = False
    yescaptcha_client_key: str = ""
    yescaptcha_api_base: str = "https://api.yescaptcha.com"
    yescaptcha_task_type: str = "RecaptchaV2EnterpriseTaskProxyless"
    yescaptcha_timeout_seconds: float = Field(default=180.0, ge=30.0, le=600.0)

    # Network & Retry
    proxy_url: str = ""
    request_timeout: float = Field(default=30.0, ge=5.0, le=180.0)
    max_retries_page_fetch: int = Field(default=3, ge=0, le=10)
    retry_delay_base: float = Field(default=2.0, ge=0.0, le=30.0)
    retry_delay_max: float = Field(default=10.0, ge=0.0, le=120.0)
    page_delay_min: float = Field(default=0.5, ge=0.0, le=10.0)
    page_delay_max: float = Field(default=1.5, ge=0.0, le=20.0)

    # Download & Backpressure
    max_concurrent_downloads: int = Field(default=4, ge=1, le=16)
    max_concurrent_video_details: int = Field(default=6, ge=1, le=40)
    max_concurrent_list_pages: int = Field(default=2, ge=1, le=10)
    download_attempts: int = Field(default=3, ge=1, le=10)
    min_file_size_bytes: int = Field(default=10240, ge=1)
    preferred_quality: str = ""  # e.g. "2160,1440,1080" or empty for best
    xh_preferred_codec: Literal["h264", "av1"] = "h264"
    upload_batch_size: int = Field(default=1, ge=1, le=100)
    upload_workers: int = Field(default=3, ge=1, le=8)
    max_pending_upload_files: int = Field(default=20, ge=1, le=200)
    min_free_disk_gb: float = Field(default=2.0, ge=0.0, le=500.0)

    # rclone Uploader
    uploader_enabled: bool = True
    rclone_bin: str = "rclone"
    rclone_remote: str = "pikpak"
    rclone_remote_base_dir: str = "Pornhub"
    rclone_xh_remote_base_dir: str = "xHamster"
    rclone_transfers: int = Field(default=1, ge=1, le=16)
    rclone_checkers: int = Field(default=2, ge=1, le=32)
    rclone_pikpak_upload_concurrency: int = Field(default=4, ge=1, le=32)
    rclone_pikpak_chunk_size: str = "32Mi"
    rclone_drive_chunk_size: str = "64Mi"
    rclone_drive_stop_on_upload_limit: bool = True
    rclone_extra_flags: str = ""
    rclone_ignore_checksum: bool = True
    rclone_ignore_size: bool = False
    rclone_verify_download: bool = False
    rclone_tpslimit: int = Field(default=0, ge=0, le=100)
    rclone_tpslimit_burst: int = Field(default=0, ge=0, le=100)
    rclone_log_level: str = "ERROR"
    rclone_timeout_seconds: int = Field(default=1800, ge=30, le=86400)

    # Auto-Update Scheduler
    schedule_enabled: bool = True
    schedule_time: str = "04:00"  # HH:MM local time
    update_target_concurrency: int = Field(default=2, ge=1, le=6)
    update_scan_mode: Literal["catch_up", "full"] = "catch_up"  # catch_up | full
    notify_if_no_updates: bool = False

    # ServerChan Notification
    serverchan_enabled: bool = False
    serverchan_send_key: str = ""
    serverchan_api_url: str = "https://sctapi.ftqq.com"

    # CSS Selectors
    selectors: SelectorSettings = Field(default_factory=SelectorSettings)

    @property
    def normalized_proxy(self) -> Optional[str]:
        p = (self.proxy_url or "").strip()
        return p or None

    def to_redacted_dict(self) -> dict[str, Any]:
        data = self.model_dump()
        for field in SENSITIVE_RUNTIME_FIELDS:
            if data.get(field):
                data[field] = REDACTED_PLACEHOLDER
        data["has_cookie"] = bool(self.ph_cookie.strip())
        data["has_xh_cookie"] = bool(self.xh_cookie.strip())
        return data

    def merge_update(self, patch: dict[str, Any]) -> "RuntimeSettings":
        current = self.model_dump()
        for key, value in patch.items():
            if key not in current:
                continue
            if key in SENSITIVE_RUNTIME_FIELDS and str(value) == REDACTED_PLACEHOLDER:
                continue
            if key == "selectors" and isinstance(value, dict):
                merged_sel = dict(current["selectors"])
                merged_sel.update(value)
                current["selectors"] = merged_sel
            else:
                current[key] = value
        return RuntimeSettings.model_validate(current)
