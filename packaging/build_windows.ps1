# Build the Windows desktop app (run in PowerShell from the repository root).
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
# Result: dist\CCTVAnalysis\CCTVAnalysis.exe (zip the folder to distribute).
$ErrorActionPreference = "Stop"
python -m pip install --upgrade pip
python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
python -m pip install -r requirements.txt pyinstaller
if (-not (Test-Path packaging\ffmpeg\ffmpeg.exe)) {
  # LGPL build: no GPL x264 inside the commercial bundle; the app falls back to OpenH264/MPEG-4.
  $url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-lgpl.zip"
  Invoke-WebRequest $url -OutFile ffmpeg.zip
  Expand-Archive ffmpeg.zip -DestinationPath ffmpeg_tmp -Force
  New-Item -ItemType Directory -Force packaging\ffmpeg | Out-Null
  Get-ChildItem ffmpeg_tmp -Recurse -Include ffmpeg.exe,ffprobe.exe | Copy-Item -Destination packaging\ffmpeg
  Remove-Item ffmpeg.zip; Remove-Item ffmpeg_tmp -Recurse
}
pyinstaller --noconfirm packaging\cctv_app.spec
dist\CCTVAnalysis\CCTVAnalysis.exe --self-test
if ($LASTEXITCODE -ne 0) { throw "self-test of the packaged app failed" }
