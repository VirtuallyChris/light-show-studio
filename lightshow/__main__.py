import multiprocessing
import os
import runpy
import sys


def prepare_packaged() -> None:
    """For this process and everything it starts. HTTPS: a packaged Python has no certificates of its own (on a Mac, Spotify's
    sign-in and every download would fail). The data folder: the AI models run in their own Python, which isn't packaged and
    would otherwise take the app's read-only folder for it."""
    import certifi

    from .appdirs import data_dir
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("LSS_DATA_DIR", data_dir())


def dispatch(argv: list[str]) -> int | None:
    """The packaged app's way in to its other modules (see appdirs.self_command):
    `--run-module <module> [args]` runs that module as __main__ with those args. Anything else is the CLI,
    and a packaged app started with no arguments (a double-click) opens the studio."""
    if getattr(sys, "frozen", False):
        prepare_packaged()
    if argv[:1] == ["--run-module"] and len(argv) > 1:
        sys.argv = [argv[1], *argv[2:]]
        runpy.run_module(argv[1], run_name="__main__", alter_sys=True)
        return 0
    from .cli import main
    if not argv and getattr(sys, "frozen", False):
        argv = ["studio"]
    return main(argv)


if __name__ == "__main__":
    multiprocessing.freeze_support()      # the audio capture's helper process, in a packaged app
    sys.exit(dispatch(sys.argv[1:]))
