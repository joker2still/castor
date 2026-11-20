#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate


while true; do
    echo "[$(date)] Starting castor --web..."
    python3 -u server.py >> logs/web.log 2>&1
    EXIT_CODE=$?
    echo "[$(date)] castor exited with code $EXIT_CODE, restarting in 3s..."
    sleep 3
done
