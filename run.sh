#!/bin/bash

reset
while true; do
    echo "Starting the bot..."

    # Run your Discord bot
    python3.11 core.py

    # Check the exit code of the bot
    EXIT_CODE=$?
    if [ $EXIT_CODE -eq 0 ]; then
        echo "The bot has stopped normally. Exiting the script."
        break
    else
        echo "The bot stopped with an error (code $EXIT_CODE). Restarting in 5 seconds..."
        sleep 5
    fi

    echo "Clearing the screen before restarting..."
    clear
done

