---
name: setup-py-env
description: Set up Python file association on Windows so .py files can be double-clicked to run. Use when setting up a new Windows machine for this project.
allowed-tools: Bash(powershell:*), Bash(cmd:*), Bash(where:*)
disable-model-invocation: true
---

# Python Environment Setup for Windows

This skill configures Windows to associate `.py` files with Python, enabling double-click execution of the app.

## What it does

1. Detects the Python installation (prefers Anaconda, falls back to system Python)
2. Configures Windows registry to associate `.py` files with `pythonw.exe` (no console window)
3. Verifies the setup was successful

## Usage

Run the skill:

```
/setup-py-env
```

Or specify a custom Python path:

```
/setup-py-env D:/Anaconda3/pythonw.exe
```

## Steps to Execute

1. **Run the setup script with admin privileges:**

   ```bash
   powershell -Command "Start-Process '${CLAUDE_SKILL_DIR}/scripts/setup_py_association.bat' -Verb RunAs -Wait"
   ```

2. **Verify the association:**

   ```bash
   powershell -Command "Get-ItemProperty -Path 'HKCU:\Software\Classes\Python.File\shell\open\command' -ErrorAction SilentlyContinue | Select-Object '(default)'"
   ```

3. **Test by informing the user they can now double-click `app.py` to launch the application.**

## Requirements

- Windows OS
- Administrator privileges (UAC prompt will appear)
- Python installed (Anaconda or system Python)
