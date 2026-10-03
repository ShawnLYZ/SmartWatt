# Starts the SmartWatt API. Requires the broker to be running
# (tools/run-broker.ps1) and Mosquitto reachable at 127.0.0.1:1883.
$env:SMARTWATT_BROKER = "127.0.0.1"
$env:SMARTWATT_DB     = "smartwatt.db"

# For the one-hour soak in the acceptance gate, roll the 1 Hz rows after a
# minute instead of after 48 hours, and check every 30 s -- the retention
# loop only rolls rows OLDER than the retention window, so at the 48 h
# default a fresh database produces no telemetry_1min rows to count.
# Leave both unset for normal running.
#   $env:SMARTWATT_HZ_RETENTION_S   = "60"
#   $env:SMARTWATT_ROLLUP_INTERVAL_S = "30"

uv run uvicorn smartwatt_server.api:app --host 0.0.0.0 --port 8000
