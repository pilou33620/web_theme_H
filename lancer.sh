#!/bin/sh
# Ancien point d'entrée, gardé pour les services et web_launcher : voir demarrer_WEB_THEME_H.sh.
exec sh "$(dirname "$0")/demarrer_WEB_THEME_H.sh" "$@"
