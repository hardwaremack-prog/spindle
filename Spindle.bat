@echo off
title Spindle
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% -c "import mutagen" 2>nul || (
  echo First run - setting things up...
  call "%~dp0setup-windows.bat"
)
%PY% -m spindle.server %*
pause
