@echo off
rem Double-click to play: runs play.py with the Python on PATH (install Python 3.10+ from python.org).
cd /d "%~dp0"
where py >nul 2>nul && (py -3 play.py %*) || (python play.py %*)
pause
