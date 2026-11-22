# web_theme_hospital — Theme Hospital sur la tablette

Un petit serveur (en Rust) lance **Theme Hospital** (via [CorsixTH](https://corsixth.com), le moteur libre) sur un Raspberry Pi et l'affiche dans le navigateur de la tablette, au doigt ou au stylet. Linux seulement (Raspberry Pi OS, Debian, Ubuntu).

## Installation (Raspberry Pi 4, une seule fois)

```
git clone https://github.com/pilou33620/web_theme_H.git
cd web_theme_H
sh raspberry/installer.sh
```

Le script installe `curl`, `xvfb`, `x11vnc`, `novnc`, CorsixTH, puis crée le service `web_theme_hospital` : le jeu démarre à chaque allumage du Raspberry.

Rust n'est pas nécessaire : au premier lancement, `demarrer_WEB_THEME_H.sh` (par `scripts/binaires.sh`) télécharge l'exécutable du serveur (publication « binaires » du dépôt, compilée par la CI pour Linux x86_64 et aarch64, vérifiée par son empreinte SHA-256) dans `bin/`, puis le reprend à chaque nouvelle version. Si `cargo` est installé, il le compile sur place à la place. (`sh raspberry/installer.sh --dependances` installe seulement les dépendances.)

`lancer.sh`, l'ancien nom, relaie vers `demarrer_WEB_THEME_H.sh` avec les mêmes options (un service installé avant garde donc marche).

**Sans git** : l'archive de production `theme_h-prod-linux-aarch64.tar.gz` (Raspberry Pi 64 bits) ou `theme_h-prod-linux-x86_64.tar.gz`, dans la [publication « binaires »](https://github.com/pilou33620/web_theme_H/releases/tag/binaires), donne un dossier `theme_h/` prêt à lancer (`sh raspberry/installer.sh`, ou `./demarrer_WEB_THEME_H.sh`). À chaque démarrage (sauf `--sans-maj`), le serveur compare son archive à la dernière publiée ; si elle a changé, il la télécharge, vérifie son empreinte SHA-256, recopie ses fichiers par-dessus et redémarre. Le jeu (`theme_hospital/`), les sauvegardes (`donnees/`), `vendor/` et les journaux ne sont pas dans l'archive : jamais touchés.

Avec [web_launcher](https://github.com/pilou33620/web_launcher) : installe seulement les dépendances (`--dependances`), sans le service ; le jeu se démarre et s'arrête depuis sa carte « Theme Hospital ».

## Mettre à jour CorsixTH

Debian garde une version ancienne de CorsixTH (0.68.0 alors que la 0.70.1 existe). Pour avoir la dernière version stable, directement depuis le git du projet :

```
sh raspberry/corsixth.sh --verifier   # dit s'il y a plus récent, n'installe rien
sh raspberry/corsixth.sh              # installe ou met à jour (git + compilation, ~30 min sur un Pi 4)
```

La première fois, le script demande le mot de passe `sudo` pour installer les paquets de compilation. Ensuite tout se passe dans `vendor/` (hors git) : l'ancienne version reste utilisable jusqu'à la fin de la compilation. Redémarre ensuite le jeu. `--retirer` revient à la version de Debian.

## Utilisation

1. Copie ton dossier de Theme Hospital (celui qui contient `Data`) dans **`theme_hospital/`**. Tu peux l'acheter sur GOG ; la démo gratuite marche aussi.
2. Démarre : automatique avec le service, sinon `./demarrer_WEB_THEME_H.sh`. Le jeu se lance tout seul.
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

`--port 8791` · `--local` (cette machine seulement) · `--lan` (réseau local : c'est déjà le défaut) · `--resolution 1024x768` · `--sans-jeu` (ne pas lancer le jeu au démarrage) · `--affichage 99` · `--vnc-port 5999`. `--sans-maj` : pas de mise à jour de la copie de production (sans effet sur un clone git). Acceptées sans effet, pour web_launcher : `--sans-navigateur`, `--sans-pause`. Les valeurs s'écrivent aussi `--port=8791`. Variables d'essai : `TH_JEU`, `TH_XVFB`, `TH_X11VNC` (autre commande), `NOVNC_DIR`.

La résolution par défaut (4:3) remplit l'écran d'un iPad ; pour une tablette 16:9, prends `--resolution 1280x720`. Sur un Pi 4, `--resolution 800x600` allège le flux si l'image saccade.

## À savoir

- **Son** : bruitages, annonces et musique arrivent dans la page (environ 0,2 s de retard). Safari ne joue le son qu'après un premier toucher de l'écran. Volumes et musique : dans les options du jeu.
- **Paysage conseillé** : en paysage, les boutons passent en colonne à droite pour laisser libre la barre du bas du jeu ; en portrait, le jeu est plus petit.
- **Une seule partie à la fois** : tous les appareils connectés voient et commandent le même jeu.
- **Pas de mot de passe** : garde-le sur le réseau de la maison, ne l'ouvre pas sur Internet.
- **x11vnc refuse de démarrer dans une session Wayland** (variable `WAYLAND_DISPLAY` posée, par exemple dans un terminal du bureau) : lance le serveur par le service, par web_launcher, ou par `env -u WAYLAND_DISPLAY ./demarrer_WEB_THEME_H.sh`.
- Si le jeu plante ou si tu le quittes depuis son menu, la page propose « Démarrer ». Les journaux sont dans `logs/` (`xvfb.log`, `x11vnc.log`, `jeu.log`).
- Commandes utiles : `sudo systemctl status web_theme_hospital`, `sudo systemctl restart web_theme_hospital`, `journalctl -u web_theme_hospital -f`.

## Comment ça marche

```
rust/theme_h/        le serveur (Rust) : lib.rs lance et surveille Xvfb, x11vnc, CorsixTH, lit le son, injecte les gestes ;
                     serveur.rs : routes, WebSocket (/vnc relaie l'écran vers noVNC ; /input transforme les gestes
                     en souris/clavier VNC ; /son : le jeu écrit son son dans un tube, sortie « disk » de SDL,
                     lu au rythme du temps réel), options
demarrer_WEB_THEME_H.sh  obtient l'exécutable (scripts/binaires.sh), le lance, le relance après une mise à jour (code 75)
lancer.sh            ancien nom, relaie vers demarrer_WEB_THEME_H.sh
web/index.html       la page : affichage noVNC (lecture seule), gestes doigt/stylet/souris, touches à l'écran, son (Web Audio)
raspberry/           installer.sh : dépendances + service systemd ; corsixth.sh : CorsixTH à jour depuis git
theme_hospital/      le dossier du jeu, à remplir (non suivi par git)
```

Les gestes ne passent pas par noVNC mais par un second client VNC minimal (`Entree`) : on contrôle ainsi le glisser, le clic droit et la paume.

## Sécurité

Sans `--local`, le serveur passe par la garde de la caisse commune `web_tools_commun` (dépôt web_suite) : un autre appareil entre une fois le code d'appairage affiché dans le terminal et sur la carte du launcher. Un nom d'hôte inconnu et une adresse Internet sont refusés, et HTTPS est servi sur le même port (CA du poste à installer sur la tablette). Le relais du launcher ne demande pas de code.

## Tests

`cargo test -p theme_h` (lancé par la CI à chaque envoi) : fichiers du jeu, config de CorsixTH, ligne de commande, routes, garde (appareil non appairé), relais VNC, relais du son, injection de la souris et du clavier avec un faux serveur VNC. `binaires.yml` vérifie en plus que l'archive de production Linux, décompressée dans un dossier vide, démarre et sert la page.
