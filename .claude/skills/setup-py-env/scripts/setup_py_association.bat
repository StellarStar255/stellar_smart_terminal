@echo off
:: Setup Python file association for double-click execution
:: Run this script as Administrator
:: Usage: setup_py_association.bat [python_path]

setlocal enabledelayedexpansion

echo ============================================
echo  Python File Association Setup for Windows
echo ============================================
echo.

:: Check for admin rights
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: This script requires Administrator privileges.
    echo Please right-click and select "Run as administrator"
    pause
    exit /b 1
)

:: Determine Python path
if "%~1"=="" (
    echo Detecting Python installation...

    :: Check common Anaconda locations first
    if exist "D:\Anaconda3\pythonw.exe" (
        set "PYTHON_PATH=D:\Anaconda3\pythonw.exe"
        goto :found
    )
    if exist "C:\Anaconda3\pythonw.exe" (
        set "PYTHON_PATH=C:\Anaconda3\pythonw.exe"
        goto :found
    )
    if exist "%USERPROFILE%\Anaconda3\pythonw.exe" (
        set "PYTHON_PATH=%USERPROFILE%\Anaconda3\pythonw.exe"
        goto :found
    )
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" (
        set "PYTHON_PATH=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
        goto :found
    )
    if exist "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe" (
        set "PYTHON_PATH=%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"
        goto :found
    )
    if exist "%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe" (
        set "PYTHON_PATH=%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe"
        goto :found
    )

    :: Try to find pythonw in PATH
    for /f "delims=" %%A in ('where pythonw 2^>nul') do (
        set "PYTHON_PATH=%%A"
        goto :found
    )

    echo ERROR: Python not found automatically.
    echo Please run this script with the Python path as argument:
    echo   setup_py_association.bat "C:\path\to\pythonw.exe"
    pause
    exit /b 1
) else (
    set "PYTHON_PATH=%~1"
)

:found
:: Verify Python exists
if not exist "%PYTHON_PATH%" (
    echo ERROR: Python not found at: %PYTHON_PATH%
    pause
    exit /b 1
)

echo Using Python: %PYTHON_PATH%
echo.

:: Set up the file association
echo Setting .py file association...

:: Create the file type in HKCR (system-wide)
reg add "HKCR\.py" /ve /d "Python.File" /f >nul
reg add "HKCR\.py" /v "Content Type" /d "text/plain" /f >nul

:: Create the Python.File type with open command
reg add "HKCR\Python.File" /ve /d "Python File" /f >nul
reg add "HKCR\Python.File\DefaultIcon" /ve /d "%PYTHON_PATH%,0" /f >nul
reg add "HKCR\Python.File\shell" /ve /d "open" /f >nul
reg add "HKCR\Python.File\shell\open" /ve /d "Open" /f >nul
reg add "HKCR\Python.File\shell\open\command" /ve /d "\"%PYTHON_PATH%\" \"%%1\" %%*" /f >nul

:: Also set up for current user (takes precedence)
reg add "HKCU\Software\Classes\.py" /ve /d "Python.File" /f >nul
reg add "HKCU\Software\Classes\Python.File\shell\open\command" /ve /d "\"%PYTHON_PATH%\" \"%%1\" %%*" /f >nul

echo.
echo ============================================
echo  SUCCESS! Python file association configured.
echo ============================================
echo.
echo Python path: %PYTHON_PATH%
echo.
echo You can now double-click .py files to run them.
echo Using pythonw.exe = no console window (ideal for GUI apps)
echo.

endlocal
pause
