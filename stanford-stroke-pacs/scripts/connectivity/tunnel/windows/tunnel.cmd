@echo off
title SSC-PACS SSH Tunnel
set "IP=XXX.XXX.XXX.XXX"
set "USER=XXX"
echo Opening SSH tunnel to SSC-PACS (localhost:8043 -^> %USER%@%IP%)
echo Enter your password when prompted.
echo.
ssh -N -L 8043:localhost:8043 -o ServerAliveInterval=60 -o ServerAliveCountMax=3 -o PermitLocalCommand=yes -o LocalCommand="echo. & echo [OK] Tunnel established - open the PACS here: & echo. & echo    http://localhost:8043 & echo. & echo Leave this window open while you use the PACS. Close it to disconnect. & echo." %USER%@%IP%
echo.
echo Tunnel closed.
pause
