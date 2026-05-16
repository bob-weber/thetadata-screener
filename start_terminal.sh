#!/bin/bash
if lsof -ti :25503 > /dev/null 2>&1; then
    echo "Theta terminal already running."
    exit 0
fi
echo "Starting Theta terminal..."
nohup java -jar ~/thetadata/ThetaTerminalv3.jar > ~/thetadata/thetadata.log 2>&1 &
echo "Started (PID $!). Log: ~/thetadata/thetadata.log"
