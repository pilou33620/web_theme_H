#!/bin/sh
# Theme Hospital (Linux, Raspberry Pi) : obtient l'exécutable du serveur puis le lance ;
# garde le terminal ouvert en cas d'erreur.
#   ./demarrer_WEB_THEME_H.sh [--port 8791] [--local] [--resolution 1024x768] [--sans-jeu] [--affichage 99] [--vnc-port 5999] [--sans-maj]
cd "$(dirname "$0")" || exit 1
. ./scripts/binaires.sh
# Code 75 : « mis à jour, relancez-moi » (§4 des règles des web tools) : on reprend l'exécutable
# de la nouvelle version, puis on le relance.
export THEME_H_RELANCEUR=1
while :; do
  obtenir_binaires && bin/theme_h "$@"
  code=$?
  [ "$code" -eq 75 ] || break
  export THEME_H_DEJA_MAJ=1
done
# pause seulement devant un vrai terminal (pas sous systemd ni dans le terminal de web_launcher, TERM=dumb)
if [ "$code" -ne 0 ] && [ -t 0 ] && [ "$TERM" != dumb ]; then
  echo
  echo "[X] Le démarrage a échoué. La raison est écrite au-dessus."
  echo
  printf "Appuyez sur Entrée pour fermer..."; read -r _
fi
exit "$code"
