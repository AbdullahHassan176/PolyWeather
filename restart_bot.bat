@echo off
cd /d D:\Experimentation\PolyWeather
echo Stopping existing bot instance...
taskkill /F /IM python.exe /FI "WINDOWTITLE eq PolyWeather*" 2>nul
del /F /Q polyweather.pid 2>nul

title PolyWeather Bot
:loop
echo [%date% %time%] Starting bot...
py -3.11 main.py
echo [%date% %time%] Bot exited (code %errorlevel%) -- restarting in 10s...
timeout /t 10 /nobreak >nul
del /F /Q polyweather.pid 2>nul
goto loop
