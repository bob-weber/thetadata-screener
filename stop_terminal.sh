#!/bin/bash
PID=$(lsof -ti :25503 2>/dev/null)
if [ -z "$PID" ]; then
    echo "Theta terminal not running."
    exit 0
fi
echo "Stopping Theta terminal (PID $PID)..."
kill $PID 2>/dev/null
for i in $(seq 1 5); do
    sleep 1
    if ! kill -0 $PID 2>/dev/null; then
        echo "Stopped."
        exit 0
    fi
done
echo "Force killing..."
kill -9 $PID 2>/dev/null
