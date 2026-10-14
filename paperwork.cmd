@echo off
setlocal
if "%~1"=="" goto start_app
if /i "%~1"=="start" goto start_app
if /i "%~1"=="stop" goto stop_app
echo Usage: paperwork start or paperwork stop
exit /b 0

:start_app
pushd "%~dp0"
call "Launch Paperwork.cmd"
set "paperworkExitCode=%errorlevel%"
popd
exit /b %paperworkExitCode%

:stop_app
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "$ErrorActionPreference='Stop'; foreach($port in 8765..8784) { $address='http://127.0.0.1:'+ $port; try { $health=Invoke-RestMethod -Uri ($address+'/api/health') -TimeoutSec 1 } catch { continue }; if($health.app -eq 'paperwork-local') { try { $null=Invoke-RestMethod -Method Post -Uri ($address+'/api/shutdown') -TimeoutSec 3; Write-Host 'Paperwork stopped.'; exit 0 } catch { Write-Host 'Paperwork could not be stopped. Please try again.'; exit 1 } } }; Write-Host 'Paperwork is already stopped.'"
exit /b %errorlevel%
