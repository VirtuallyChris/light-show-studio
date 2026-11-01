"""What a packaged app checks about itself (packaging/smoke_test.py): the section labeler it ships really loads.
A labeler that cannot load only falls back to the rules, without an error, so a broken build would look fine."""

import sys

from . import labeler

if __name__ == "__main__":
    if not labeler._bundled():
        sys.exit("the bundled section labeler did not load")
    print("the bundled section labeler loads")
