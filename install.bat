@echo off
setlocal
REM Kurt installer for Windows

set "KHOME=%USERPROFILE%\.kurt"
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  echo [X] Python not found. Install Python 3.9+ from https://www.python.org/downloads/
  echo     and tick "Add python.exe to PATH" in the installer. Then run this again.
  pause
  exit /b 1
)

pushd "%~dp0"

echo =^> Creating private environment in %KHOME%\venv
%PY% -m venv "%KHOME%\venv"
if errorlevel 1 ( echo [X] Could not create virtual environment. & popd & pause & exit /b 1 )

"%KHOME%\venv\Scripts\python.exe" -m pip install --upgrade pip -q
echo =^> Installing Kurt
"%KHOME%\venv\Scripts\python.exe" -m pip install . -q
if errorlevel 1 ( echo [X] pip install failed. & popd & pause & exit /b 1 )

popd

if not exist "%KHOME%\bin" mkdir "%KHOME%\bin"
> "%KHOME%\bin\kurt.cmd" echo @"%KHOME%\venv\Scripts\kurt.exe" %%*

powershell -NoProfile -Command "$b=Join-Path $env:USERPROFILE '.kurt\bin'; $p=[Environment]::GetEnvironmentVariable('Path','User'); if(-not $p){$p=''}; if(($p -split ';') -notcontains $b){[Environment]::SetEnvironmentVariable('Path',($p.TrimEnd(';')+';'+$b).TrimStart(';'),'User')}"

echo.
echo [OK] Kurt installed.
echo      Open a NEW terminal window (PowerShell / cmd), cd into any project, and run:  kurt
pause