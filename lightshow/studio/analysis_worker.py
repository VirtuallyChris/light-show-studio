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
    # The replies go out on a copy of the process's stdout; the process's own stdout becomes its log (stderr),
    # so nothing printed from native code can land in the replies, or fill the pipe and freeze the analysis.
    import os
    out = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    try:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    except OSError:
        pass
    sys.stdout = sys.stderr                              # the analysis prints as it goes: never into the replies

    def reply(obj: dict) -> None:
        out.write(json.dumps(obj) + "\n")
        out.flush()

    from .fetch_ahead import analyze_here
    from . import priority
    reply({"ready": True})
    # load the models now, while the jam's first song plays: loaded at the first song to learn they
    # took minutes, and that song's show came at its end (a request waits for its model's load)
    import threading
    from . import beats_model, drum_model, genre, stems, structure_model
    for w in (structure_model.WORKER, stems.WORKER, genre.WORKER, beats_model.WORKER, drum_model.WORKER):
        threading.Thread(target=w.warm, daemon=True).start()
    for line in sys.stdin.buffer:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
            import time as _t
            print(f"{_t.strftime('%H:%M:%S')} [worker] got {os.path.basename(req.get('path', ''))}{' (playing now: urgent)' if req.get('urgent') else ''}",
                  file=sys.stderr, flush=True)
            # the song playing now, not yet learned: above normal priority until it is done (the lights only
            # glow meanwhile); every other song below normal, so the lights and the music come first
            priority.process(priority.ABOVE_NORMAL if req.get("urgent") else priority.BELOW_NORMAL)
            reply({"analysis": analyze_here(req["path"], req["cache"])})
            priority.process(priority.BELOW_NORMAL)
        except Exception as e:  # noqa: BLE001 - one song that fails must not end the worker
            reply({"error": f"{type(e).__name__}: {e}"})
    # The studio has gone (it closed our stdin). End at once: a warming thread may be printing, and an ordinary
    # shutdown while a daemon thread holds stderr's lock aborts the process ("Fatal Python error").
    out.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
