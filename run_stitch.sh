#!/bin/bash

export DJ_USER="stitch-lead"
export DJ_PASSWORD="NSPChunks4ever!"
export DJ_DATABASE_HOST="elias.bcms.bcm.edu"
export DATALAKE_PATH="/mnt/datalake/data/emu"
export ENVIRONMENT="prod"
export STITCHED_PATH="/mnt/stitched/EMU-18112"
export PROJECTWORLDS_PATH="/mnt/projectworlds/EMU-18112"
export DJ_SUPPORT_FILEPATH_MANAGEMENT="TRUE"
export LOGGING_PATH="/home/stitch-lead/EMU24Schema/log.txt"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

nohup python "/home/stitch-lead/EMU24Schema/scripts/populate.py" --patient YFW > "$SCRIPT_DIR/test_speech_247_pop_nohup.log" 2>&1 &

echo "Started with PID $! — logs at $SCRIPT_DIR/test_speech_247_pop_nohup.log"
