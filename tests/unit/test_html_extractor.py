import pytest

from app.core.exceptions import ValidationError
from app.service.html_extractor import HtmlExtractor


def test_extracts_article_text_and_removes_noise():
    html = """
    <html>
      <head><title>제목</title><style>.x{}</style></head>
      <body>
        <nav>메뉴</nav>
        <article>
          <h1>본문 제목</h1>
          <p>첫 문단</p>
          <script>alert(1)</script>
          <p>둘째 문단</p>
        </article>
        <footer>푸터</footer>
      </body>
    </html>
    """

    text = HtmlExtractor().extract_text(html)

    assert "본문 제목" in text
    assert "첫 문단" in text
    assert "둘째 문단" in text
    assert "메뉴" not in text
    assert "alert" not in text
    assert "푸터" not in text


def test_extract_title_uses_title_tag_first():
    html = "<html><head><title>문서 제목</title></head><body><h1>H1</h1></body></html>"

    assert HtmlExtractor().extract_title(html) == "문서 제목"


def test_raises_when_no_readable_text():
    with pytest.raises(ValidationError):
        HtmlExtractor().extract_text("<html><script>only()</script></html>")
