#!/bin/sh
# Theme Hospital (Linux, Raspberry Pi) : obtient l'exécutable du serveur (binaires.sh) puis le lance.
#   ./lancer.sh [--port 8791] [--local] [--resolution 1024x768] [--sans-jeu] [--affichage 99] [--vnc-port 5999]
cd "$(dirname "$0")" || exit 1
. ./binaires.sh
# Code 75 : « mis à jour, relancez-moi » (§4 des règles des web tools) : on reprend l'exécutable
# de la nouvelle version, puis on le relance.
export THEME_H_RELANCEUR=1
while :; do
  obtenir_binaires && bin/theme_h "$@"
  code=$?
  [ "$code" -eq 75 ] || break
done
exit "$code"
