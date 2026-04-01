@echo off
cd /d D:\Experimentation\PolyWeather
echo Stopping existing paper bot instance...
taskkill /F /IM python.exe /FI "WINDOWTITLE eq PolyWeather Paper*" 2>nul
del /F /Q polyweather_paper.pid 2>nul

title PolyWeather Paper Bot
:loop
echo [%date% %time%] Starting paper bot (experimental filters)...
py -3.11 main.py --paper
echo [%date% %time%] Paper bot exited (code %errorlevel%) -- restarting in 10s...
timeout /t 10 /nobreak >nul
del /F /Q polyweather_paper.pid 2>nul
goto loop
