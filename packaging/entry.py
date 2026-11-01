"""The packaged app's entry point (PyInstaller runs this file as a script, outside the package)."""

import multiprocessing
import sys

from lightshow.__main__ import dispatch

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(dispatch(sys.argv[1:]))
