"""Retrain the section labeler on every reviewed song, in a process of its own: the studio starts it as songs
are reviewed (labeler.maybe_retrain). By hand, with eval and export too: tools/section_labeler.py."""

import os

from ..appdirs import data_dir
from . import labeler
from .jam import JamLibrary

if __name__ == "__main__":
    labeler.train(JamLibrary(os.path.join(data_dir(), "shows")))
