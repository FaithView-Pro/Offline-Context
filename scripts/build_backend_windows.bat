@echo off
REM FaithView Pro — Build Python sidecar for Windows
REM Requires: Python 3.11+, PyInstaller

echo [1/3] Installing PyInstaller...
pip install pyinstaller -q

echo [2/3] Building sidecar executable...
cd /d "%~dp0"
pyinstaller faithview_sidecar.spec --clean --noconfirm

echo [3/3] Build complete!
echo Output: dist\sidecar\sidecar.exe
if exist "dist\sidecar\sidecar.exe" (
    echo SUCCESS
) else (
    echo FAILED — check output above
)
