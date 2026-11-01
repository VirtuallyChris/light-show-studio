# PyInstaller spec for the downloads: pyinstaller packaging/lightshow.spec
# Built by .github/workflows/release.yml on Windows, macOS and Linux. One folder (not one file): the app starts
# fast and its helper processes (analysis, Bulb Show) start from the same executable.
import os
import platform
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
NAME = "LightShowStudio"
VERSION = os.environ.get("LSS_VERSION", "0.0.0")

# Things in the source folders that belong to one home, never to the download
SKIP_DIRS = {"__pycache__", "shows", "_stubs"}
SKIP_FILES = {"config.json"}


def tree(pkg):
    """Every file of a package as data, the .py sources included: the AI models run in their own Python
    (the in-app installer's), which imports lightshow from these files."""
    out = []
    for base, dirs, files in os.walk(os.path.join(ROOT, pkg)):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f in SKIP_FILES or f.endswith((".log", ".pyc", ".log.1")):
                continue
            out.append((os.path.join(base, f), os.path.relpath(base, ROOT)))
    return out


def modules(pkg):
    """Every module of one of our packages, read from its folder: the app starts some of them only by name
    (--run-module), so the import scan alone misses them (and collect_submodules cannot import bulbshow here)."""
    out = []
    for base, dirs, files in os.walk(os.path.join(ROOT, pkg)):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and os.path.exists(os.path.join(base, d, "__init__.py"))]
        rel = os.path.relpath(base, ROOT).replace(os.sep, ".")
        out += [rel if f == "__init__.py" else f"{rel}.{f[:-3]}" for f in files if f.endswith(".py")]
    return out


datas = tree("lightshow") + tree("bulbshow") + collect_data_files("soundcard") + collect_data_files("librosa") + collect_data_files("xgboost")
# The version the app knows itself by (lightshow/studio/updates.py), from the release tag
os.makedirs(workpath, exist_ok=True)
version_file = os.path.join(workpath, "VERSION")
with open(version_file, "w", encoding="utf-8") as fh:
    fh.write(VERSION)
datas += [(version_file, "lightshow")] + collect_data_files("certifi")
hidden = modules("lightshow") + modules("bulbshow") + collect_submodules("pychromecast")
# The AI models run in the model environment, not in the app: never pull them in
excludes = ["torch", "torchaudio", "torchvision", "transformers", "ctranslate2", "faster_whisper", "demucs",
            "tkinter", "matplotlib", "IPython", "pytest"]

if sys.platform == "win32":
    icon = os.path.join(ROOT, "Light Show Studio.ico")
else:
    icon = os.path.join(ROOT, "lightshow", "studio", "static", "icon-512.png")   # (Pillow turns it into .icns)

a = Analysis([os.path.join(SPECPATH, "entry.py")], pathex=[ROOT], datas=datas, binaries=collect_dynamic_libs("xgboost"), hiddenimports=hidden,
             excludes=excludes, noarchive=False)
INTEL_MAC = sys.platform == "darwin" and platform.machine() == "x86_64"
if INTEL_MAC:
    # XGBoost and scikit-learn both link libomp, and the app keeps one copy. On Intel Macs that copy is scikit-learn's,
    # too old for XGBoost (no ___kmpc_dispatch_deinit), and the section labeler will not load: ship Homebrew's instead.
    omp = os.path.realpath("/usr/local/opt/libomp/lib/libomp.dylib")
    a.binaries = [(dest, omp if kind != "SYMLINK" and os.path.basename(dest) == "libomp.dylib" else src, kind)
                  for dest, src, kind in a.binaries]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=NAME, icon=icon,
          console=sys.platform.startswith("linux"),     # Windows and macOS: no console window
          upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)

if sys.platform == "darwin":
    app = BUNDLE(coll, name="Light Show Studio.app", icon=icon, bundle_identifier="io.github.lightshowstudio",
                 version=VERSION,
                 info_plist={
                     "CFBundleName": "Light Show Studio",
                     "CFBundleDisplayName": "Light Show Studio",
                     "CFBundleShortVersionString": VERSION,
                     "LSMinimumSystemVersion": "14.0" if INTEL_MAC else "12.0",     # (Homebrew's libomp, above)
                     "NSHighResolutionCapable": True,
                     "NSMicrophoneUsageDescription": "Light Show Studio listens to measure your speakers' delay and to follow the music.",
                     "NSLocalNetworkUsageDescription": "Light Show Studio finds and drives the lights and speakers on your network.",
                     "NSBonjourServices": ["_googlecast._tcp", "_hue._tcp"],
                 })
