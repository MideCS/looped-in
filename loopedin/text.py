"""Turn HTML email bodies into readable plain text, stdlib only."""

import html
import re
from html.parser import HTMLParser

_BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote"}
_SKIP = {"style", "script", "head", "title"}


class _Extractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP and self._skip:
            self._skip -= 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _Extractor()
    parser.feed(markup)
    parser.close()
    return tidy(html.unescape("".join(parser.parts)))


def tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\xa0", " ").replace("\u200c", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# Gmail wraps long "On <date>, <name> wrote:" lines, so allow one line break inside it.
_QUOTE_HEADER = re.compile(
    r"^(On [^\n]{0,200}(\n[^\n]{0,120})?wrote:|-{2,} ?Original Message ?-{2,}|From: [^\n]+\n(Sent|Date): |_{10,})",
    re.MULTILINE)


def strip_quoted(text: str) -> str:
    """Drop the quoted earlier messages from a reply, keeping only what this sender wrote."""
    match = _QUOTE_HEADER.search(text)
    if match and match.start() > 0:
        text = text[:match.start()]
    kept = [line for line in text.splitlines() if not line.lstrip().startswith(">")]
    return tidy("\n".join(kept))
