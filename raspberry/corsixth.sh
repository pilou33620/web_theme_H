#!/bin/sh
# CorsixTH à jour, sans attendre Debian : le code vient de git et se compile sur le Pi (vendor/).
#   sh raspberry/corsixth.sh                 installe ou met à jour vers la dernière version stable (~30 min sur un Pi 4)
#   sh raspberry/corsixth.sh --verifier      dit s'il existe une version plus récente (n'installe rien, sans sudo)
#   sh raspberry/corsixth.sh --dependances   installe seulement les paquets de compilation (sudo)
#   sh raspberry/corsixth.sh --retirer       supprime la version compilée : le jeu reprend celle de Debian
# Après une mise à jour : redémarrer le jeu (carte « Theme Hospital » du launcher, ou systemctl restart).
set -e
DIR=$(cd "$(dirname "$0")/.." && pwd)
DEPOT=https://github.com/CorsixTH/CorsixTH.git
SRC=$DIR/vendor/CorsixTH-src
BUILD=$DIR/vendor/CorsixTH-build
PREFIX=$DIR/vendor/corsixth
PAQUETS="cmake ninja-build pkg-config g++ git zlib1g-dev libsdl2-dev libsdl2-mixer-dev liblua5.4-dev
  libfreetype-dev libavcodec-dev libavformat-dev libavutil-dev libswscale-dev libswresample-dev
  lua-filesystem lua-lpeg"

installee=$(cat "$PREFIX/VERSION" 2>/dev/null || true)
[ -n "$installee" ] || installee=$(dpkg-query -W -f='v${Version}' corsix-th 2>/dev/null | sed 's/-[^-]*$//' || true)

# dernière version stable : tag vX.Y.Z, sans -rc, -beta ou -alpha
derniere=$(git ls-remote --tags --refs "$DEPOT" 'v[0-9]*' | sed 's|.*refs/tags/||' | grep -v -E -- '-(rc|beta|alpha)' | sort -V | tail -n 1)
[ -n "$derniere" ] || { echo "Impossible de lire les versions de CorsixTH sur GitHub (réseau ?)."; exit 1; }

case "$1" in
  --verifier)
    echo "CorsixTH installé : ${installee:-aucun}"
    echo "Dernière version  : $derniere"
    if [ "$installee" = "$derniere" ]; then echo "À jour."
    else echo "Mise à jour disponible : sh raspberry/corsixth.sh"; fi
    exit 0 ;;
  --retirer)
    rm -rf "$PREFIX" "$BUILD" "$SRC"
    echo "Version compilée supprimée : le jeu reprendra celle de Debian au prochain démarrage."
    exit 0 ;;
esac

manque=""
for p in $PAQUETS; do dpkg -s "$p" >/dev/null 2>&1 || manque="$manque $p"; done
if [ -n "$manque" ]; then
  echo "==> Paquets de compilation :$manque"
  sudo apt-get update
  sudo apt-get install -y $manque
fi
[ "$1" = "--dependances" ] && { echo "Paquets installés. Lance : sh raspberry/corsixth.sh"; exit 0; }

if [ "$installee" = "$derniere" ] && [ -x "$PREFIX/bin/corsix-th" ]; then
  echo "CorsixTH $derniere est déjà installé."; exit 0
fi

echo "==> Code source de CorsixTH $derniere"
mkdir -p "$DIR/vendor"
if [ -d "$SRC/.git" ]; then
  git -C "$SRC" fetch --depth 1 origin "refs/tags/$derniere:refs/tags/$derniere"
  git -C "$SRC" checkout -q "$derniere"
else
  rm -rf "$SRC"
  git clone --depth 1 --branch "$derniere" "$DEPOT" "$SRC"
fi

echo "==> Compilation (long : 2 processeurs seulement, pour laisser le Pi respirer)"
# pas de libcurl (vérification de version du jeu coupée : check_for_updates = false) ni de RtMidi
# (instrument MIDI branché : la musique passe par la banque de sons logicielle)
cmake -S "$SRC" -B "$BUILD" -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$PREFIX" \
  -DWITH_UPDATE_CHECK=OFF -DWITH_MIDI_DEVICE=OFF
nice -n 10 cmake --build "$BUILD" -j 2

echo "==> Installation dans $PREFIX"
rm -f "$PREFIX/bin/corsix-th"   # supprimer un programme en cours d'exécution est permis, l'écraser non
cmake --install "$BUILD"
echo "$derniere" > "$PREFIX/VERSION"
echo
echo "CorsixTH $derniere installé. Redémarre le jeu pour l'utiliser."
