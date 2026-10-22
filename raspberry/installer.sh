#!/bin/sh
# Installe ce qu'il faut sur un Raspberry Pi (Raspberry Pi OS, Debian, Ubuntu), puis le service.
#   sh raspberry/installer.sh                installe les dépendances + le service (démarrage à l'allumage)
#   sh raspberry/installer.sh 8800           idem, sur un autre port (défaut 8791)
#   sh raspberry/installer.sh --dependances  seulement les dépendances (on lance alors ./lancer.sh à la main)
#   sh raspberry/installer.sh --retirer      désinstalle le service
set -e
DIR=$(cd "$(dirname "$0")/.." && pwd)
SERVICE=web_theme_hospital
FICHIER=/etc/systemd/system/$SERVICE.service

if [ "$1" = "--retirer" ]; then
  sudo systemctl disable --now "$SERVICE" 2>/dev/null || true
  sudo rm -f "$FICHIER"
  sudo systemctl daemon-reload
  echo "Service $SERVICE retiré."
  exit 0
fi

echo "==> Dépendances"
sudo apt-get update
sudo apt-get install -y python3 xvfb x11vnc novnc
if ! command -v corsix-th >/dev/null 2>&1 && ! command -v CorsixTH >/dev/null 2>&1 && ! command -v corsixth >/dev/null 2>&1; then
  sudo apt-get install -y corsix-th || sudo snap install corsixth || {
    echo "!!! CorsixTH n'a pu être installé automatiquement (paquet corsix-th ou snap corsixth)."
    echo "    Installe-le à la main (https://corsixth.com), il doit être dans le PATH."
  }
fi
# noVNC absent des dépôts : on le télécharge dans vendor/novnc
if [ ! -f /usr/share/novnc/core/rfb.js ] && [ ! -f "$DIR/vendor/novnc/core/rfb.js" ]; then
  mkdir -p "$DIR/vendor/novnc"
  curl -fsSL https://github.com/novnc/noVNC/archive/refs/tags/v1.5.0.tar.gz | tar xz --strip-components=1 -C "$DIR/vendor/novnc"
fi

[ "$1" = "--dependances" ] && { echo "Dépendances installées. Lance : ./lancer.sh"; exit 0; }

PORT="${1:-8791}"
echo "==> Service systemd ($SERVICE, port $PORT)"
sudo tee "$FICHIER" >/dev/null <<EOF
[Unit]
Description=Theme Hospital dans le navigateur (web_theme_hospital)
After=network-online.target
Wants=network-online.target

[Service]
User=$(id -un)
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/server.py --port $PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE"
sleep 2
echo
echo "Service démarré. Copie le dossier de Theme Hospital dans : $DIR/theme_hospital/"
echo "Puis, sur la tablette, ouvre l'une de ces adresses :"
cat "$DIR/adresse-reseau.txt" 2>/dev/null || echo "   http://$(hostname).local:$PORT"
echo
echo "Commandes utiles : sudo systemctl restart $SERVICE   |   journalctl -u $SERVICE -f"
