#!/usr/bin/env bash
#
# Run Yasuho with an auto-restart loop. Uses the virtualenv created by ./setup.sh,
# falling back to the system python if no venv is present.
#
cd "$(dirname "$0")"

if [ -x ./.venv/bin/python ]; then
    PY=./.venv/bin/python
else
    PY="$(command -v python3.11 || command -v python3.10 || command -v python3)"
    echo "[run] No .venv found - using $PY (run ./setup.sh to create one)."
fi

while true; do
    echo "Starting the bot..."
    "$PY" core.py
    EXIT_CODE=$?

    if [ $EXIT_CODE -eq 0 ]; then
        echo "The bot has stopped normally. Exiting."
        break
    fi
    echo "The bot stopped with an error (code $EXIT_CODE). Restarting in 5 seconds..."
    sleep 5
done
