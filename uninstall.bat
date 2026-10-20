@echo off
REM Removes Kurt (Windows)
rmdir /s /q "%USERPROFILE%\.kurt"
powershell -NoProfile -Command "$b=Join-Path $env:USERPROFILE '.kurt\bin'; $p=[Environment]::GetEnvironmentVariable('Path','User'); if($p){[Environment]::SetEnvironmentVariable('Path',(($p -split ';' | Where-Object { $_ -and $_ -ne $b }) -join ';'),'User')}"
echo [OK] Kurt removed.
pause