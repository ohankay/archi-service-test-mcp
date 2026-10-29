@echo off
rem ---------------------------------------------------------------------------
rem  archi-service-test-mcp - offline installation (Windows x64 or x86/32-bit, Python 3.10 - 3.14)
rem  Usage:     install.cmd            -> installs under %USERPROFILE%\.service-test-mcp
rem             set STM_INSTALL_HOME=D:\x  -> to install into another folder
rem             set STM_PYTHON=C:\Python312\python.exe -> to use a specific Python
rem  No internet needed: every package is installed from the wheelhouse folder.
rem ---------------------------------------------------------------------------
setlocal EnableExtensions
set "HERE=%~dp0"
if "%STM_INSTALL_HOME%"=="" set "STM_INSTALL_HOME=%USERPROFILE%\.service-test-mcp"

rem Architecture of the package (bundle_arch.txt is written by the build): x64 or x86
set "ARCH=x64"
if exist "%HERE%bundle_arch.txt" set /p ARCH=<"%HERE%bundle_arch.txt"
set "PKG=archi-service-test-mcp"
if /i "%ARCH%"=="x86" (
    set "BITS=32"
    set "OTHER=x64"
) else (
    set "BITS=64"
    set "OTHER=x86"
)
set "PYCHECK=import struct,sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,14) and struct.calcsize('P')*8 == %BITS% else 1)"
set "PYANY=import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,14) else 1)"

rem 1) Find a Python of the package's architecture (%BITS%-bit)
set "PYEXE="
if defined STM_PYTHON (
    "%STM_PYTHON%" -c "%PYCHECK%" >nul 2>&1 && set "PYEXE="%STM_PYTHON%""
    if not defined PYEXE goto :wrong_python
)
for %%V in (3.14 3.13 3.12 3.11 3.10) do (
    if not defined PYEXE (
        py -%%V-%BITS% -c "%PYCHECK%" >nul 2>&1 && set "PYEXE=py -%%V-%BITS%"
    )
)
if not defined PYEXE (
    python -c "%PYCHECK%" >nul 2>&1 && set "PYEXE=python"
)
if not defined PYEXE goto :wrong_python
echo [1/4] Python: %PYEXE% (%BITS%-bit, package: %ARCH%)

rem 2) Virtual environment (warn when an existing one has another architecture)
if exist "%STM_INSTALL_HOME%\Scripts\python.exe" (
    "%STM_INSTALL_HOME%\Scripts\python.exe" -c "%PYCHECK%" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] The existing installation in %STM_INSTALL_HOME% has another architecture or an unsupported Python.
        echo         Delete the folder and run again, or choose another folder with STM_INSTALL_HOME.
        exit /b 15
    )
    echo [2/4] Updating the existing installation: %STM_INSTALL_HOME%
) else (
    echo [2/4] Creating the virtual environment: %STM_INSTALL_HOME%
    %PYEXE% -m venv "%STM_INSTALL_HOME%"
    if errorlevel 1 ( echo [ERROR] The venv could not be created. & exit /b 11 )
)

rem 3) Offline package installation
echo [3/4] Installing packages (offline)...
"%STM_INSTALL_HOME%\Scripts\python.exe" -m pip install --no-index --find-links "%HERE%wheelhouse" --upgrade --disable-pip-version-check --quiet "%PKG%"
if errorlevel 1 ( echo [ERROR] Package installation failed. & exit /b 12 )

rem 4) Verification: are the dependencies consistent, do the Windows modules (pywin32) load?
echo [4/4] Verifying...
"%STM_INSTALL_HOME%\Scripts\python.exe" -m pip check --disable-pip-version-check
if errorlevel 1 ( echo [ERROR] Missing or incompatible packages ^(list above^). Delete the folder and install again: %STM_INSTALL_HOME% & exit /b 16 )
"%STM_INSTALL_HOME%\Scripts\python.exe" -c "import pywintypes, lxml.etree, zeep, yaml, jsonpath_ng, requests_ntlm, requests_negotiate_sspi, mcp.server.fastmcp, service_test_mcp.server, service_test_mcp.agent.cli"
if errorlevel 1 ( echo [ERROR] Python modules could not be loaded ^(pywin32 / lxml / zeep / mcp^). Delete the folder and install again: %STM_INSTALL_HOME% & exit /b 16 )
"%STM_INSTALL_HOME%\Scripts\service-test-mcp.exe" --version
if errorlevel 1 ( echo [ERROR] service-test-mcp does not run. & exit /b 13 )
rem Definitions of an earlier version (tanimlar\, gizli.yaml) are converted first, so the user's edits are kept.
"%STM_INSTALL_HOME%\Scripts\service-test-mcp.exe" --migrate >nul
rem Ready definitions (system, operations, approaches, tests). Existing files are not touched.
"%STM_INSTALL_HOME%\Scripts\service-test-mcp.exe" --init >nul
if exist "%HERE%company-definitions" "%STM_INSTALL_HOME%\Scripts\service-test-mcp.exe" --init --source "%HERE%company-definitions" --force >nul

echo.
echo INSTALLATION DONE
echo   MCP server       : %STM_INSTALL_HOME%\Scripts\service-test-mcp.exe
echo   Standalone agent : %STM_INSTALL_HOME%\Scripts\service-test-agent.exe
echo   Definitions      : %USERPROFILE%\.service-test-mcp\definitions
exit /b 0

:wrong_python
rem No Python of the right architecture. Is there one of another architecture?
set "FOUND_OTHER="
if defined STM_PYTHON "%STM_PYTHON%" -c "%PYANY%" >nul 2>&1 && set "FOUND_OTHER=1"
py -c "%PYANY%" >nul 2>&1 && set "FOUND_OTHER=1"
python -c "%PYANY%" >nul 2>&1 && set "FOUND_OTHER=1"
if defined FOUND_OTHER (
    echo [ERROR] This package is for %ARCH%, but this computer only has a Python of another architecture.
    echo         Install with the archi-service-test-mcp-windows-%OTHER%.zip package instead,
    echo         or install a %BITS%-bit Python 3.10 - 3.14.
    exit /b 14
)
echo [ERROR] Python 3.10 - 3.14 was not found.
echo         Install Python 3.12 from the company software center or python.org, then run this again.
exit /b 10
