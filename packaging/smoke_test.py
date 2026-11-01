"""Start a built app headless and check it serves its pages: python packaging/smoke_test.py <path to the executable>

The studio with no lights, no Spotify player and no browser window, then Bulb Show (started the way the studio
starts it, through the app's own executable) with virtual bulbs and speakers, both in a scratch data folder,
so a build machine's run touches nothing. Fails if either does not answer in time or a page does not load."""

import os
import subprocess
import sys
import tempfile
import time
import urllib.request

PORT = int(os.environ.get("LSS_SMOKE_PORT", "8799"))
LIMIT_S = 120


def serve(name: str, cmd: list[str], port: int, pages: list[str], env: dict) -> bool:
    log = os.path.join(env["LSS_DATA_DIR"], f"{name}-smoke.log")
    with open(log, "w") as out:
        proc = subprocess.Popen(cmd, env=env, stdout=out, stderr=subprocess.STDOUT)
    try:
        end, up = time.time() + LIMIT_S, False
        while time.time() < end and proc.poll() is None:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{pages[0]}", timeout=3):
                    up = True
                    break
            except OSError:
                time.sleep(2)
        if not up:
            print(f"{name}: did not answer (exit code {proc.poll()})")
            return False
        for page in pages:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{page}", timeout=15) as r:
                body = r.read()
                print(f"{name} {page}: {r.status}, {len(body)} bytes")
                if r.status != 200 or not body:
                    return False
        return True
    finally:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
        with open(log, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        if text.strip():
            print(f"--- {name} output\n{text[-6000:]}")


def main(exe: str) -> int:
    env = {**os.environ, "LSS_DATA_DIR": tempfile.mkdtemp(prefix="lss-smoke-")}
    ok = serve("studio", [exe, "studio", "--no-lights", "--no-player", "--no-browser", "--port", str(PORT)],
               PORT, ["/", "/party", "/api/addons", "/api/setup"], env)
    ok = serve("bulbshow", [exe, "--run-module", "bulbshow", "--no-browser", "--no-lights", "--no-cast", "--port", str(PORT + 1)],
               PORT + 1, ["/api/settings", "/"], env) and ok
    for name in ("studio.log", os.path.join("bulbshow", "bulbshow.log")):
        path = os.path.join(env["LSS_DATA_DIR"], name)
        if os.path.exists(path):
            print(f"--- {name}\n{open(path, encoding='utf-8', errors='replace').read()[-6000:]}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
