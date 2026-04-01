@echo off
title PolyWeather Bot
cd /d D:\Experimentation\PolyWeather

:loop
echo [%date% %time%] Starting bot...
del /F /Q polyweather.pid 2>nul
py -3.11 main.py
echo [%date% %time%] Bot exited (code %errorlevel%) — restarting in 10s...
timeout /t 10 /nobreak >nul
goto loop
