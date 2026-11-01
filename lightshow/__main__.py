import multiprocessing
import runpy
import sys


def dispatch(argv: list[str]) -> int | None:
    """The packaged app's way in to its other modules (see appdirs.self_command):
    `--run-module <module> [args]` runs that module as __main__ with those args. Anything else is the CLI,
    and a packaged app started with no arguments (a double-click) opens the studio."""
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
