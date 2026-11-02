"""Does the party's capture really work on this Mac or Linux machine? python packaging/party_capture_check.py

The studio's own code from end to end, with no Spotify and no account: the hidden player's browser is started on a page
playing a 440 Hz tone, kept from the speakers as in a party, and recorded by the party's recorder process. Fails unless the
recording is that tone. Run by the release workflow. On a Mac, macOS must allow audio recording, and asks the first time.
"""

import http.server
import os
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PAGE = b"""<!doctype html><script>
const ctx = new AudioContext(); const o = ctx.createOscillator(); o.frequency.value = 440; o.connect(ctx.destination); o.start();
</script>"""
SR = 44100


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *a):
        pass


def main() -> int:
    from lightshow.studio import audio_io, linux_audio, mac_capture, player_browser
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    player = player_browser.HiddenPlayer(f"http://127.0.0.1:{server.server_address[1]}/", player_browser.HIDDEN_OUTPUT)
    player.start()
    try:
        end = time.monotonic() + 60
        while time.monotonic() < end and not player.routed:
            time.sleep(0.5)
        print(f"player: running {player.running}, routed {player.routed}, error {player.error!r}", flush=True)
        if not player.routed:
            return 1
        device = mac_capture.DEVICE if sys.platform == "darwin" else linux_audio.DESCRIPTION
        capacity = SR * 10
        ring = audio_io.Ring(capacity, 2)
        proc = audio_io._spawn(audio_io._recorder_main, (ring.name, capacity, 2, SR, device, 1024))
        error = audio_io._wait_ready(ring, proc)
        if error:
            print(f"recorder: {error}", flush=True)
            return 1
        heard, end = False, time.monotonic() + 60       # (a browser's first run can take its time to start the page)
        while not heard and time.monotonic() < end:
            time.sleep(4)
            written = int(ring.h[audio_io.W])
            if written < SR * 3:
                continue
            take = ring.get(written - SR * 3, SR * 3)[:, 0].astype(np.float64) / 32767
            rms = float(np.sqrt((take ** 2).mean()))
            peak = np.abs(np.fft.rfft(take)).argmax() / 3
            print(f"recorded: rms {rms:.3f}, peak {peak:.1f} Hz, recorder error {ring.error()!r}", flush=True)
            heard = rms > 0.2 and abs(peak - 440) < 2
        ring.h[audio_io.STOP] = 1
        proc.join(5)
        return 0 if heard else 1
    finally:
        player.stop()
        if sys.platform.startswith("linux"):
            print(f"the hidden output is gone again: {linux_audio.SINK not in linux_audio._pactl('list', 'short', 'sinks')}")


if __name__ == "__main__":
    code = main()
    print("party capture:", "ok" if code == 0 else "FAILED", flush=True)
    os._exit(code)
