@echo off
rem Build the standalone app into dist\Vinyl Recorder and zip it as dist\Vinyl-Recorder-windows.zip
cd /d "%~dp0.."
tasklist /fi "imagename eq Vinyl Recorder.exe" | find /i "Vinyl Recorder.exe" >nul && (
    echo Close Vinyl Recorder.exe first - a running copy locks the dist folder.
    goto :error
)
.venv\Scripts\python.exe -m pip install -q pyinstaller || goto :error
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\vinyl_recorder.spec || goto :error
copy /y "packaging\Read me first.txt" "dist\Vinyl Recorder\Read me first.txt" >nul
powershell -NoProfile -Command "Compress-Archive -Force -Path 'dist\Vinyl Recorder' -DestinationPath 'dist\Vinyl-Recorder-windows.zip'" || goto :error
echo.
echo Done: dist\Vinyl-Recorder-windows.zip
exit /b 0

:error
echo Build failed.
exit /b 1
