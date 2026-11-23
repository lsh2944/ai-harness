@echo off
rem ai-harness initial setup for Windows. Run it yourself in a terminal (cmd or PowerShell):
rem     .\setup.cmd [--no-apply]
rem The steps are the same as setup.sh and run in tools\harness_setup.py (setup-windows).
rem This file stays ASCII only (cmd reads it in the console code page) and uses .cmd instead of .ps1
rem so that the PowerShell execution policy does not block it.
setlocal
set "HERE=%~dp0"
set "PYTHONUTF8=1"
call :find_python
if errorlevel 1 (
    echo Python 3.11 or later was not found. Install it first, e.g.: winget install Python.Python.3.12 1>&2
    exit /b 1
)
%PY% "%HERE%tools\harness_setup.py" setup-windows %*
exit /b %ERRORLEVEL%

:find_python
rem py launcher first, then python on PATH. The Microsoft Store alias fails the probe and is skipped.
for %%C in ("py -3" "python") do (
    %%~C -c "import sys, tomllib" >nul 2>&1 && (
        set "PY=%%~C"
        exit /b 0
    )
)
exit /b 1
