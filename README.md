# web_theme_hospital — Theme Hospital sur la tablette

Un petit serveur Python lance **Theme Hospital** (via [CorsixTH](https://corsixth.com), le moteur libre) sur un Raspberry Pi et l'affiche dans le navigateur de la tablette, au doigt ou au stylet. Aucune bibliothèque Python à installer.

## Installation (Raspberry Pi 4, une seule fois)

```
git clone https://github.com/pilou33620/web_theme_hospital.git
cd web_theme_hospital
sh raspberry/installer.sh
```

Le script installe `xvfb`, `x11vnc`, `novnc`, CorsixTH, puis crée le service `web_theme_hospital` : le jeu démarre à chaque allumage du Raspberry. (`sh raspberry/installer.sh --dependances` installe seulement les dépendances.)

## Utilisation

1. Copie ton dossier de Theme Hospital (celui qui contient `Data`) dans **`theme_hospital/`**. Tu peux l'acheter sur GOG ; la démo gratuite marche aussi.
2. Démarre : automatique avec le service, sinon `./lancer.sh`. Le jeu se lance tout seul.
3. Sur la tablette (même Wi-Fi), ouvre l'adresse affichée (aussi écrite dans `adresse-reseau.txt`), par exemple `http://192.168.1.42:8791` ou `http://raspberrypi.local:8791`. Sur iPad : Partager → « Sur l'écran d'accueil » pour jouer en plein écran.

Si le dossier du jeu manque, la page l'indique et propose « Réessayer » dès que tu l'as copié. Les parties sont sauvegardées dans `donnees/corsixth/saves/`.

## Commandes tactiles

| Geste | Effet |
|---|---|
| Taper (doigt) | Clic gauche |
| Glisser (doigt) | Clic gauche maintenu et déplacé (poser une salle, tracer un couloir) |
| Taper à deux doigts | Clic droit |
| Stylet : poser | Clic gauche ; la pointe survolante (iPad récent) déplace le curseur |
| Bouton **Clic droit**, puis un appui | Clic droit en un seul appui (pour le stylet) |
| Boutons ▲ ◀ ▶ ▼ (maintenus) | Défilement de la carte (flèches du clavier) |
| Boutons **Pause**, **Échap** | Touches P et Échap |
| ☰ | Plein écran, masquer les touches, redémarrer ou arrêter le jeu |

Pendant l'usage du stylet, la paume posée sur l'écran est ignorée. Un anneau blanc montre où se trouve le curseur.

## Options du serveur

`--port 8791` · `--local` (cette machine seulement) · `--resolution 1280x720` · `--sans-jeu` (ne pas lancer le jeu au démarrage) · `--affichage 99` · `--vnc-port 5999`

Sur un Pi 4, `--resolution 1024x576` allège le flux si l'image saccade.

## À savoir

- **Pas de son** : l'audio du jeu est coupé (le navigateur ne reçoit que l'image).
- **Une seule partie à la fois** : tous les appareils connectés voient et commandent le même jeu.
- **Pas de mot de passe** : garde-le sur le réseau de la maison, ne l'ouvre pas sur Internet.
- Si le jeu plante ou si tu le quittes depuis son menu, la page propose « Démarrer ». Les journaux sont dans `logs/` (`xvfb.log`, `x11vnc.log`, `jeu.log`).
- Commandes utiles : `sudo systemctl status web_theme_hospital`, `sudo systemctl restart web_theme_hospital`, `journalctl -u web_theme_hospital -f`.

## Comment ça marche

```
server.py            serveur HTTP + WebSocket (Python standard) ; lance et surveille Xvfb, x11vnc, CorsixTH ;
                     /vnc relaie l'écran vers noVNC ; /input transforme les gestes en souris/clavier VNC
web/index.html       la page : affichage noVNC (lecture seule), gestes doigt/stylet/souris, touches à l'écran
raspberry/           installer.sh : dépendances + service systemd
theme_hospital/      le dossier du jeu, à remplir (non suivi par git)
tests/               bancs d'essai : python3 tests/test_serveur.py
```

Les gestes ne passent pas par noVNC mais par un second client VNC minimal (`Entree`) : on contrôle ainsi le glisser, le clic droit et la paume.

## Tests

`python3 tests/test_serveur.py` : routes, relais VNC, injection de la souris et du clavier avec un faux serveur VNC ; et, si `Xvfb`, `x11vnc` et `python3-xlib` sont installés, toute la chaîne avec un faux jeu qui journalise les clics reçus.
