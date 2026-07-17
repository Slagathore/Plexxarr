@echo off
echo Removing Sensarr from scheduled tasks...
schtasks /delete /tn "Sensarr" /f
schtasks /delete /tn "Plexxarr" /f 2>nul
schtasks /delete /tn "PlexResetButton" /f 2>nul
if %errorlevel% neq 0 (
    echo Task not found or could not be removed.
) else (
    echo Done. Sensarr will no longer start on login.
)
pause
