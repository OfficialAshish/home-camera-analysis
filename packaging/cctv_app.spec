# PyInstaller spec for the desktop app:  pyinstaller packaging/cctv_app.spec
# Put ffmpeg.exe / ffprobe.exe in packaging/ffmpeg/ first (build_windows.ps1 downloads them).
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
datas = [(os.path.join(ROOT, "cctv_analysis", "app", "static"), "cctv_analysis/app/static")]
pub = os.path.join(ROOT, "cctv_analysis", "license_public_key.pem")
if os.path.exists(pub):
    datas.append((pub, "cctv_analysis"))
ff = os.path.join(SPECPATH, "ffmpeg")
if os.path.isdir(ff):
    datas.append((ff, "ffmpeg"))
for pkg in ("rapidocr_onnxruntime", "ultralytics", "open_image_models", "fast_plate_ocr"):
    datas += collect_data_files(pkg)
# torchvision >= 0.2x ships its C++ operators (NMS, used by YOLO) as _C_stable.so/.pyd; PyInstaller's
# collectors skip such files -> "operator torchvision::nms does not exist". Add them explicitly.
import glob
import importlib.util
binaries = []
_tv = os.path.dirname(importlib.util.find_spec("torchvision").origin)
for pat in ("*.so", "*.pyd", "*.dll", "*.dylib"):
    binaries += [(f, "torchvision") for f in glob.glob(os.path.join(_tv, pat))]
if os.path.isdir(_tv + ".libs"):
    binaries += [(f, "torchvision.libs") for f in glob.glob(os.path.join(_tv + ".libs", "*"))]
hidden = collect_submodules("cctv_analysis") + ["waitress", "flask", "psutil", "cryptography"]

a = Analysis([os.path.join(SPECPATH, "launcher.py")], pathex=[ROOT], datas=datas, binaries=binaries,
             hiddenimports=hidden,
             excludes=["matplotlib.tests", "numpy.tests", "IPython", "jupyter_client", "notebook"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="CCTVAnalysis", console=True,
          icon=None, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="CCTVAnalysis", upx=False)
