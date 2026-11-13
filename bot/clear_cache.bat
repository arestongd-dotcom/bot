@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

echo [1/3] Clearing photo cache...
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m scripts.clear_cache
) else (
    python -m scripts.clear_cache
)
if errorlevel 1 echo [!] Cache clear failed.

echo [2/3] Removing __pycache__...
for /d /r %%d in (__pycache__) do @if exist "%%d" rd /s /q "%%d"

echo [3/3] Done.
pause
