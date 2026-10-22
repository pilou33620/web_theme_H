#!/bin/sh
# Lance le serveur (et donc le jeu). Options : --port 8791, --local, --resolution 1280x720, --sans-jeu
cd "$(dirname "$0")" && exec python3 server.py "$@"
