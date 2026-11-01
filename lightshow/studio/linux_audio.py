"""Linux: the output nobody hears, made by the studio itself. No driver, no administrator, no settings to change.

PulseAudio and PipeWire (through its PulseAudio side) both let any program add a virtual output, a "null sink", and
every output can be recorded through its monitor. The hidden player is started with PULSE_SINK pointing at it, so its
sound goes there and nothing else moves. The output goes away again when the studio stops.
"""

from __future__ import annotations

import re
import shutil
import subprocess

SINK = "lightshow_hidden"
DESCRIPTION = "Light Show Studio hidden"
_module: list[str] = []                        # the module this studio loaded, to unload


def _pactl(*args: str) -> str:
    return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=5, check=True).stdout.strip()


def available() -> bool:
    return shutil.which("pactl") is not None


def ensure_sink() -> str | None:
    """Makes the hidden output if it isn't there, leaving the default output where it was. Its name, or None."""
    if not available():
        return None
    try:
        if any(line.split("\t")[1:2] == [SINK] for line in _pactl("list", "short", "sinks").splitlines()):
            return DESCRIPTION
        default = _pactl("get-default-sink")
        safe = DESCRIPTION.replace(" ", "\\ ")
        _module[:] = [_pactl("load-module", "module-null-sink", f"sink_name={SINK}", f'sink_properties=device.description="{safe}"')]
        if re.fullmatch(r"\S+", default) and _pactl("get-default-sink") != default:
            _pactl("set-default-sink", default)
        return DESCRIPTION
    except (OSError, subprocess.SubprocessError):
        return None


def remove_sink() -> None:
    if _module:
        try:
            _pactl("unload-module", _module.pop())
        except (OSError, subprocess.SubprocessError):
            pass
