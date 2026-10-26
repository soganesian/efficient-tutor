"""Inline every <img src="shots/..."> as a data URI so report.html and slides.html are self-contained."""

import base64
import mimetypes
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def inline(src: Path, dst: Path) -> None:
    html = src.read_text(encoding="utf-8")

    def repl(match: re.Match) -> str:
        path = HERE / match.group(2)
        if not path.exists():
            print(f"missing image: {path}", file=sys.stderr)
            return match.group(0)
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return f'{match.group(1)}data:{mime};base64,{data}"'

    html = re.sub(r'(<img[^>]*?src=")(shots/[^"]+)"', repl, html)
    dst.write_text(html, encoding="utf-8")
    print(f"{dst.name}: {dst.stat().st_size // 1024} KB")


for name in ("report", "slides"):
    source = HERE / f"{name}.src.html"
    if source.exists():
        inline(source, HERE / f"{name}.html")
