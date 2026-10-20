"""Big JSON answers for the studio page, kept off the event loop.

A learned song's analysis is ~5.6 MB of JSON, two thirds of it align_bands (the spectrum the jam lines a song up
by, on the server only); turning it into JSON took ~140 ms on the event loop at every song start, while the
speakers' stream and the lights waited. The page gets the analysis without what only the server uses, and the
dump runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import json

from aiohttp import web

# what the server keeps for itself: the jam's line-up spectrum, the rules' own sections and the sections as first found
SERVER_ONLY = ("align_bands", "sections_rules", "sections_found", "drops_found", "_songmap_bars")


def for_browser(doc):
    """A copy of an answer with any analysis in it slimmed for the page (top level, or under "analysis")."""
    if not isinstance(doc, dict):
        return doc
    out = {k: v for k, v in doc.items() if k not in SERVER_ONLY}
    if isinstance(out.get("analysis"), dict):
        out["analysis"] = {k: v for k, v in out["analysis"].items() if k not in SERVER_ONLY}
    return out


async def response(doc, status: int = 200) -> web.Response:
    """A JSON response for the page, slimmed (for_browser) and dumped in a worker thread."""
    body = await asyncio.get_running_loop().run_in_executor(None, lambda: json.dumps(for_browser(doc)).encode())
    return web.Response(body=body, status=status, content_type="application/json")
