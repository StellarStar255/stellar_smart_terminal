@echo off
REM Stellar Smart Terminal — Windows 打包脚本
REM 用法: build.bat
REM 产物: dist\StellarSmartTerminal\StellarSmartTerminal.exe
setlocal

cd /d "%~dp0"

set VENV=.venv-build

REM 1. 创建/复用独立构建虚拟环境
if not exist "%VENV%\Scripts\python.exe" (
    echo ==^> Creating build venv: %VENV%
    python -m venv "%VENV%" || goto :error
)

echo ==^> Installing dependencies + PyInstaller
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip --quiet || goto :error
"%VENV%\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller --quiet || goto :error

REM 2. PyInstaller 构建（onedir，图标 assets\smart_termina128.ico，见 smart_terminal.spec）
echo ==^> Running PyInstaller
"%VENV%\Scripts\pyinstaller.exe" --noconfirm smart_terminal.spec || goto :error

echo.
echo ==^> Build finished.
echo     Onedir : dist\StellarSmartTerminal\
echo     Run    : dist\StellarSmartTerminal\StellarSmartTerminal.exe
goto :eof

:error
echo Build failed with error %errorlevel%.
exit /b %errorlevel%
