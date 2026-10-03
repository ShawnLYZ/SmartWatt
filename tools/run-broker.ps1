# Starts the SmartWatt broker in the foreground. Ctrl-C to stop.
$mosquitto = "C:\Program Files\mosquitto\mosquitto.exe"
$config    = Join-Path $PSScriptRoot "mosquitto.conf"
& $mosquitto -c $config -v
