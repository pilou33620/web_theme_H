#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bancs d'essai : python3 tests/test_serveur.py

Partie 1 (toujours) : un faux serveur VNC remplace x11vnc.
Partie 2 (si Xvfb, x11vnc et python-xlib sont installés) : la vraie chaîne, avec un faux jeu
qui journalise la souris et le clavier reçus.
"""
import asyncio
import base64
import json
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE))
import server  # noqa: E402

warnings.simplefilter('ignore', ResourceWarning)   # sockets de test fermés par la fin de la boucle


# ----------------------------------------------------------------- aides

class FauxVnc:
    """Parle juste assez de RFB (sans mot de passe) pour que Entree et le relais s'y connectent."""

    def __init__(self):
        self.recu_pointeur = []   # octets d'événements reçus par le client « entrée »
        self.recu_relais = b''

    async def client(self, lecture, ecriture):
        ecriture.write(b'RFB 003.008\n')
        await lecture.readexactly(12)
        ecriture.write(b'\x01\x01')                       # un seul type de sécurité : aucun
        await lecture.readexactly(1)
        ecriture.write(struct.pack('>I', 0))              # sécurité acceptée
        await lecture.readexactly(1)                      # ClientInit
        ecriture.write(struct.pack('>HH', 1280, 720) + b'\0' * 16 + struct.pack('>I', 4) + b'test')
        await ecriture.drain()
        try:
            while True:
                d = await lecture.read(1024)
                if not d:
                    return
                self.recu_relais += d
                if d[:1] in (b'\x04', b'\x05'):
                    self.recu_pointeur.append(d)
        except OSError:
            pass


def cle_ws():
    return base64.b64encode(os.urandom(16)).decode()


def trame_client(donnees, op=1):
    masque = os.urandom(4)
    n = len(donnees)
    tete = bytes([0x80 | op]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack('>H', n))
    return tete + masque + bytes(b ^ masque[i % 4] for i, b in enumerate(donnees))


async def lire_trame_serveur(lecture):
    t = await asyncio.wait_for(lecture.readexactly(2), 5)
    n = t[1] & 0x7F
    if n == 126:
        n = struct.unpack('>H', await lecture.readexactly(2))[0]
    return t[0] & 0x0F, await asyncio.wait_for(lecture.readexactly(n), 5)


async def ouvrir_ws(port, chemin):
    lecture, ecriture = await asyncio.open_connection('127.0.0.1', port)
    ecriture.write(('GET %s HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                    'Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: binary\r\n\r\n'
                    % (chemin, cle_ws())).encode())
    await ecriture.drain()
    tete = await asyncio.wait_for(lecture.readuntil(b'\r\n\r\n'), 5)
    return tete.decode(), lecture, ecriture


async def http(port, methode, chemin):
    lecture, ecriture = await asyncio.open_connection('127.0.0.1', port)
    ecriture.write(('%s %s HTTP/1.1\r\nHost: x\r\n\r\n' % (methode, chemin)).encode())
    await ecriture.drain()
    brut = await asyncio.wait_for(lecture.read(), 5)
    ecriture.close()
    tete, _, corps = brut.partition(b'\r\n\r\n')
    return int(tete.split()[1]), corps


# ----------------------------------------------------------------- partie 1 : sans programmes externes

