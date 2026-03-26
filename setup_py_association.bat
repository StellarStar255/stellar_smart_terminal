@echo off
:: Setup Python file association for double-click execution
:: Run this script as Administrator

echo Setting up .py file association with Anaconda Python...
echo.

:: Check for admin rights
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: This script requires Administrator privileges.
    echo Please right-click and select "Run as administrator"
    pause
    exit /b 1
)

:: Set file association - use pythonw for GUI apps (no console window)
:: Using Anaconda Python installation
set PYTHON_PATH=D:\Anaconda3\pythonw.exe

:: Check if Python exists
if not exist "%PYTHON_PATH%" (
    echo ERROR: Python not found at %PYTHON_PATH%
    echo Please edit this script and update PYTHON_PATH
    pause
    exit /b 1
)

echo Using Python: %PYTHON_PATH%
echo.

:: Set up the file association
echo Setting .py file association...

:: Create the file type
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
echo SUCCESS! Python file association has been set up.
echo.
echo You can now double-click app.py to run the Smart Terminal.
echo.
echo Note: For GUI applications like this one, pythonw.exe is used
echo so no console window will appear.
echo.
pause
