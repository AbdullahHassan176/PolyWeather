$dir    = "D:\Experimentation\PolyWeather"
$python = "C:\Users\dullz\AppData\Local\Programs\Python\Python311\python.exe"

# Clear stale PID files
Remove-Item "$dir\polyweather.pid" -ErrorAction SilentlyContinue

# Start live bot only
Start-Process -FilePath $python `
    -ArgumentList "`"$dir\main.py`"" `
    -WorkingDirectory $dir `
    -WindowStyle Hidden

Write-Host "Live bot started. Tail polyweather.log to verify."
