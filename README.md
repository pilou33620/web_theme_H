# web_theme_hospital — Theme Hospital sur la tablette

Un petit serveur Python lance **Theme Hospital** (via [CorsixTH](https://corsixth.com), le moteur libre) sur un Raspberry Pi et l'affiche dans le navigateur de la tablette, au doigt ou au stylet. Aucune bibliothèque Python à installer.

## Installation (Raspberry Pi 4, une seule fois)

```
git clone https://github.com/pilou33620/web_theme_H.git
cd web_theme_H
sh raspberry/installer.sh
```

Le script installe `xvfb`, `x11vnc`, `novnc`, CorsixTH, puis crée le service `web_theme_hospital` : le jeu démarre à chaque allumage du Raspberry. (`sh raspberry/installer.sh --dependances` installe seulement les dépendances.)

Avec [web_launcher](https://github.com/pilou33620/web_launcher) : installe seulement les dépendances (`--dependances`), sans le service ; le jeu se démarre et s'arrête depuis sa carte « Theme Hospital ».

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
| Glisser à deux doigts | Déplacer la carte |
| Stylet : poser | Clic gauche ; la pointe survolante (iPad récent) déplace le curseur |
| Bouton **Clic droit**, puis un appui | Clic droit en un seul appui (pour le stylet) |
| Bouton **↻ Tourner** | Quart de tour de l'objet en cours de pose (touche Espace), un par appui |
| Boutons ▲ ◀ ▶ ▼ (maintenus) | Défilement de la carte (flèches du clavier) |
| Boutons **+** et **−** | Zoom avant et arrière |
| Bouton **Sauver** | Sauvegarde rapide (Alt+Maj+S) ; la partie est aussi sauvegardée chaque mois |
| Boutons **Pause**, **Échap** | Touches P et Échap |
| Bouton **Triches** | Fenêtre des triches (F11) : « Argent » donne 10 000 $ par appui |
| ☰ | Plein écran, masquer les touches, redémarrer ou arrêter le jeu |

Pendant l'usage du stylet, la paume posée sur l'écran est ignorée. Un anneau blanc montre où se trouve le curseur.

## Options du serveur

`--port 8791` · `--local` (cette machine seulement) · `--resolution 1024x768` · `--sans-jeu` (ne pas lancer le jeu au démarrage) · `--affichage 99` · `--vnc-port 5999`

La résolution par défaut (4:3) remplit l'écran d'un iPad ; pour une tablette 16:9, prends `--resolution 1280x720`. Sur un Pi 4, `--resolution 800x600` allège le flux si l'image saccade.

## À savoir

- **Son** : bruitages, annonces et musique arrivent dans la page (environ 0,2 s de retard). Safari ne joue le son qu'après un premier toucher de l'écran. Volumes et musique : dans les options du jeu.
- **Paysage conseillé** : en paysage, les boutons passent en colonne à droite pour laisser libre la barre du bas du jeu ; en portrait, le jeu est plus petit.
- **Une seule partie à la fois** : tous les appareils connectés voient et commandent le même jeu.
- **Pas de mot de passe** : garde-le sur le réseau de la maison, ne l'ouvre pas sur Internet.
- Si le jeu plante ou si tu le quittes depuis son menu, la page propose « Démarrer ». Les journaux sont dans `logs/` (`xvfb.log`, `x11vnc.log`, `jeu.log`).
- Commandes utiles : `sudo systemctl status web_theme_hospital`, `sudo systemctl restart web_theme_hospital`, `journalctl -u web_theme_hospital -f`.

## Comment ça marche

```
server.py            serveur HTTP + WebSocket (Python standard) ; lance et surveille Xvfb, x11vnc, CorsixTH ;
                     /vnc relaie l'écran vers noVNC ; /input transforme les gestes en souris/clavier VNC ;
                     /son : le jeu écrit son son dans un tube (sortie « disk » de SDL), lu au rythme du temps réel
web/index.html       la page : affichage noVNC (lecture seule), gestes doigt/stylet/souris, touches à l'écran, son (Web Audio)
raspberry/           installer.sh : dépendances + service systemd
theme_hospital/      le dossier du jeu, à remplir (non suivi par git)
tests/               bancs d'essai : python3 tests/test_serveur.py
```

Les gestes ne passent pas par noVNC mais par un second client VNC minimal (`Entree`) : on contrôle ainsi le glisser, le clic droit et la paume.

## Tests

`python3 tests/test_serveur.py` : routes, relais VNC, relais du son, injection de la souris et du clavier avec un faux serveur VNC ; et, si `Xvfb`, `x11vnc` et `python3-xlib` sont installés, toute la chaîne avec un faux jeu qui journalise les clics reçus.
