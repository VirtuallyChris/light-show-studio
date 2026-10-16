"""The full analysis of a song learned ahead, in a process of its own, below normal priority.

Learning ahead analyses a song a minute. Done inside the studio, that work shared one Python process
with the lights and the music, and a busy minute could hold them up. Here it runs in its own process
(the studio's own Python), at below-normal priority and on at most a few cores, so the lights and the
playback always come first; the models it asks (genre, sections, stems) run on the GPU as ever.

One JSON line in ({"path": the song's file, "cache": a folder for the analysis cache}), one JSON line
out ({"analysis": ...} or {"error": ...}); the first line out is {"ready": true}.
"""

from __future__ import annotations

import json
import sys


def main() -> None:
    out = sys.stdout
    sys.stdout = sys.stderr                              # the analysis prints as it goes: never into the replies

    def reply(obj: dict) -> None:
        out.write(json.dumps(obj) + "\n")
        out.flush()

    from .fetch_ahead import analyze_here
    reply({"ready": True})
    for line in sys.stdin.buffer:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
            reply({"analysis": analyze_here(req["path"], req["cache"])})
        except Exception as e:  # noqa: BLE001 - one song that fails must not end the worker
            reply({"error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()
