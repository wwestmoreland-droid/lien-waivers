"""Where a button sends you back to. Pages pass `back` so an action taken on Today returns to
Today; only paths on this site are accepted."""
from __future__ import annotations

from urllib.parse import quote


def safe_back(back: str | None, default: str) -> str:
    if back and back.startswith("/") and not back.startswith("//") and "\\" not in back:
        return back
    return default


def with_msg(url: str, msg: str) -> str:
    if not msg:
        return url
    path, _, frag = url.partition("#")
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}msg={quote(msg)}" + (f"#{frag}" if frag else "")
