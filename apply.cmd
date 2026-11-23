@echo off
rem Apply ai-harness to the installed copies (~\.claude, ~\.codex, ~\.agents) on Windows.
rem Run it yourself in a terminal after closing Claude Code and Codex:
rem     .\apply.cmd
rem The steps are the same as apply.sh and run in tools\harness_setup.py (apply-windows).
rem install.py --apply still asks you to type "apply" before writing anything.
setlocal
set "HERE=%~dp0"
set "PYTHONUTF8=1"
if not "%~1"=="" (
    echo usage: apply.cmd 1>&2
    exit /b 2
)
call :find_python
if errorlevel 1 (
    echo Python 3.11 or later was not found. Install it first, e.g.: winget install Python.Python.3.12 1>&2
    exit /b 1
)
%PY% "%HERE%tools\harness_setup.py" apply-windows
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
