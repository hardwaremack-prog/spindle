@echo off
REM One-time setup for Spindle on Windows 10/11.
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% --version >nul 2>nul || (
  echo Installing Python...
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
  set PY=py -3
)
echo Installing the tagging library...
%PY% -m pip install --upgrade mutagen
where ffmpeg >nul 2>nul || if not exist tools\ffmpeg.exe (
  echo Installing ffmpeg...
  winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
)
where fpcalc >nul 2>nul || if not exist tools\fpcalc.exe (
  echo Getting fpcalc (lets Spindle recognise songs by sound)...
  powershell -NoProfile -Command "$u='https://github.com/acoustid/chromaprint/releases/download/v1.5.1/chromaprint-fpcalc-1.5.1-windows-x86_64.zip'; Invoke-WebRequest $u -OutFile $env:TEMP\fpcalc.zip; Expand-Archive $env:TEMP\fpcalc.zip $env:TEMP\fpcalc -Force; Copy-Item (Get-ChildItem $env:TEMP\fpcalc -Recurse -Filter fpcalc.exe)[0].FullName tools\"
)
echo.
echo Done. If ffmpeg was just installed, close this window and open Spindle again.
