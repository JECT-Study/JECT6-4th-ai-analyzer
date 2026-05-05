from bs4 import BeautifulSoup

from app.core.exceptions import ValidationError

_REMOVE_SELECTORS = ("script", "style", "nav", "footer", "header", "noscript")


class HtmlExtractor:
    """Extract readable text from crawled HTML."""

    def extract_text(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        for tag_name in _REMOVE_SELECTORS:
            for tag in soup.find_all(tag_name):
                tag.decompose()

        root = soup.find("article") or soup.find("main") or soup.body or soup
        text = root.get_text(separator="\n", strip=True)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        content = "\n\n".join(lines)
        if not content:
            raise ValidationError("no readable text found in crawled html")
        return content

    def extract_title(self, html: str) -> str | None:
        soup = BeautifulSoup(html, "html.parser")
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
            return title or None
        heading = soup.find(["h1", "h2"])
        if heading:
            title = heading.get_text(" ", strip=True)
            return title or None
        return None
