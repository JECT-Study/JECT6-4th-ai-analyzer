from dataclasses import dataclass

import httpx

from app.core.config import get_settings
from app.core.exceptions import ExternalServiceError, ValidationError
from app.core.url_security import validate_crawl_destination

_HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")


@dataclass(frozen=True)
class CrawledPage:
    url: str
    html: str
    http_status: int
    content_type: str


class CrawlerClient:
    """Fetch HTML pages using safe defaults for user-provided URLs."""

    def __init__(self) -> None:
        self._settings = get_settings()

    async def fetch(self, url: str) -> CrawledPage:
        safe_url = await validate_crawl_destination(url)
        timeout = httpx.Timeout(self._settings.crawl_request_timeout_seconds)
        headers = {"User-Agent": self._settings.crawl_user_agent}

        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                headers=headers,
            ) as client:
                async with client.stream("GET", safe_url) as response:
                    self._validate_response(response)
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self._settings.crawl_max_response_bytes:
                            raise ValidationError("crawled response is too large")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
        except ValidationError:
            raise
        except httpx.HTTPError as exc:
            raise ExternalServiceError("crawl request failed") from exc

        encoding = response.encoding or "utf-8"
        html = raw.decode(encoding, errors="replace")
        return CrawledPage(
            url=safe_url,
            html=html,
            http_status=response.status_code,
            content_type=response.headers.get("content-type", ""),
        )

    @staticmethod
    def _validate_response(response: httpx.Response) -> None:
        if response.status_code < 200 or response.status_code >= 300:
            raise ExternalServiceError(
                f"crawl request returned status {response.status_code}"
            )
        content_type = response.headers.get("content-type", "").lower()
        if content_type and not any(t in content_type for t in _HTML_CONTENT_TYPES):
            raise ValidationError("crawl url did not return html")