class TestFichiers(unittest.TestCase):
    def test_trouver_donnees(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            self.assertIsNone(server.trouver_donnees(t))
            (t / 'Theme Hospital' / 'DATA').mkdir(parents=True)
            self.assertIsNone(server.trouver_donnees(t))                     # Data vide : pas le jeu
            (t / 'Theme Hospital' / 'DATA' / 'VBLK-0.DAT').write_bytes(b'x')
            self.assertEqual(server.trouver_donnees(t), t / 'Theme Hospital')  # trouvé dans un sous-dossier

    def test_chemin_hors_base_refuse(self):
        self.assertIsNone(server.fichier_sous(RACINE / 'web', '../server.py'))
        self.assertIsNone(server.fichier_sous(RACINE / 'web', '/%2e%2e/server.py'))
        self.assertIsNotNone(server.fichier_sous(RACINE / 'web', 'index.html'))
        self.assertIsNone(server.fichier_sous(None, 'index.html'))


class TestServeur(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.faux = FauxVnc()
        self.srv_vnc = await asyncio.start_server(self.faux.client, '127.0.0.1', 0)
        port_vnc = self.srv_vnc.sockets[0].getsockname()[1]
        self.jeu = server.Jeu(1280, 720, 98, port_vnc)
        self.jeu.etat = 'en_marche'        # on saute le lancement des programmes
        self.srv = await asyncio.start_server(server.Serveur(self.jeu).client, '127.0.0.1', 0)
        self.port = self.srv.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        for s in (self.srv, self.srv_vnc):
            s.close()

    async def test_pages_et_statut(self):
        code, corps = await http(self.port, 'GET', '/')
        self.assertEqual(code, 200)
        self.assertIn(b'Theme Hospital', corps)
        code, corps = await http(self.port, 'GET', '/api/status')
        self.assertEqual((code, json.loads(corps)['etat']), (200, 'en_marche'))
        self.assertEqual((await http(self.port, 'GET', '/../server.py'))[0], 404)
        self.assertEqual((await http(self.port, 'GET', '/inconnu'))[0], 404)
        self.assertEqual((await http(self.port, 'DELETE', '/'))[0], 405)

    async def test_relais_vnc(self):
        tete, lecture, ecriture = await ouvrir_ws(self.port, '/vnc')
        self.assertIn('101', tete)
        self.assertIn('Sec-WebSocket-Protocol: binary', tete)
        op, donnees = await lire_trame_serveur(lecture)
        self.assertEqual((op, donnees), (2, b'RFB 003.008\n'))             # le serveur VNC parle au navigateur
        ecriture.write(trame_client(b'RFB 003.008\n', 2))                   # le navigateur répond...
        await ecriture.drain()
        tampon = b''
        while len(tampon) < 2:
            tampon += (await lire_trame_serveur(lecture))[1]
        self.assertEqual(tampon, b'\x01\x01')                               # ...et reçoit la suite de la poignée de main
        ecriture.write(trame_client(b'\x01', 2))
        await ecriture.drain()
        tampon = b''
        while len(tampon) < 4:
            tampon += (await lire_trame_serveur(lecture))[1]
        self.assertEqual(tampon, struct.pack('>I', 0))
        ecriture.close()

    async def test_refus_si_jeu_arrete(self):
        self.jeu.etat = 'arrete'
        lecture, ecriture = await asyncio.open_connection('127.0.0.1', self.port)
        ecriture.write(('GET /vnc HTTP/1.1\r\nUpgrade: websocket\r\nSec-WebSocket-Key: %s\r\n\r\n' % cle_ws()).encode())
        self.assertIn(b'503', await asyncio.wait_for(lecture.read(), 5))

    async def test_relais_son(self):
        _, lecture, ecriture = await ouvrir_ws(self.port, '/son')
        for _ in range(50):
            if self.jeu.auditeurs:
                break
            await asyncio.sleep(0.02)
        for q in self.jeu.auditeurs:
            q.put_nowait(b'\x01\x00\x02\x00')
        self.assertEqual(await asyncio.wait_for(lire_trame_serveur(lecture), 5), (2, b'\x01\x00\x02\x00'))
        ecriture.close()
        for _ in range(50):                                                 # page fermée : plus d'auditeur
            if not self.jeu.auditeurs:
                break
            await asyncio.sleep(0.02)
        self.assertFalse(self.jeu.auditeurs)

    async def test_entree_pointeur_et_touches(self):
        _, lecture, ecriture = await ouvrir_ws(self.port, '/input')
        for msg in ({'t': 'p', 'x': 640, 'y': 360, 'b': 1},
                    {'t': 'p', 'x': 5000, 'y': -3, 'b': 4},                 # hors écran : ramené dans l'écran
                    {'t': 'k', 'k': 0xff52, 'd': 1},
                    {'t': 'k', 'k': 0xff52, 'd': 0},
                    {'t': 'p', 'x': 'abc'}, {'t': 'x'}):                    # messages invalides : ignorés
            ecriture.write(trame_client(json.dumps(msg).encode()))
        await ecriture.drain()
        await asyncio.sleep(0.3)
        attendu = (struct.pack('>BBHH', 5, 1, 640, 360) + struct.pack('>BBHH', 5, 4, 1279, 0) +
                   struct.pack('>BBxxI', 4, 1, 0xff52) + struct.pack('>BBxxI', 4, 0, 0xff52))
        self.assertEqual(b''.join(self.faux.recu_pointeur), attendu)
        ecriture.close()


# ----------------------------------------------------------------- partie 2 : la vraie chaîne

def chaine_reelle_possible():
    try:
        import Xlib  # noqa: F401
    except ImportError:
        return False
    return bool(shutil.which('Xvfb') and shutil.which('x11vnc'))


@unittest.skipUnless(chaine_reelle_possible(), 'Xvfb, x11vnc ou python-xlib manquant')
class TestChaineReelle(unittest.IsolatedAsyncioTestCase):
    async def test_demarrage_clics_arret(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            (t / 'jeu' / 'Data').mkdir(parents=True)
            (t / 'jeu' / 'Data' / 'VBlk-0.dat').write_bytes(b'x')
            for nom in ('DOSSIER_JEU', 'DONNEES', 'LOGS'):
                setattr(self, '_' + nom, getattr(server, nom))
            server.DOSSIER_JEU, server.DONNEES, server.LOGS = t / 'jeu', t / 'donnees', t / 'logs'
            journal = t / 'journal.txt'
            os.environ['FAUX_JOURNAL'] = str(journal)
            os.environ['TH_JEU'] = '%s %s' % (sys.executable, RACINE / 'tests' / 'faux_jeu.py')
            try:
                jeu = server.Jeu(1280, 720, 97, 5997)
                await jeu.demarrer()
                self.assertEqual((jeu.etat, jeu.message), ('en_marche', ''))
                await asyncio.sleep(0.5)
                self.assertIn('pret 1280x720', journal.read_text())
                self.assertIn('theme_hospital_install', (t / 'donnees' / 'corsixth' / 'config.txt').read_text())

                # le son du jeu arrive aux auditeurs en trames entières, au rythme du temps réel
                q = asyncio.Queue(1000)
                jeu.auditeurs.add(q)
                await asyncio.sleep(2)
                jeu.auditeurs.discard(q)
                morceaux = [q.get_nowait() for _ in range(q.qsize())]
                self.assertTrue(morceaux and all(len(m) % 4 == 0 for m in morceaux))
                self.assertEqual(morceaux[0][:4], b'\x01\x00\x02\x00')
                debit = sum(map(len, morceaux)) / 2 / (server.SON_FREQUENCE * 4)
                self.assertAlmostEqual(debit, 1, delta=0.05)

                # un clic gauche, un clic droit, puis une touche
                await jeu.saisir({'t': 'p', 'x': 200, 'y': 150, 'b': 0})
                await jeu.saisir({'t': 'p', 'x': 200, 'y': 150, 'b': 1})
                await jeu.saisir({'t': 'p', 'x': 200, 'y': 150, 'b': 0})
                await jeu.saisir({'t': 'p', 'x': 300, 'y': 250, 'b': 4})
                await jeu.saisir({'t': 'p', 'x': 300, 'y': 250, 'b': 0})
                await jeu.saisir({'t': 'k', 'k': 0x70, 'd': 1})
                await jeu.saisir({'t': 'k', 'k': 0x70, 'd': 0})
                await asyncio.sleep(1)
                lignes = journal.read_text().splitlines()
                for attendu in ('down 1 200 150', 'up 1 200 150', 'down 3 300 250', 'up 3 300 250',
                                'keydown p', 'keyup p'):
                    self.assertIn(attendu, lignes)

                # l'arrêt range tout, et un redémarrage repart proprement
                await jeu.arreter()
                self.assertEqual(jeu.etat, 'arrete')
                self.assertFalse(Path('/tmp/.X11-unix/X97').exists())
                await jeu.demarrer()
                self.assertEqual(jeu.etat, 'en_marche')
                await jeu.arreter()
            finally:
                server.DOSSIER_JEU, server.DONNEES, server.LOGS = self._DOSSIER_JEU, self._DONNEES, self._LOGS
                os.environ.pop('TH_JEU', None)

    async def test_jeu_sans_donnees(self):
        with tempfile.TemporaryDirectory() as t:
            ancien = server.DOSSIER_JEU
            server.DOSSIER_JEU = Path(t)
            try:
                jeu = server.Jeu(1280, 720, 96, 5996)
                await jeu.demarrer()
                self.assertEqual(jeu.etat, 'erreur')
                self.assertIn('theme_hospital', jeu.message)
            finally:
                server.DOSSIER_JEU = ancien


if __name__ == '__main__':
    unittest.main(verbosity=2, warnings='ignore')
