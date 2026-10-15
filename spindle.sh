#!/bin/bash
# Start Spindle. Add --lan to open it from another computer on your network.
cd "$(dirname "$0")"
exec python3 -m spindle.server "$@"
