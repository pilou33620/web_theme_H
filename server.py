#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web_theme_hospital : Theme Hospital (CorsixTH) dans le navigateur, jouable à la tablette.

Le serveur lance un écran virtuel (Xvfb), le jeu et x11vnc, relaie l'écran vers la page
(noVNC) et transforme les gestes (doigt, stylet, souris) en clics sur l'écran virtuel.
Python standard uniquement (3.8+). Linux seulement (Raspberry Pi OS, Debian, Ubuntu...).
"""
import argparse
import asyncio
import base64
import fcntl
import hashlib
import json
import mimetypes
import os
import shlex
import shutil
import signal
import socket
import struct
import sys
import time
from pathlib import Path
from urllib.parse import unquote

RACINE = Path(__file__).resolve().parent
WEB = RACINE / 'web'
DOSSIER_JEU = RACINE / 'theme_hospital'
DONNEES = RACINE / 'donnees'
LOGS = RACINE / 'logs'
GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
SON_FREQUENCE = 22050   # son du jeu : PCM 16 bits signé petit-boutiste, stéréo (format de SDL_mixer)
TYPES = {'.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
         '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
         '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png'}


class Erreur(Exception):
    """Erreur lisible, affichée telle quelle dans la page."""


# ----------------------------------------------------------------- recherche des fichiers

def trouver_novnc():
    candidats = [os.environ.get('NOVNC_DIR', ''), '/usr/share/novnc',
                 '/usr/share/javascript/novnc', str(RACINE / 'vendor' / 'novnc')]
    for c in candidats:
        if c and (Path(c) / 'core' / 'rfb.js').is_file():
            return Path(c)
    return None


def trouver_donnees(base, profondeur=3):
    """Dossier (base ou sous-dossier) qui contient Data/VBlk-0.dat (casse libre), sinon None."""
    try:
        enfants = sorted(Path(base).iterdir())
    except OSError:
        return None
    for e in enfants:
        if e.is_dir() and e.name.lower() == 'data':
            try:
                if any(f.name.lower() == 'vblk-0.dat' for f in e.iterdir()):
                    return Path(base)
            except OSError:
                pass
    if profondeur > 0:
        for e in enfants:
            if e.is_dir() and not e.name.startswith('.'):
                r = trouver_donnees(e, profondeur - 1)
                if r:
                    return r
    return None


def commande_jeu():
    env = os.environ.get('TH_JEU')
    if env:
        return shlex.split(env)
    for nom in ('corsix-th', 'CorsixTH', 'corsixth'):
        chemin = shutil.which(nom, path=os.environ.get('PATH', '') + os.pathsep + '/usr/games')  # Debian : /usr/games hors du PATH
        if chemin:
            return [chemin]
    return None


def ecrire_config_jeu(dossier_jeu, largeur, hauteur):
    """Écrit le config.txt de CorsixTH (le jeu n'est jamais configuré à la main)."""
    dossier = DONNEES / 'corsixth'
    (dossier / 'saves').mkdir(parents=True, exist_ok=True)
    (dossier / 'captures').mkdir(parents=True, exist_ok=True)
    chemin = dossier / 'config.txt'
    chemin.write_text(
        'theme_hospital_install = [[%s]]\n'
        'width = %d\nheight = %d\nfullscreen = false\n'
        'play_intro = false\ncheck_for_updates = false\n'
        'audio = true\naudio_frequency = %d\naudio_channels = 2\naudio_buffer_size = 1024\n'
        'prevent_edge_scrolling = true\n'  # au doigt, le curseur reste au bord : la carte défilerait sans fin
        'debug = true\n'  # F11 ouvre la fenêtre des triches (bouton « Triches » de la page)
        'savegames = [[%s]]\nscreenshots = [[%s]]\n'
        % (dossier_jeu, largeur, hauteur, SON_FREQUENCE, dossier / 'saves', dossier / 'captures'),
        encoding='utf-8')
    return chemin


def nettoyer_affichage(num):
    """Supprime le verrou d'un Xvfb mort (arrêt brutal) qui empêcherait de redémarrer."""
    verrou = Path('/tmp/.X%s-lock' % num)
    try:
        pid = int(verrou.read_text().strip())
    except (OSError, ValueError):
        return
    try:
        os.kill(pid, 0)
        return  # encore vivant : on n'y touche pas
    except ProcessLookupError:
        pass
    except OSError:
        return
    for f in (verrou, Path('/tmp/.X11-unix/X%s' % num)):
        try:
            f.unlink()
        except OSError:
            pass


# ----------------------------------------------------------------- entrées : client VNC minimal

class Entree:
    """Second client VNC, qui n'affiche rien : il injecte la souris et le clavier."""

    def __init__(self, ecriture):
        self.w = ecriture
        self.tache = None

    @classmethod
    async def ouvrir(cls, port):
        lecture, ecriture = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', port), 5)
        try:
            await asyncio.wait_for(lecture.readexactly(12), 5)          # "RFB 003.008\n"
            ecriture.write(b'RFB 003.008\n')
            n = (await asyncio.wait_for(lecture.readexactly(1), 5))[0]
            if n == 0:
                raise ConnectionError('connexion refusée par x11vnc')
            if 1 not in await lecture.readexactly(n):
                raise ConnectionError('x11vnc demande un mot de passe')
            ecriture.write(b'\x01')                                     # sécurité : aucune
            if struct.unpack('>I', await lecture.readexactly(4))[0] != 0:
                raise ConnectionError('poignée de main VNC refusée')
            ecriture.write(b'\x01')                                     # ClientInit : partagé
            await ecriture.drain()
            await lecture.readexactly(4 + 16)                           # taille + format de pixels
            nom = struct.unpack('>I', await lecture.readexactly(4))[0]
            await lecture.readexactly(nom)
        except BaseException:
            ecriture.close()
            raise
        e = cls(ecriture)
        e.tache = asyncio.ensure_future(e._vider(lecture))              # on ignore tout ce que x11vnc envoie
        return e

    @staticmethod
    async def _vider(lecture):
        try:
            while await lecture.read(65536):
                pass
        except (OSError, asyncio.CancelledError):
            pass

    def _ecrire(self, octets):
        if self.w.is_closing():
            raise ConnectionError('entrée fermée')
        self.w.write(octets)

    def pointeur(self, x, y, masque, largeur, hauteur):
        x = max(0, min(largeur - 1, x))
        y = max(0, min(hauteur - 1, y))
        self._ecrire(struct.pack('>BBHH', 5, masque & 0x1F, x, y))

    def touche(self, keysym, appuyee):
        self._ecrire(struct.pack('>BBxxI', 4, 1 if appuyee else 0, keysym))

    def fermer(self):
        if self.tache:
            self.tache.cancel()
        self.w.close()


# ----------------------------------------------------------------- le jeu et son écran virtuel

class Jeu:
    def __init__(self, largeur=1024, hauteur=768, affichage=99, port_vnc=5999):
        self.largeur, self.hauteur = largeur, hauteur
        self.affichage, self.port_vnc = str(affichage), port_vnc
        self.etat = 'arrete'   # arrete | demarrage | en_marche | erreur
        self.message = ''
        self.procs = []
        self.entree = None
        self._verrou = asyncio.Lock()
        self._verrou_entree = asyncio.Lock()
        self._taches = set()
        self.auditeurs = set()   # files des pages qui écoutent le son
        self._son, self._pompe = None, None

    def statut(self):
        return {'etat': self.etat, 'message': self.message, 'largeur': self.largeur,
                'hauteur': self.hauteur, 'son': SON_FREQUENCE, 'novnc': trouver_novnc() is not None,
                'jeu_trouve': trouver_donnees(DOSSIER_JEU) is not None}

    def lancer_en_fond(self, coroutine):
        t = asyncio.ensure_future(coroutine)
        self._taches.add(t)
        t.add_done_callback(self._taches.discard)

    async def _lancer(self, nom, commande, env=None):
        LOGS.mkdir(exist_ok=True)
        try:
            with open(LOGS / ('%s.log' % nom), 'wb') as log:
                p = await asyncio.create_subprocess_exec(
                    *commande, stdout=log, stderr=log, stdin=asyncio.subprocess.DEVNULL,
                    env=env, start_new_session=True)
        except OSError as e:
            raise Erreur('Impossible de lancer %s : %s' % (commande[0], e))
        self.procs.append((nom, p))
        return p

    async def _attendre(self, test, delai, quoi, proc):
        fin = time.monotonic() + delai
        while time.monotonic() < fin:
            if proc.returncode is not None:
                raise Erreur('%s s\'est arrêté au démarrage (voir logs/).' % quoi)
            if await test():
                return
            await asyncio.sleep(0.2)
        raise Erreur('%s ne répond pas (voir logs/).' % quoi)

    async def demarrer(self):
        async with self._verrou:
            if self.etat in ('demarrage', 'en_marche'):
                return
            self.etat, self.message = 'demarrage', ''
            try:
                await self._demarrer()
            except Erreur as e:
                await self._tout_arreter()
                self.etat, self.message = 'erreur', str(e)
            except BaseException:
                await self._tout_arreter()
                self.etat, self.message = 'erreur', 'Erreur inattendue au démarrage.'
                raise

    async def _demarrer(self):
        dossier = trouver_donnees(DOSSIER_JEU)
        if not dossier:
            raise Erreur('Dossier de Theme Hospital introuvable : copie-le dans « theme_hospital/ » '
                         '(il doit contenir le dossier Data).')
        jeu = commande_jeu()
        if not jeu:
            raise Erreur('CorsixTH est introuvable : lance « sh raspberry/installer.sh --dependances ».')
        xvfb = shlex.split(os.environ.get('TH_XVFB', '')) or ['Xvfb']
        x11vnc = shlex.split(os.environ.get('TH_X11VNC', '')) or ['x11vnc']
        config = ecrire_config_jeu(dossier, self.largeur, self.hauteur)
        tube = DONNEES / 'son.fifo'
        son = {'SDL_AUDIODRIVER': 'disk', 'SDL_DISKAUDIOFILE': str(tube), 'SDL_DISKAUDIODELAY': '0'} \
            if self._ouvrir_son(tube) \
            else {'SDL_AUDIODRIVER': 'dummy'}
        # rendu 2D de SDL et affichage X11 direct : sans GPU, l'OpenGL logiciel coûtait ~5 fois plus de processeur
        env = dict(os.environ, DISPLAY=':' + self.affichage, SDL_VIDEODRIVER='x11', SDL_RENDER_DRIVER='software',
                   SDL_FRAMEBUFFER_ACCELERATION='0', LIBGL_ALWAYS_SOFTWARE='1', **son)

        nettoyer_affichage(self.affichage)
        p = await self._lancer('xvfb', xvfb + [':' + self.affichage, '-screen', '0',
                                               '%dx%dx24' % (self.largeur, self.hauteur), '-nolisten', 'tcp'])
        socket_x = Path('/tmp/.X11-unix/X' + self.affichage)

        async def x_pret():
            return socket_x.exists()
        await self._attendre(x_pret, 10, 'Xvfb', p)

        p = await self._lancer('x11vnc', x11vnc + ['-display', ':' + self.affichage, '-rfbport',
                                                   str(self.port_vnc), '-localhost', '-nopw',
                                                   '-forever', '-shared', '-quiet'], env)

        async def vnc_pret():
            try:
                _, w = await asyncio.open_connection('127.0.0.1', self.port_vnc)
            except OSError:
                return False
            w.close()
            return True
        await self._attendre(vnc_pret, 10, 'x11vnc', p)

        p = await self._lancer('jeu', jeu + ['--config-file=%s' % config], env)
        await asyncio.sleep(2)  # un jeu qui plante au chargement le fait dans les deux premières secondes
        if p.returncode is not None:
            raise Erreur('Le jeu s\'est arrêté au démarrage (voir logs/jeu.log).')
        self.etat = 'en_marche'
        self.lancer_en_fond(self._surveiller())

    def _ouvrir_son(self, tube):
        """Tube où le jeu écrit son son (sortie « disk » de SDL, sans pause : DISKAUDIODELAY=0)."""
        try:
            if tube.exists():
                tube.unlink()
            os.mkfifo(tube)
            # O_RDWR : l'ouverture ne bloque pas, et pas de fin de fichier tant que le jeu n'écrit pas encore
            self._son = os.open(tube, os.O_RDWR | os.O_NONBLOCK)
        except OSError:
            return False   # pas de son, le jeu tourne quand même
        try:
            fcntl.fcntl(self._son, getattr(fcntl, 'F_SETPIPE_SZ', 1031), 8192)   # petit tube : peu de retard
        except OSError:
            pass
        self._pompe = asyncio.ensure_future(self._pomper_son())
        return True

    async def _pomper_son(self):
        """Lit le tube au rythme du temps réel : tube plein, le fil audio du jeu attend, et le son sort
        exactement à la bonne vitesse (la pause de SDL, elle, dérive de plusieurs %)."""
        debit = SON_FREQUENCE * 4                          # octets par seconde : 2 canaux x 16 bits
        t0, lus, reste = time.monotonic(), 0, b''
        while True:
            await asyncio.sleep(0.02)
            du = int((time.monotonic() - t0) * debit) - lus
            try:
                d = os.read(self._son, du) if du > 0 else b''
            except BlockingIOError:
                d = b''
            lus += len(d)
            if du > debit // 5:                            # 0,2 s de retard : jeu en pause ou pas encore de son
                t0, lus = time.monotonic(), 0
            d = reste + d
            coupe = len(d) - len(d) % 4                    # trames entières
            d, reste = d[:coupe], d[coupe:]
            for q in self.auditeurs:
                if d and not q.full():
                    q.put_nowait(d)                        # page trop lente : ce morceau est sauté

    async def _surveiller(self):
        """Si le jeu (ou son écran) s'arrête, on range tout : la page propose de redémarrer."""
        procs = list(self.procs)
        if not procs:
            return  # tout a déjà été arrêté avant que cette tâche ne démarre
        attentes = {asyncio.ensure_future(p.wait()): nom for nom, p in procs}
        fini, _ = await asyncio.wait(list(attentes), return_when=asyncio.FIRST_COMPLETED)
        for t in attentes:
            if t not in fini:
                t.cancel()
        if self.etat != 'en_marche' or procs != self.procs:
            return  # arrêt demandé, ou une autre session a pris le relais
        nom = attentes[next(iter(fini))]
        await self._tout_arreter()
        self.etat = 'arrete'
        self.message = 'Partie terminée.' if nom == 'jeu' else 'L\'écran virtuel s\'est arrêté (%s).' % nom

    async def _tout_arreter(self):
        if self.entree:
            self.entree.fermer()
            self.entree = None
        procs, self.procs = self.procs, []
        for _, p in reversed(procs):
            if p.returncode is not None:
                continue
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(p.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    await asyncio.wait_for(p.wait(), 3)
                    break
                except asyncio.TimeoutError:
                    continue
        if self._pompe:
            self._pompe.cancel()
        if self._son is not None:
            os.close(self._son)
        self._son, self._pompe = None, None

    async def arreter(self):
        async with self._verrou:
            self.etat, self.message = 'arrete', ''
            await self._tout_arreter()

    async def redemarrer(self):
        await self.arreter()
        await self.demarrer()

    async def saisir(self, msg):
        """Applique un message de la page : {t:'p',x,y,b} (pointeur) ou {t:'k',k,d} (touche)."""
        if self.etat != 'en_marche':
            return
        try:
            async with self._verrou_entree:
                if self.entree is None:
                    self.entree = await Entree.ouvrir(self.port_vnc)
            if msg.get('t') == 'p':
                self.entree.pointeur(int(msg['x']), int(msg['y']), int(msg.get('b', 0)),
                                     self.largeur, self.hauteur)
            elif msg.get('t') == 'k':
                self.entree.touche(int(msg['k']), bool(msg.get('d')))
        except (OSError, ValueError, KeyError, TypeError, AttributeError, struct.error,
                asyncio.IncompleteReadError, asyncio.TimeoutError):
            if self.entree:
                self.entree.fermer()
            self.entree = None  # rouvert au prochain message


# ----------------------------------------------------------------- WebSocket (RFC 6455, minimal)

def demasquer(donnees, cle):
    if not donnees:
        return donnees
    k = (cle * (len(donnees) // 4 + 1))[:len(donnees)]
    return (int.from_bytes(donnees, 'big') ^ int.from_bytes(k, 'big')).to_bytes(len(donnees), 'big')


class WS:
    def __init__(self, lecture, ecriture):
        self.r, self.w = lecture, ecriture

    async def envoyer(self, donnees, op=2):
        if isinstance(donnees, str):
            donnees, op = donnees.encode(), 1
        n = len(donnees)
        if n < 126:
            tete = bytes([0x80 | op, n])
        elif n < 65536:
            tete = bytes([0x80 | op, 126]) + struct.pack('>H', n)
        else:
            tete = bytes([0x80 | op, 127]) + struct.pack('>Q', n)
        self.w.write(tete + donnees)
        await self.w.drain()

    async def recevoir(self):
        """Message complet (opcode, octets), ou None quand la connexion se ferme."""
        tampon, op0 = b'', 2
        while True:
            try:
                t = await self.r.readexactly(2)
                fin, op, n = t[0] & 0x80, t[0] & 0x0F, t[1] & 0x7F
                if n == 126:
                    n = struct.unpack('>H', await self.r.readexactly(2))[0]
                elif n == 127:
                    n = struct.unpack('>Q', await self.r.readexactly(8))[0]
                if n > 8 << 20:
                    return None
                cle = await self.r.readexactly(4) if t[1] & 0x80 else b''
                d = await self.r.readexactly(n)
                if cle:
                    d = demasquer(d, cle)
                if op == 8:
                    return None
                if op == 9:
                    await self.envoyer(d, 10)
                    continue
            except (asyncio.IncompleteReadError, OSError):
                return None
            if op == 10:
                continue
            if op in (1, 2):
                op0, tampon = op, d
            else:
                tampon += d
            if fin:
                return op0, tampon

    def fermer(self):
        self.w.close()


# ----------------------------------------------------------------- serveur HTTP

def reponse(code, corps=b'', type_='text/plain; charset=utf-8', cache='no-store'):
    raisons = {200: 'OK', 202: 'Accepted', 400: 'Bad Request', 403: 'Forbidden', 404: 'Not Found',
               405: 'Method Not Allowed', 503: 'Service Unavailable'}
    if isinstance(corps, str):
        corps = corps.encode('utf-8')
    tete = 'HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\nCache-Control: %s\r\nConnection: close\r\n\r\n' % (
        code, raisons.get(code, 'OK'), type_, len(corps), cache)
    return tete.encode() + corps


def fichier_sous(base, chemin):
    """Fichier de `base` demandé par `chemin`, jamais en dehors de `base`."""
    if base is None:
        return None
    base = base.resolve()
    cible = (base / unquote(chemin).lstrip('/')).resolve()
    try:
        cible.relative_to(base)
    except ValueError:
        return None
    return cible if cible.is_file() else None


class Serveur:
    def __init__(self, jeu):
        self.jeu = jeu

    async def client(self, lecture, ecriture):
        try:
            ligne = await asyncio.wait_for(lecture.readline(), 10)
            morceaux = ligne.decode('latin1').split(' ')
            if len(morceaux) < 2:
                return
            methode, chemin = morceaux[0], morceaux[1].split('?', 1)[0]
            entetes = {}
            while True:
                l = await asyncio.wait_for(lecture.readline(), 10)
                if l in (b'\r\n', b'\n', b''):
                    break
                k, _, v = l.decode('latin1').partition(':')
                entetes[k.strip().lower()] = v.strip()
            if entetes.get('upgrade', '').lower() == 'websocket':
                await self.websocket(lecture, ecriture, chemin, entetes)
            else:
                ecriture.write(await self.http(methode, chemin))
                await ecriture.drain()
        except (asyncio.TimeoutError, OSError, UnicodeDecodeError):
            pass
        finally:
            ecriture.close()

    async def http(self, methode, chemin):
        if chemin.startswith('/api/'):
            actions = {'/api/demarrer': self.jeu.demarrer, '/api/arreter': self.jeu.arreter,
                       '/api/redemarrer': self.jeu.redemarrer}
            if chemin == '/api/status' and methode == 'GET':
                return reponse(200, json.dumps(self.jeu.statut()), 'application/json')
            if chemin in actions and methode == 'POST':
                self.jeu.lancer_en_fond(actions[chemin]())
                await asyncio.sleep(0.05)
                return reponse(202, json.dumps(self.jeu.statut()), 'application/json')
            return reponse(404, 'Introuvable')
        if methode != 'GET':
            return reponse(405, 'Méthode non permise')
        if chemin.startswith('/novnc/'):
            cible, cache = fichier_sous(trouver_novnc(), chemin[len('/novnc/'):]), 'max-age=86400'
        else:
            cible, cache = fichier_sous(WEB, 'index.html' if chemin == '/' else chemin), 'no-cache'
        if cible is None:
            return reponse(404, 'Introuvable')
        type_ = TYPES.get(cible.suffix) or mimetypes.guess_type(cible.name)[0] or 'application/octet-stream'
        return reponse(200, cible.read_bytes(), type_, cache)

    async def websocket(self, lecture, ecriture, chemin, entetes):
        cle = entetes.get('sec-websocket-key')
        if chemin not in ('/vnc', '/input', '/son') or not cle:
            ecriture.write(reponse(404, 'Introuvable'))
            return
        if self.jeu.etat != 'en_marche':
            ecriture.write(reponse(503, 'Le jeu n\'est pas démarré'))
            return
        accept = base64.b64encode(hashlib.sha1(cle.encode() + GUID).digest()).decode()
        lignes = ['HTTP/1.1 101 Switching Protocols', 'Upgrade: websocket', 'Connection: Upgrade',
                  'Sec-WebSocket-Accept: ' + accept]
        if 'binary' in [p.strip() for p in entetes.get('sec-websocket-protocol', '').split(',')]:
            lignes.append('Sec-WebSocket-Protocol: binary')
        ecriture.write(('\r\n'.join(lignes) + '\r\n\r\n').encode())
        await ecriture.drain()
        ws = WS(lecture, ecriture)
        if chemin == '/vnc':
            await self.relais_vnc(ws)
        elif chemin == '/son':
            await self.relais_son(ws)
        else:
            await self.relais_entree(ws)

    async def relais_vnc(self, ws):
        try:
            tr, tw = await asyncio.open_connection('127.0.0.1', self.jeu.port_vnc)
        except OSError:
            return

        async def vers_tcp():
            try:
                while True:
                    m = await ws.recevoir()
                    if m is None:
                        return
                    tw.write(m[1])
                    await tw.drain()
            except OSError:
                pass

        async def vers_ws():
            try:
                while True:
                    d = await tr.read(65536)
                    if not d:
                        return
                    await ws.envoyer(d)
            except OSError:
                pass

        taches = [asyncio.ensure_future(vers_tcp()), asyncio.ensure_future(vers_ws())]
        try:
            await asyncio.wait(taches, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in taches:
                t.cancel()
            tw.close()

    async def relais_son(self, ws):
        q = asyncio.Queue(20)                             # au plus ~1 s de son en attente
        self.jeu.auditeurs.add(q)
        fin = asyncio.ensure_future(ws.recevoir())        # la page n'envoie rien : se termine à la fermeture
        try:
            while True:
                morceau = asyncio.ensure_future(q.get())
                await asyncio.wait([morceau, fin], return_when=asyncio.FIRST_COMPLETED)
                if fin.done():
                    morceau.cancel()
                    return
                await ws.envoyer(morceau.result())
        except OSError:
            pass
        finally:
            self.jeu.auditeurs.discard(q)
            fin.cancel()

    async def relais_entree(self, ws):
        while True:
            m = await ws.recevoir()
            if m is None:
                return
            try:
                msg = json.loads(m[1])
            except ValueError:
                continue
            if isinstance(msg, dict):
                await self.jeu.saisir(msg)


# ----------------------------------------------------------------- démarrage

def adresses(port):
    ips = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('10.255.255.255', 1))   # aucun paquet envoyé : sert à trouver l'interface
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    urls = ['http://%s:%d' % (ip, port) for ip in ips]
    urls.append('http://%s.local:%d' % (socket.gethostname(), port))
    return urls


def verifier_dependances():
    manque = [n for n in ('Xvfb', 'x11vnc') if not shutil.which(n)]
    if manque:
        print('!!! Programmes manquants : %s (sudo apt install xvfb x11vnc)' % ', '.join(manque))
    if not trouver_novnc():
        print('!!! noVNC introuvable (sudo apt install novnc)')
    if not commande_jeu():
        print('!!! CorsixTH introuvable (sudo apt install corsix-th, ou sudo snap install corsixth)')


async def principal(a):
    largeur, hauteur = (int(v) for v in a.resolution.lower().split('x'))
    jeu = Jeu(largeur, hauteur, a.affichage, a.vnc_port)
    hote = '127.0.0.1' if a.local else '0.0.0.0'
    try:
        serveur = await asyncio.start_server(Serveur(jeu).client, hote, a.port)
    except OSError as e:
        sys.exit('Port %d indisponible : %s' % (a.port, e))
    arret = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, arret.set)
    verifier_dependances()
    urls = adresses(a.port) if not a.local else ['http://127.0.0.1:%d' % a.port]
    print('\n   Pour jouer depuis la tablette (même Wi-Fi), ouvre l\'une de ces adresses :\n')
    for u in urls:
        print('      ' + u)
    print()
    (RACINE / 'adresse-reseau.txt').write_text('\n'.join(urls) + '\n', encoding='utf-8')
    if not a.sans_jeu:
        jeu.lancer_en_fond(jeu.demarrer())
    await arret.wait()
    serveur.close()
    await jeu.arreter()


def main():
    p = argparse.ArgumentParser(description='Theme Hospital (CorsixTH) dans le navigateur')
    p.add_argument('--port', type=int, default=8791)
    p.add_argument('--lan', action='store_true', help='ouvert au réseau local (par défaut)')
    p.add_argument('--local', action='store_true', help='cette machine seulement')
    p.add_argument('--resolution', default='1024x768', help='taille de l\'écran du jeu (défaut 1024x768, format 4:3 de l\'iPad)')
    p.add_argument('--affichage', type=int, default=99, help='numéro de l\'écran virtuel X (défaut 99)')
    p.add_argument('--vnc-port', type=int, default=5999, help='port VNC interne (défaut 5999)')
    p.add_argument('--sans-jeu', action='store_true', help='ne pas lancer le jeu au démarrage')
    asyncio.run(principal(p.parse_args()))


if __name__ == '__main__':
    main()
