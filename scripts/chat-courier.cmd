@echo off
setlocal EnableExtensions

set "COURIER_ROOT=%~dp0.."
for %%I in ("%COURIER_ROOT%") do set "COURIER_ROOT=%%~fI"
if not exist "%COURIER_ROOT%\chat_courier\cli.py" (
    echo {"event":"configuration_error","ok":false,"phase":"startup","detail":"Courier source is incomplete"}
    exit /b 2
)

rem CHAT_COURIER_PYTHON is a human/operator setup value. Calling Agents must
rem use this launcher without changing it.
if exist "%COURIER_ROOT%\.venv\Scripts\python.exe" (
    set "COURIER_PYTHON=%COURIER_ROOT%\.venv\Scripts\python.exe"
) else if defined CHAT_COURIER_PYTHON (
    set "COURIER_PYTHON=%CHAT_COURIER_PYTHON%"
) else (
    set "COURIER_PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if not exist "%COURIER_PYTHON%" (
    echo {"event":"configuration_error","ok":false,"phase":"startup","detail":"Approved Courier Python was not found; create .venv or configure CHAT_COURIER_PYTHON"}
    exit /b 2
)

rem Replace, rather than extend, a caller-provided path. The Python entrypoint
rem verifies that the imported module actually comes from this source tree.
set "PYTHONPATH=%COURIER_ROOT%"
set "CHAT_COURIER_EXPECTED_SOURCE_ROOT=%COURIER_ROOT%"

"%COURIER_PYTHON%" -m chat_courier.cli %*
exit /b %ERRORLEVEL%
