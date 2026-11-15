@echo off
rem MacroPad Mixer - Windows installer. Double-click this file.
rem Runs install.ps1 from mixer\ (CIRCUITPY drive) or from the same folder (repo).
setlocal
set "PS1=%~dp0mixer\install.ps1"
if not exist "%PS1%" set "PS1=%~dp0install.ps1"
if not exist "%PS1%" goto missing

"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
if errorlevel 1 goto failed
exit /b 0

:missing
echo Could not find install.ps1.
echo Expected it at "%~dp0mixer\install.ps1".
pause
exit /b 1

:failed
echo.
echo Installation failed. Read the messages above, then press any key to close this window.
pause
exit /b 1
