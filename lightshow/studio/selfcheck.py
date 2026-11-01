"""What a packaged app checks about itself (packaging/smoke_test.py): the section labeler it ships really loads, and it
can reach GitHub over HTTPS for its updates. Neither gives an error when it fails: a labeler that cannot load falls back
to the rules, an update that cannot be checked just opens the old version, so a broken build would look fine."""

import sys
import time
import urllib.request

from . import labeler

if __name__ == "__main__":
    if not labeler._bundled():
        sys.exit("the bundled section labeler did not load")
    print("the bundled section labeler loads")
    for attempt in range(3):
        try:
            # (plainly, as Spotify's sign-in and every download do: it relies on the certificates the app set up for itself)
            with urllib.request.urlopen("https://github.com/", timeout=20) as r:
                print(f"reaches GitHub: {r.status}")
            break
        except OSError as e:
            if attempt == 2:
                sys.exit(f"could not reach GitHub: {e}")
            time.sleep(5)
