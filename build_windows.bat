@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "LOG=build\windows-build.log"
if not exist "build" mkdir "build"
>"%LOG%" echo Windows build log

if not exist "homo_editor.py" (
    echo ERROR: homo_editor.py not found.
    goto fail
)
if not exist "homo_runtime.hpp" (
    echo ERROR: homo_runtime.hpp not found.
    goto fail
)

set "PYTHON_EXE="
set "PYTHON_ARGS="
if defined PYTHON (
    set "PYTHON_EXE=%PYTHON%"
) else (
    where py >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_EXE=py"
        set "PYTHON_ARGS=-3"
    ) else (
        where python >nul 2>nul
        if errorlevel 1 goto nopython
        set "PYTHON_EXE=python"
    )
)

"%PYTHON_EXE%" %PYTHON_ARGS% --version >>"%LOG%" 2>&1
if errorlevel 1 goto nopython

if not exist ".build-venv\Scripts\python.exe" (
    echo Creating build environment: .build-venv
    "%PYTHON_EXE%" %PYTHON_ARGS% -m venv .build-venv >>"%LOG%" 2>&1
    if errorlevel 1 goto fail
)

call ".build-venv\Scripts\activate.bat"
if errorlevel 1 goto fail

python -c "import tkinter" >>"%LOG%" 2>&1
if errorlevel 1 (
    echo ERROR: This Python does not include Tkinter.
    echo Install python.org Python 3.9+ and try again.
    goto fail
)

echo Installing PyInstaller...
python -m pip install --upgrade pip pyinstaller >>"%LOG%" 2>&1
if errorlevel 1 goto fail

echo Building Windows executable...
python -m PyInstaller ^
    --noconfirm --clean --onefile --windowed ^
    --name "HomoEditor" ^
    --icon "assets\icon.ico" ^
    --add-data "homo_runtime.hpp;." ^
    --add-data "assets\icon-256.png;assets" ^
    --add-data "assets\icon-64.png;assets" ^
    --add-data "assets\icon-32.png;assets" ^
    --distpath dist --workpath build\pyinstaller ^
    homo_editor.py >>"%LOG%" 2>&1
if errorlevel 1 goto fail

if not exist "dist\HomoEditor.exe" goto fail

echo.
echo Build complete: dist\HomoEditor.exe
echo Note: the target machine still needs a C++ compiler (MinGW-w64 g++ or MSVC).
endlocal
exit /b 0

:nopython
echo ERROR: Python 3.9+ was not found.
echo Install Python from python.org, or set PYTHON to the full path of python.exe.
goto fail

:fail
echo.
echo Build failed.
echo Full log:
echo   %LOG%
echo.
if exist "%LOG%" (
    echo Last log lines:
    powershell -NoProfile -Command "Get-Content -Tail 20 -Path '%LOG%'"
)
echo.
echo If PyInstaller failed, open the log and check the last error lines.
pause
endlocal
exit /b 1
