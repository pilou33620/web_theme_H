"""Appairage des web tools : un code à 6 chiffres ouvre l'accès réseau à un appareil.

    python appairage.py      vérification (banc d'essai, sans réseau)

COPIE IDENTIQUE dans chaque web tool (web_suite, WEB_CAO, WEB_ANTENNA, WEB_3D,
web_ferme) : une correction se fait partout.

Un serveur qui écoute sur le réseau local exige des autres appareils un jeton.
Le taper (22 caractères) était pénible : le serveur affiche donc dans son
terminal un code à 6 chiffres, que l'on saisit sur la tablette. Le code ne
remplace pas le jeton, il le DONNE : bon code, la tablette reçoit le cookie du
jeton. Seul un appareil qui voit l'écran du serveur peut donc entrer. Ce poste
lui-même (boucle locale) n'a jamais besoin de code.

UN SEUL JETON, UN SEUL COOKIE POUR TOUS LES TOOLS D'UN POSTE : le jeton est
dans ~/.web_tools/jeton-reseau.txt, et un navigateur envoie un cookie à tous
les ports d'une même adresse. Une tablette appairée une fois (au lanceur ou à
n'importe quel tool) est reconnue par tous les tools de ce poste.

HTTPS EN MODE RÉSEAU : sur le Wi-Fi, le cookie du jeton circulait en clair ;
qui écoutait le récupérait et devenait un appareil appairé. Une autorité (CA)
propre à ce poste, créée une fois dans ~/.web_tools/tls/ par la commande
openssl, signe le certificat du serveur (les adresses privées du poste, IPv4
et IPv6 : Tailscale fd7a:115c:a1e0::, ULA fc00::/7 ; pas le lien local fe80::). Un
appareil l'installe une fois (/appairage/ca.pem) et fait confiance à tous les
tools du poste. Le MÊME PORT sert HTTPS et HTTP : un premier octet 0x16 est une
poignée de main TLS. La boucle locale garde HTTP (navigateur du poste, MCP,
bancs) ; un autre appareil arrivé en HTTP est renvoyé vers https://. Sans
openssl (Windows sans Git), ou avec WEB_TOOLS_HTTP=1, tout reste en HTTP
et le terminal le dit.

Usage dans un serveur (http.server) :

    GARDE = appairage.Garde("WEB_3D", actif=ecoute_sur_le_reseau)
    class Gestionnaire(SimpleHTTPRequestHandler):
        def parse_request(self):
            return super().parse_request() and GARDE.filtrer(self)
    ...
    GARDE.brancher(serveur)   # en mode réseau : HTTPS sur le port du serveur
    GARDE.annoncer()          # au démarrage : affiche le code dans le terminal
"""

import functools
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.parse
from http.cookies import CookieError, SimpleCookie

BISCUIT = "websuite_jeton"          # le nom posé par WEB·SUITE depuis toujours : ses appareils restent appairés
PAGE = "/appairage"                 # la page du code (sa propre adresse : Safari ressortait « / » de son cache)
ROUTE = "/api/appairer"             # la seule route ouverte sans jeton, en POST
ENTETE = "X-Appairage"              # en-tête maison : un autre site ne peut pas l'ajouter sans pré-vol CORS
MARQUE = "[connexion]"              # WEB·SUITE recopie ces lignes du journal d'un outil dans son terminal
CA = PAGE + "/ca.pem"               # le certificat de la CA du poste, à installer sur la tablette


def fichier_jeton():
    return os.environ.get("WEB_TOOLS_JETON_FICHIER") or os.path.join(
        os.path.expanduser("~"), ".web_tools", "jeton-reseau.txt")


def _lire(chemin):
    try:
        with open(chemin, encoding="utf-8") as f:
            j = f.read().strip()
        return j if len(j) >= 16 else None
    except OSError:
        return None


def jeton(ancien=None):
    """Le jeton partagé par les tools de ce poste, gardé d'un démarrage à l'autre
    (les favoris de la tablette restent bons). Supprimer le fichier en crée un neuf.

    ancien : fichier d'un jeton propre à un tool (lanceur/jeton-reseau.txt de
    WEB·SUITE) ; repris s'il n'y a pas encore de jeton partagé, pour que les
    appareils déjà appairés le restent."""
    chemin = fichier_jeton()
    j = _lire(chemin)
    if j:
        return j
    neuf = (ancien and _lire(ancien)) or secrets.token_urlsafe(16)
    try:
        os.makedirs(os.path.dirname(chemin), exist_ok=True)
        # O_EXCL : deux tools qui démarrent ensemble ne s'écrasent pas leur jeton
        fd = os.open(chemin, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        for _ in range(50):                 # l'autre tool est en train de l'écrire
            j = _lire(chemin)
            if j:
                return j
            time.sleep(0.02)
        # Toujours vide ou trop court (démarrage coupé en pleine écriture) : on le remplace.
        # Rendre `neuf` sans l'écrire changeait de jeton à chaque démarrage.
        temp = "%s.%d.tmp" % (chemin, os.getpid())
        try:
            with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
                f.write(neuf + "\n")
            os.replace(temp, chemin)
        except OSError:
            try:
                os.remove(temp)
            except OSError:
                pass
            print("  %s illisible et impossible à remplacer : jeton de cette session seulement." % chemin, flush=True)
            return neuf
        print("  %s était vide ou abîmé : jeton neuf écrit, les appareils redonneront le code." % chemin, flush=True)
        # shortcut: deux tools qui le remplacent au même instant gardent le dernier écrit ; un verrou si ça arrive
        return _lire(chemin) or neuf
    except OSError:
        # Dossier personnel en lecture seule : un jeton pour
        # cette session seulement ; les appareils redonneront le code au redémarrage.
        return neuf
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(neuf + "\n")
    return neuf


def masquer(texte):
    """Une ligne de journal sans le jeton (« GET /?jeton=... » avant la redirection 303)."""
    return re.sub(r"jeton=[^&\s\"]+", "jeton=***", texte)


def ip_tailscale():
    """Adresse Tailscale (100.64/10) de ce poste, ou None. 100.100.100.100 est l'adresse
    interne de Tailscale : sa route passe par tailscale0 (aucun paquet n'est envoyé)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("100.100.100.100", 1))
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return ip if ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10") else None


@functools.lru_cache(maxsize=1)
def nom_tailscale():
    """Le nom MagicDNS exact de CE poste (pilou.tailxxxx.ts.net), "" sans Tailscale."""
    # shortcut: gardé jusqu'au redémarrage de l'outil, à revoir si le poste change de nom Tailscale
    try:
        r = subprocess.run(["tailscale", "status", "--json", "--peers=false"], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=3)
        return json.loads(r.stdout)["Self"]["DNSName"].lower().rstrip(".")
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
        return ""


def hote_permis(entete):
    """L'en-tête Host désigne-t-il ce poste ? (parade au DNS rebinding)

    Une page piégée qui fait résoudre son nom vers 127.0.0.1 devient « de même
    origine » ; elle envoie alors SON nom dans Host. Une IP littérale ne se
    rebranche pas : on accepte toutes les IP (la tablette tape celle du poste),
    localhost, le nom du poste et son nom Tailscale exact (jamais tout *.ts.net : n'importe
    qui peut en avoir un)."""
    h = (entete or "").strip().lower().rstrip(".")
    if h.startswith("["):
        if "]" not in h:
            return False                    # « [ » seul : rien à reconnaître
        h = h[1:].split("]")[0]
    elif h.count(":") == 1:
        h = h.split(":")[0]
    nom = socket.gethostname().lower()
    if h in ("", "localhost", nom, nom + ".local"):
        return True
    if h.endswith(".ts.net"):
        return h == nom_tailscale()
    try:
        ipaddress.ip_address(h.split("%")[0])
        return True
    except ValueError:
        return False


# -- HTTPS ------------------------------------------------------------------
# La CA ne peut signer QUE des adresses privées et des noms locaux (nameConstraints) :
# installée sur une tablette, elle ne pourrait pas servir à imiter un site d'Internet,
# même si sa clé quittait ce poste.
# fc00::/7 (ULA) contient le fd7a:115c:a1e0::/48 de Tailscale. Changer PLAGES refait la CA
# (ca.json) : chaque appareil doit alors la réinstaller.
PLAGES = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "127.0.0.0/8",
          "fc00::/7")
TLS_JOURS = 397                     # en dessous de la limite d'Apple (398) ; renouvelé à 30 jours


def dossier_tls():
    return os.path.join(os.path.dirname(fichier_jeton()), "tls")


def _openssl():
    o = shutil.which("openssl")
    for p in (o, r"C:\Program Files\Git\usr\bin\openssl.exe",
              r"C:\Program Files\Git\mingw64\bin\openssl.exe"):
        if p and os.path.exists(p):
            return p
    return None


def _nom_local():
    nom = socket.gethostname().split(".")[0].lower()
    return nom if re.fullmatch(r"[a-z0-9-]{1,63}", nom) else ""


def adresses_poste():
    """Les adresses privées de ce poste (réseau local, Tailscale, boucle locale), IPv4 d'abord."""
    ips = {"127.0.0.1"}
    # fd7a:115c:a1e0::53 : le DNS de Tailscale ; fdff:… : une ULA du réseau local, s'il en a
    for famille, cible in ((socket.AF_INET, "192.168.255.255"), (socket.AF_INET, "10.255.255.255"),
                           (socket.AF_INET, "172.31.255.255"), (socket.AF_INET6, "fd7a:115c:a1e0::53"),
                           (socket.AF_INET6, "fdff:ffff:ffff:ffff::1")):
        try:
            with socket.socket(famille, socket.SOCK_DGRAM) as so:
                so.connect((cible, 1))           # aucun paquet n'est envoyé
                ips.add(so.getsockname()[0])
        except OSError:
            pass
    try:
        ips.update(a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None))
    except OSError:
        pass
    t = ip_tailscale()
    if t:
        ips.add(t)
    plages = [ipaddress.ip_network(p) for p in PLAGES]
    ips = {str(ipaddress.ip_address(i.split("%")[0])) for i in ips}
    return sorted((i for i in ips if any(ipaddress.ip_address(i) in p for p in plages)),
                  key=lambda i: (":" in i, i))


def _cnf(chemin, texte):
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(texte)


def _generer(dossier, openssl, adresses, ca_neuve):
    """Crée la CA (si ca_neuve) puis le certificat du serveur, par la commande openssl
    (fichiers de configuration : la même syntaxe pour OpenSSL et LibreSSL)."""
    nom = _nom_local()
    tmp = tempfile.mkdtemp(dir=dossier)
    try:
        def lancer(*args):
            r = subprocess.run([openssl] + list(args), cwd=tmp, capture_output=True, text=True, timeout=60)
            if r.returncode:
                raise OSError("openssl %s : %s" % (args[0], (r.stderr or r.stdout).strip()[-300:]))
        if ca_neuve:
            masque = lambda p: "%s/%s" % (p.network_address, p.netmask)
            contraintes = ",".join(["permitted;IP:" + masque(ipaddress.ip_network(p)) for p in PLAGES]
                                   + ["permitted;DNS:localhost", "permitted;DNS:local"]
                                   + (["permitted;DNS:" + nom] if nom else []))
            _cnf(os.path.join(tmp, "ca.cnf"),
                 "[req]\ndistinguished_name=dn\nprompt=no\nx509_extensions=v3\n"
                 "[dn]\nCN=WEB tools %s (CA locale)\n[v3]\n"
                 "basicConstraints=critical,CA:TRUE,pathlen:0\n"
                 "keyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n"
                 "nameConstraints=critical,%s\n" % (nom or "poste", contraintes))
            lancer("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "3650",
                   "-config", "ca.cnf", "-keyout", "ca.key", "-out", "ca.pem")
            _cnf(os.path.join(tmp, "ca.json"), json.dumps({"plages": list(PLAGES)}))
        else:
            for f in ("ca.key", "ca.pem"):
                shutil.copy(os.path.join(dossier, f), tmp)
        noms = ["localhost"] + ([nom, nom + ".local"] if nom else [])
        _cnf(os.path.join(tmp, "req.cnf"),
             "[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=%s\n" % (nom or "localhost"))
        _cnf(os.path.join(tmp, "ext.cnf"),
             "[v3]\nbasicConstraints=critical,CA:FALSE\n"
             "keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n"
             "subjectAltName=%s\n" % ",".join(["DNS:" + n for n in noms] + ["IP:" + i for i in adresses]))
        lancer("req", "-new", "-newkey", "rsa:2048", "-nodes", "-config", "req.cnf",
               "-keyout", "serveur.key", "-out", "serveur.csr")
        lancer("x509", "-req", "-sha256", "-days", str(TLS_JOURS), "-in", "serveur.csr",
               "-CA", "ca.pem", "-CAkey", "ca.key", "-set_serial", str(secrets.randbits(63)),
               "-extfile", "ext.cnf", "-extensions", "v3", "-out", "serveur.pem")
        _cnf(os.path.join(tmp, "serveur.json"),
             json.dumps({"adresses": adresses, "noms": noms,
                         "expire": time.time() + TLS_JOURS * 86400}))
        for f in ("ca.key", "serveur.key"):
            os.chmod(os.path.join(tmp, f), 0o600)
        # la CA d'abord : un tool qui lit entre deux trouve au pire un certificat à refaire
        for f in (["ca.key", "ca.pem", "ca.json"] if ca_neuve else []) + ["serveur.key", "serveur.pem", "serveur.json"]:
            os.replace(os.path.join(tmp, f), os.path.join(dossier, f))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _ca_a_refaire(dossier):
    """CA absente, ou faite pour d'autres PLAGES (une CA d'avant IPv6 n'a pas de ca.json) :
    ses contraintes de nom refuseraient le certificat du serveur."""
    try:
        with open(os.path.join(dossier, "ca.json"), encoding="utf-8") as f:
            return (json.load(f)["plages"] != list(PLAGES)
                    or not os.path.exists(os.path.join(dossier, "ca.pem"))
                    or not os.path.exists(os.path.join(dossier, "ca.key")))
    except (OSError, ValueError, KeyError, TypeError):
        return True


def _a_refaire(dossier, adresses):
    try:
        with open(os.path.join(dossier, "serveur.json"), encoding="utf-8") as f:
            d = json.load(f)
        return (_ca_a_refaire(dossier) or not set(adresses) <= set(d["adresses"])
                or d["expire"] - time.time() < 30 * 86400
                or not os.path.exists(os.path.join(dossier, "serveur.pem")))
    except (OSError, ValueError, KeyError, TypeError):
        return True


def empreinte_ca(dossier=None):
    """SHA-256 du certificat de la CA, comme iOS l'affiche (« Plus de détails »)."""
    with open(os.path.join(dossier or dossier_tls(), "ca.pem"), encoding="ascii") as f:
        der = ssl.PEM_cert_to_DER_cert(f.read())
    h = hashlib.sha256(der).hexdigest().upper()
    return " ".join(h[i:i + 2] for i in range(0, len(h), 2))


def contexte_tls():
    """(contexte SSL du serveur, None) ou (None, raison de rester en HTTP)."""
    if os.environ.get("WEB_TOOLS_HTTP") == "1":
        return None, "WEB_TOOLS_HTTP=1"
    openssl = _openssl()
    dossier = dossier_tls()
    adresses = adresses_poste()
    verrou = os.path.join(dossier, "verrou")
    try:
        os.makedirs(dossier, exist_ok=True)
        if _a_refaire(dossier, adresses):
            if not openssl:
                return None, "la commande openssl est introuvable"
            # Deux tools qui démarrent ensemble : un seul crée la CA, l'autre attend.
            for _ in range(300):
                try:
                    os.close(os.open(verrou, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
                    break
                except FileExistsError:
                    try:
                        if time.time() - os.path.getmtime(verrou) > 120:
                            os.remove(verrou)          # laissé par un tool tué en route
                    except FileNotFoundError:
                        pass                           # libéré entre-temps
                    time.sleep(0.1)
            else:
                return None, "verrou %s tenu trop longtemps" % verrou
            try:
                if _a_refaire(dossier, adresses):
                    _generer(dossier, openssl, adresses, _ca_a_refaire(dossier))
            finally:
                os.remove(verrou)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(os.path.join(dossier, "serveur.pem"), os.path.join(dossier, "serveur.key"))
        return ctx, None
    except (OSError, ssl.SSLError, subprocess.SubprocessError) as exc:
        return None, "certificat impossible : %s" % exc


def brancher_tls(serveur, ctx):
    """Le port du serveur sert aussi HTTPS : chaque connexion est lue sans être consommée
    (MSG_PEEK) ; 0x16 ouvre une poignée de main TLS, le reste est du HTTP."""
    serveur.tls_web_tools = ctx
    origine = serveur.finish_request

    def finir(requete, adresse):
        attente = requete.gettimeout()
        try:
            requete.settimeout(15)               # un client muet ne garde pas le fil
            premier = requete.recv(1, socket.MSG_PEEK)
            if premier == b"\x16":
                requete = ctx.wrap_socket(requete, server_side=True)
            requete.settimeout(attente)
        except (OSError, ssl.SSLError):
            return                               # certificat refusé par le client, coupure…
        try:
            origine(requete, adresse)
        finally:
            if isinstance(requete, ssl.SSLSocket):
                try:
                    requete.close()
                except OSError:
                    pass
    serveur.finish_request = finir


def en_https(handler):
    # SSLObject : un serveur asyncio (web_theme_H), qui n'a pas de socket SSL à lui
    return isinstance(getattr(handler, "connection", None), (ssl.SSLSocket, ssl.SSLObject))


def filtrer_tls(handler, ce_poste=None):
    """True : la requête suit son cours. Sert la CA du poste (sans jeton : elle est
    publique), et renvoie vers https:// un autre appareil arrivé en HTTP."""
    if not getattr(handler.server, "tls_web_tools", None):
        return True
    u = urllib.parse.urlsplit(handler.path)
    if handler.command in ("GET", "HEAD") and u.path == CA:
        try:
            with open(os.path.join(dossier_tls(), "ca.pem"), "rb") as f:
                corps = f.read()
        except OSError:
            Garde._repondre(handler, 404, "CA introuvable")
            return False
        # ce type-là : Safari propose d'installer le profil
        Garde._repondre(handler, 200, corps, "application/x-x509-ca-cert",
                        [("Content-Disposition", 'attachment; filename="web-tools-ca.pem"')])
        return False
    if en_https(handler) or (ce_poste or Garde.ce_poste)(handler):
        return True
    Garde._repondre(handler, 307, "HTTPS", entetes=[
        ("Location", "https://%s%s" % (handler.headers.get("Host") or "", handler.path))])
    return False


def hote_url(h):
    """Une IPv6 littérale va entre crochets dans une adresse : https://[fd7a::1]:8080/."""
    return "[%s]" % h if ":" in h and not h.startswith("[") else h


def ligne_https(nom_ou_ip, port):
    """Ce que le terminal affiche à côté de l'adresse réseau."""
    return ("  HTTPS : la première fois, sur chaque appareil, installez la CA du poste :\n"
            "          https://%s:%d%s\n"
            "          (iPad : Réglages › Profil téléchargé › Installer, puis Général ›\n"
            "           Informations › Réglages des certificats › activer)\n"
            "          empreinte SHA-256 : %s" % (hote_url(nom_ou_ip), port, CA, empreinte_ca()))


class Appairage:
    """Le code court. Deviner : un code ne vit que DUREE secondes, sert une fois,
    et ESSAIS erreurs le brûlent, avec PAUSE secondes avant le suivant. Chaque
    nouveau code s'affiche dans le terminal : une série d'essais se voit."""
    DUREE, ESSAIS, PAUSE, PAUSE_MAX = 600, 5, 60, 3600

    def __init__(self, afficher=None, nom=""):
        self.afficher = afficher or (lambda texte: print(texte, flush=True))
        self.nom = nom
        self.verrou = threading.Lock()
        self.code = None
        self.expire = 0.0
        self.echecs = 0
        self.bloque = 0.0
        self.grilles = 0                           # codes brûlés d'affilée

    @staticmethod
    def lisible(code):
        return code[:3] + " " + code[3:]

    def _neuf(self, maintenant):
        self.code = "%06d" % secrets.randbelow(10 ** 6)
        self.expire = maintenant + self.DUREE
        # echecs n'est PAS remis à zéro ici : sinon 4 essais par code, en attendant
        # chaque expiration, contournaient la pause (576 essais par jour)
        self.afficher("  Code d'appairage%s : %s   (à saisir sur la tablette, valable %d min)"
                      % (" " + self.nom if self.nom else "", self.lisible(self.code), self.DUREE // 60))

    def preparer(self):
        """Un code valable existe (en créer un, et l'afficher, s'il le faut).
        Renvoie les secondes d'attente si les essais sont suspendus, sinon 0."""
        with self.verrou:
            maintenant = time.monotonic()
            if maintenant < self.bloque:
                return int(self.bloque - maintenant) + 1
            if not self.code or maintenant >= self.expire:
                self._neuf(maintenant)
            return 0

    def essayer(self, saisi):
        """Renvoie (ok, message)."""
        saisi = "".join(c for c in str(saisi or "") if c.isdigit())
        with self.verrou:
            maintenant = time.monotonic()
            if maintenant < self.bloque:
                return False, ("Trop d'essais : attendez %d s, un nouveau code s'affichera "
                               "sur le serveur." % (int(self.bloque - maintenant) + 1))
            if not self.code or maintenant >= self.expire:
                self._neuf(maintenant)
                return False, "Code expiré : un nouveau code est affiché sur le serveur."
            if len(saisi) == 6 and hmac.compare_digest(saisi, self.code):
                self.code = None                   # usage unique
                self.grilles = 0
                self.echecs = 0
                return True, "Appareil autorisé."
            self.echecs += 1
            if self.echecs >= self.ESSAIS:
                # pause doublée à chaque code brûlé d'affilée (plafond 1 h) :
                # à pause fixe, 7 200 essais par jour suffisaient à deviner un
                # code de 6 chiffres en quelques semaines
                pause = min(self.PAUSE * 2 ** self.grilles, self.PAUSE_MAX)
                self.grilles += 1
                self.echecs = 0
                self.code = None
                self.bloque = maintenant + pause
                return False, ("Trop d'essais : code annulé. Dans %d s, rechargez la page : "
                               "un nouveau code s'affichera sur le serveur." % pause)
            return False, "Code incorrect (%d essai(s) restant(s))." % (self.ESSAIS - self.echecs)


class Visites:
    """Les appareils qui utilisent un serveur, affichés dans son terminal : une ligne
    quand une IP arrive, ou revient après INACTIF secondes sans requête."""
    INACTIF = 600

    def __init__(self, nom, afficher=None):
        self.nom = nom
        self.afficher = afficher or (lambda texte: print(texte, flush=True))
        self.verrou = threading.Lock()
        self.vues = {}                      # ip -> dernière requête (time.monotonic)

    def noter(self, ip):
        try:
            a = ipaddress.ip_address(str(ip).split("%")[0])
            ip = str(getattr(a, "ipv4_mapped", None) or a)
        except ValueError:
            pass
        maintenant = time.monotonic()
        with self.verrou:
            arrivee = maintenant - self.vues.get(ip, float("-inf")) > self.INACTIF
            self.vues = {i: t for i, t in self.vues.items() if maintenant - t <= self.INACTIF}
            self.vues[ip] = maintenant
            if not arrivee:
                return
            actifs = sorted(self.vues)
        self.afficher("  %s %s  %-11s %s  ->  %d appareil(s) actif(s) : %s"
                      % (MARQUE, time.strftime("%H:%M:%S"), self.nom, ip, len(actifs), ", ".join(actifs)))


class Garde:
    """Le contrôle d'accès d'un serveur, à appeler par filtrer() en tête de chaque requête.

    actif=False (le serveur n'écoute que sur 127.0.0.1) : seul l'en-tête Host est vérifié."""

    def __init__(self, nom, actif=True, afficher=None, ancien=None):
        self.nom = nom
        self.actif = actif
        self.jeton = jeton(ancien) if actif else None
        self.code = Appairage(afficher, nom) if actif else None
        self.visites = Visites(nom, afficher)
        self.tls = None                     # en mode réseau, après brancher()
        self.port = 0
        self.sans_tls = ""                  # … ou pourquoi tout reste en HTTP

    def brancher(self, serveur):
        """En mode réseau : HTTPS sur le port du serveur. Rend le schéma des adresses."""
        self.port = serveur.server_address[1]
        if self.actif:
            self.tls, self.sans_tls = contexte_tls()
            if self.tls:
                brancher_tls(serveur, self.tls)
        return self.schema()

    def schema(self):
        return "https" if self.tls else "http"

    def adresse(self, hote, port):
        return "%s://%s:%d/" % (self.schema(), hote_url(hote), port)

    def annoncer(self):
        """Au démarrage, en mode réseau : affiche le code dans le terminal."""
        if self.actif:
            if self.tls:
                hors = [i for i in adresses_poste() if not i.startswith(("127.", "100.", "fd7a:115c:a1e0:"))]
                print(ligne_https(hors[0] if hors else "127.0.0.1", self.port), flush=True)
            elif self.sans_tls:
                print("  HTTP en clair sur le réseau (%s) : le jeton peut être lu sur le Wi-Fi."
                      % self.sans_tls, flush=True)
            self.code.preparer()

    @staticmethod
    def internet(handler):
        """Le client a-t-il une adresse publique ? (IPv6 de la box ouverte, redirection de port)
        Réseau local, lien local et Tailscale (100.64/10, fd7a::) ne sont pas publics, ni une
        IPv6 globale du /64 de l'IPv6 globale du poste où elle arrive : un appareil du réseau
        local qui a trouvé pilou.local par mDNS (la box distribue ce /64 au seul réseau local)."""
        try:
            ip = ipaddress.ip_address(handler.client_address[0].split("%")[0])
        except (ValueError, IndexError, TypeError):
            return True
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if not ip.is_global:
            return False
        if ip.version != 6:
            return True
        try:
            ici = ipaddress.ip_address(handler.connection.getsockname()[0].split("%")[0])
        except (AttributeError, OSError, ValueError, IndexError, TypeError):
            return True
        return not (ici.version == 6 and ici.is_global and ici.ipv4_mapped is None
                    and ip in ipaddress.ip_network((ici, 64), strict=False))

    @staticmethod
    def ce_poste(handler):
        try:
            ip = ipaddress.ip_address(handler.client_address[0].split("%")[0])
        except (ValueError, IndexError, TypeError):
            return False
        return (getattr(ip, "ipv4_mapped", None) or ip).is_loopback

    def _jeton_url(self, handler):
        return urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query).get("jeton", [""])[0]

    def jeton_recu(self, handler):
        try:
            biscuit = SimpleCookie(handler.headers.get("Cookie") or "")
        except CookieError:
            biscuit = {}
        recus = [self._jeton_url(handler), biscuit[BISCUIT].value if BISCUIT in biscuit else ""]
        return any(r and hmac.compare_digest(r, self.jeton) for r in recus)

    def autorise(self, handler):
        return not self.actif or self.ce_poste(handler) or self.jeton_recu(handler)

    def biscuit(self, handler=None):
        # Secure en HTTPS : le navigateur ne le renverra plus jamais en clair
        return "%s=%s; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict%s" % (
            BISCUIT, self.jeton, "; Secure" if handler is not None and en_https(handler) else "")

    # -- réponses ---------------------------------------------------------
    @staticmethod
    def _repondre(handler, code, corps=b"", type_="text/plain; charset=utf-8", entetes=()):
        if isinstance(corps, str):
            corps = corps.encode("utf-8")
        handler.close_connection = True           # le corps d'une requête refusée n'est pas lu
        handler.send_response(code)
        for k, v in entetes:
            handler.send_header(k, v)
        handler.send_header("Content-Type", type_)
        handler.send_header("Content-Length", str(len(corps)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(corps)

    def _json(self, handler, code, donnees, entetes=()):
        self._repondre(handler, code, json.dumps(donnees, ensure_ascii=False),
                       "application/json; charset=utf-8", entetes)

    def filtrer(self, handler):
        """True : la requête suit son cours. False : la réponse est déjà envoyée."""
        if not hote_permis(handler.headers.get("Host")):
            self._repondre(handler, 403, "Host non autorisé (protection DNS rebinding)")
            return False
        if self.internet(handler):
            self._repondre(handler, 403, "Accès depuis Internet refusé : passez par le réseau local ou Tailscale")
            return False
        if not filtrer_tls(handler, self.ce_poste):
            return False
        if not self.actif:
            self.visites.noter(handler.client_address[0])
            return True
        u = urllib.parse.urlsplit(handler.path)
        lecture = handler.command in ("GET", "HEAD")
        if self.autorise(handler):
            self.visites.noter(handler.client_address[0])
            if lecture and self._jeton_url(handler):
                # Le jeton passe dans un cookie et quitte l'adresse : il ne reste
                # ni dans l'historique, ni dans un lien recopié.
                reste = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(u.query)
                                                if k != "jeton"])
                self._repondre(handler, 303, entetes=[("Set-Cookie", self.biscuit(handler)),
                                                      ("Location", u.path + ("?" + reste if reste else ""))])
                return False
            if lecture and u.path == PAGE:
                self._repondre(handler, 303, entetes=[("Location", "/")])
                return False
            if handler.command == "POST" and u.path == ROUTE:
                self._json(handler, 200, {"ok": True, "message": "Cet appareil est déjà autorisé."})
                return False
            return True
        if lecture and u.path == PAGE:
            self.code.preparer()
            self._repondre(handler, 200, PAGE_HTML.replace("__NOM__", _html(self.nom)),
                           "text/html; charset=utf-8")
            return False
        if handler.command == "POST" and u.path == ROUTE and handler.headers.get(ENTETE) == "1":
            try:
                n = int(handler.headers.get("Content-Length") or 0)
                corps = json.loads(handler.rfile.read(n) or b"{}") if 0 <= n <= 4096 else {}
            except (ValueError, OSError):
                corps = {}
            ok, texte = self.code.essayer(corps.get("code") if isinstance(corps, dict) else "")
            self._json(handler, 200 if ok else 403, {"ok": ok, "message": texte},
                       [("Set-Cookie", self.biscuit(handler))] if ok else ())
            return False
        accepte = handler.headers.get("Accept") or ""
        if lecture and (u.path in ("/", "/index.html") or "text/html" in accepte):
            # une page : vers celle du code (le code s'affiche dans le terminal du serveur)
            self._repondre(handler, 303, entetes=[("Location", PAGE)])
            return False
        self._json(handler, 403, {"detail": "Appareil non appairé : ouvrez la page de ce "
                                            "serveur et saisissez le code d'appairage."})
        return False


def _html(texte):
    return (str(texte).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


PAGE_HTML = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__NOM__ · Appairage</title>
<style>
  :root { --fond:#f4f6fa; --carte:#fff; --texte:#141a24; --doux:#5d6878; --trait:#d5dbe4;
          --accent:#2563eb; --ok:#15803d; --err:#b91c1c; }
  @media (prefers-color-scheme: dark) {
    :root { --fond:#0d1117; --carte:#161b22; --texte:#e6edf3; --doux:#8b96a5; --trait:#2b333e;
            --accent:#6ea8fe; --ok:#4ade80; --err:#f87171; }
  }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; min-height:100dvh; display:grid; place-items:center;
         padding:24px 16px; background:var(--fond); color:var(--texte);
         font:15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  main { width:100%; max-width:400px; padding:28px 24px; background:var(--carte);
         border:1px solid var(--trait); border-radius:14px; text-align:center; }
  .nom { font-weight:600; letter-spacing:.08em; color:var(--doux); font-size:13px; }
  h1 { font-size:20px; margin:10px 0 6px; }
  p { color:var(--doux); margin:0 0 20px; font-size:14px; }
  input { width:100%; padding:14px 10px; border-radius:10px; border:1px solid var(--trait);
          background:var(--fond); color:var(--texte); text-align:center; outline:none;
          font:600 30px/1 ui-monospace, Menlo, Consolas, monospace; letter-spacing:.3em; }
  input:focus { border-color:var(--accent); }
  button { margin-top:14px; width:100%; padding:12px; border-radius:10px; border:0;
           background:var(--accent); color:#fff; font:600 15px/1 inherit; cursor:pointer; }
  button:disabled { opacity:.5; cursor:default; }
  #etat { min-height:1.5em; margin-top:14px; font-size:14px; }
  #etat.err { color:var(--err); } #etat.ok { color:var(--ok); }
</style>
</head>
<body>
<main>
  <div class="nom">__NOM__</div>
  <h1>Code d'appairage</h1>
  <p>Saisissez le code à 6 chiffres affiché dans le terminal du serveur (ou dans les logs
     de ce tool, dans WEB·SUITE). Cet appareil s'en souviendra ensuite,
     pour tous les web tools de ce serveur.</p>
  <form id="f" autocomplete="off">
    <input id="code" inputmode="numeric" pattern="[0-9 ]*" maxlength="7"
           autocomplete="one-time-code" placeholder="000 000" aria-label="Code d'appairage" autofocus>
    <button id="ok" type="submit">Valider</button>
  </form>
  <div id="etat" role="status"></div>
  <p id="ca" hidden>Connexion chiffrée. Le navigateur vous a averti ? Installez une fois
     <a href="/appairage/ca.pem">le certificat de ce poste</a> (iPad : Réglages › Profil
     téléchargé, puis Général › Informations › Réglages des certificats). Comparez son
     empreinte SHA-256 avec celle du terminal.</p>
</main>
<script>
const champ = document.getElementById('code'), etat = document.getElementById('etat');
const bouton = document.getElementById('ok');
let envoi = false;
if (location.protocol === 'https:') document.getElementById('ca').hidden = false;
function dire(t, c) { etat.textContent = t; etat.className = c || ''; }
async function valider() {
  const code = champ.value.replace(/\\D/g, '');
  if (code.length !== 6) { dire('Le code a 6 chiffres.', 'err'); return; }
  if (envoi) return;
  envoi = true; bouton.disabled = true; dire('Vérification…');
  try {
    const r = await fetch('/api/appairer', { method: 'POST', credentials: 'same-origin',
      headers: { 'X-Appairage': '1', 'Content-Type': 'application/json' }, body: JSON.stringify({ code }) });
    const d = await r.json().catch(() => ({}));
    if (d.ok) { dire('Appareil autorisé.', 'ok'); location.replace('/'); return; }
    dire(d.message || ('Refusé (' + r.status + ').'), 'err'); champ.select();
  } catch (e) { dire(location.protocol === 'https:' ? 'Envoi bloqué : cet appareil ne fait sans doute pas encore confiance au certificat de ce poste (ou le serveur est arrêté). Installez le certificat (lien ci-dessous), puis rechargez la page.' : 'Serveur injoignable : ' + e.message, 'err'); }
  envoi = false; bouton.disabled = false;
}
// Déjà autorisé ? Le cookie SameSite=Strict n'accompagne pas une arrivée depuis un autre site
// (launcher en HTTP, autre nom d'hôte, lien d'une autre appli) : sans ce contrôle, il fallait
// recharger la page. Refait au retour sur l'onglet (code validé ailleurs, page sortie du cache).
function deja() {
  fetch('/appairage', { credentials: 'same-origin', cache: 'no-store', redirect: 'manual' })
    .then(r => { if (r.type === 'opaqueredirect') location.replace('/'); }).catch(() => {});
}
addEventListener('pageshow', deja);
document.addEventListener('visibilitychange', () => { if (!document.hidden) deja(); });
document.getElementById('f').addEventListener('submit', e => { e.preventDefault(); valider(); });
champ.addEventListener('input', () => {
  const c = champ.value.replace(/\\D/g, '').slice(0, 6);
  champ.value = c.length > 3 ? c.slice(0, 3) + ' ' + c.slice(3) : c;
  if (c.length === 6) valider();
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    # Vérification : un vrai serveur sur la boucle locale ; « l'autre appareil » est
    # simulé en faisant croire au garde que la requête ne vient pas de ce poste.
    import http.client
    import http.server
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["WEB_TOOLS_JETON_FICHIER"] = os.path.join(tmp, "partage", "jeton.txt")
        ancien = os.path.join(tmp, "ancien.txt")
        with open(ancien, "w") as f:
            f.write("a" * 22 + "\n")
        assert jeton(ancien) == "a" * 22                         # l'ancien jeton est repris
        assert jeton() == "a" * 22                               # puis partagé
        os.remove(os.environ["WEB_TOOLS_JETON_FICHIER"])
        j = jeton()
        assert len(j) >= 16 and j != "a" * 22 and jeton() == j
        with open(os.environ["WEB_TOOLS_JETON_FICHIER"], "w") as f:
            f.write("court")                                      # fichier abîmé : remplacé et gardé
        j = jeton()
        assert len(j) >= 16 and jeton() == j and _lire(os.environ["WEB_TOOLS_JETON_FICHIER"]) == j
        if os.name != "nt":
            assert os.stat(os.environ["WEB_TOOLS_JETON_FICHIER"]).st_mode & 0o777 == 0o600
        os.environ["WEB_TOOLS_JETON_FICHIER"] = os.path.join(ancien, "impossible", "jeton.txt")
        assert len(jeton()) >= 16                                 # pas de plantage sans dossier inscriptible
        os.environ["WEB_TOOLS_JETON_FICHIER"] = os.path.join(tmp, "partage", "jeton.txt")

        affiches = []
        g = Garde("TEST", afficher=affiches.append)
        distant = [True]
        g.ce_poste = lambda h: not distant[0]

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def parse_request(self):
                return super().parse_request() and g.filtrer(self)

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
            do_POST = do_GET

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]

        def req(chemin, methode="GET", entetes=None, corps=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request(methode, chemin, body=corps, headers=entetes or {})
            r = c.getresponse()
            r.corps = r.read()
            c.close()
            return r

        def appairer(code, x=True, biscuit=None):
            e = {"Content-Type": "application/json"}
            if x:
                e[ENTETE] = "1"
            if biscuit:
                e["Cookie"] = biscuit
            return req(ROUTE, "POST", e, json.dumps({"code": code}))

        r = req("/")
        assert (r.status, r.getheader("Location")) == (303, PAGE)
        assert req("/app.js").status == 403
        assert req("/app.js", entetes={"Accept": "text/html"}).getheader("Location") == PAGE
        assert req("/api/x", "POST").status == 403
        assert req("/", entetes={"Host": "piege.example:80"}).status == 403
        vrai_nom, nom_tailscale = nom_tailscale, lambda: "pi.tail1.ts.net"
        assert hote_permis("PI.tail1.ts.net:8000") and not hote_permis("autre.tail1.ts.net:8000")
        nom_tailscale = lambda: ""                               # sans Tailscale : aucun nom en .ts.net
        assert not hote_permis("pi.tail1.ts.net")
        nom_tailscale = vrai_nom
        r = req(PAGE)
        assert r.status == 200 and b"/api/appairer" in r.corps and b"TEST" in r.corps
        assert b"opaqueredirect" in r.corps                       # déjà autorisé : la page repart seule
        code = g.code.code
        assert affiches and Appairage.lisible(code) in affiches[-1]
        assert appairer(code, x=False).status == 403              # sans en-tête : refusé
        assert g.code.code == code                                # … et sans brûler d'essai
        faux = "000000" if code != "000000" else "111111"
        assert appairer(faux).status == 403
        r = appairer(code[:3] + " " + code[3:])                   # tel qu'affiché
        assert r.status == 200 and json.loads(r.corps)["ok"]
        biscuit = r.getheader("Set-Cookie").split(";")[0]
        assert biscuit == BISCUIT + "=" + j
        assert appairer(code).status == 403                       # usage unique
        assert not any(MARQUE in a for a in affiches)            # non appairé : pas un utilisateur
        assert req("/", entetes={"Cookie": biscuit}).corps == b"ok"
        assert MARQUE in affiches[-1] and "127.0.0.1" in affiches[-1]
        assert req("/app.js", entetes={"Cookie": biscuit}).corps == b"ok"
        assert req("/api/x", "POST", {"Cookie": biscuit}).corps == b"ok"
        assert req(PAGE, entetes={"Cookie": biscuit}).getheader("Location") == "/"
        assert json.loads(appairer("x", biscuit=biscuit).corps)["ok"]
        assert req("/", entetes={"Cookie": BISCUIT + "=faux"}).status == 303
        assert req("/", entetes={"Cookie": '";;=='}).status == 303
        r = req("/outil?a=1&jeton=" + j)                          # adresse avec ?jeton=
        assert r.status == 303 and r.getheader("Location") == "/outil?a=1"
        assert r.getheader("Set-Cookie").startswith(BISCUIT + "=" + j)
        assert req("/?jeton=faux").getheader("Location") == PAGE
        # cinq erreurs : code brûlé, pause
        req(PAGE)
        code = g.code.code
        for _ in range(Appairage.ESSAIS):
            appairer("000000" if code != "000000" else "111111")
        assert appairer(code).status == 403
        assert "attendez" in json.loads(appairer(code).corps)["message"]
        g.code.bloque = 0
        req(PAGE)
        assert appairer(g.code.code).status == 200
        assert masquer('"GET /?a=1&jeton=%s HTTP/1.1" 303' % j) == '"GET /?a=1&jeton=*** HTTP/1.1" 303'
        # l'expiration d'un code ne remet pas les erreurs à zéro
        a = Appairage(afficher=lambda t: None)
        a.preparer()
        for _ in range(a.ESSAIS - 1):
            a.essayer("x")
        a.expire = 0
        a.essayer("x")                                            # code expiré : un nouveau
        assert "annulé" in a.essayer("x")[1]                      # 5e erreur : brûlé
        # adresses publiques refusées, réseau local et Tailscale acceptés
        class C:
            def __init__(self, ip):
                self.client_address = (ip, 0)
        for ip in ("192.168.1.20", "10.0.0.2", "100.80.111.121", "fd7a:115c:a1e0::1", "fe80::1%eth0",
                   "127.0.0.1", "::ffff:192.168.1.20"):
            assert not Garde.internet(C(ip)), ip
        for ip in ("8.8.8.8", "2a01:cb19:690:a100::1", "::ffff:8.8.8.8"):
            assert Garde.internet(C(ip)), ip
        # IPv6 globale du /64 du poste (mDNS pilou.local) : réseau local ; ailleurs, Internet
        class S:
            def __init__(self, ici):
                self.getsockname = lambda: (ici, 0, 0, 0)
        class CS(C):
            def __init__(self, ip, ici):
                super().__init__(ip)
                self.connection = S(ici)
        assert not Garde.internet(CS("2a01:cb19:690:a100::1", "2a01:cb19:690:a100:abcd::2"))
        assert not Garde.internet(CS("2a01:cb19:690:a100::1", "2a01:cb19:690:a100:abcd::2%eth0"))
        for ip, ici in (("2a01:cb19:690:a101::1", "2a01:cb19:690:a100::2"),   # autre /64
                        ("2a01:cb19:690:a100::1", "fd00::2"),                 # arrivée par une ULA
                        ("2a01:cb19:690:a100::1", "::ffff:192.168.1.2"),
                        ("2a01:cb19:690:a100::1", "pas une ip"),
                        ("8.8.8.8", "8.8.8.1"),                               # IPv4 globale : toujours refusée
                        ("::ffff:8.8.8.8", "::ffff:8.8.8.1")):
            assert Garde.internet(CS(ip, ici)), (ip, ici)
        distant[0] = False                                        # ce poste : pas de code
        assert req("/").corps == b"ok"
        inactif = Garde("LOCAL", actif=False)
        assert inactif.jeton is None
        lignes = []
        v = Visites("TEST", afficher=lignes.append)
        v.noter("192.168.1.20")
        v.noter("192.168.1.20")                                   # déjà vu : rien
        v.noter("::ffff:192.168.1.21")
        assert len(lignes) == 2 and "2 appareil(s) actif(s) : 192.168.1.20, 192.168.1.21" in lignes[1]
        v.vues["192.168.1.20"] -= Visites.INACTIF + 1              # parti depuis 10 min
        v.noter("192.168.1.21")
        assert len(lignes) == 2
        v.noter("192.168.1.20")                                   # revenu : affiché de nouveau
        assert len(lignes) == 3 and "2 appareil(s)" in lignes[2]
        srv.shutdown()
        srv.server_close()

        # HTTPS : la CA et le certificat créés une fois, gardés ensuite
        os.environ["WEB_TOOLS_HTTP"] = "1"
        assert contexte_tls() == (None, "WEB_TOOLS_HTTP=1")
        del os.environ["WEB_TOOLS_HTTP"]
        if _openssl():
            ctx, raison = contexte_tls()
            assert ctx and raison is None, raison
            d = dossier_tls()
            date_ca = os.path.getmtime(os.path.join(d, "ca.pem"))
            assert not _a_refaire(d, adresses_poste()) and contexte_tls()[0]
            assert os.path.getmtime(os.path.join(d, "ca.pem")) == date_ca
            assert len(empreinte_ca().split()) == 32
            if os.name == "posix":
                assert os.stat(os.path.join(d, "ca.key")).st_mode & 0o077 == 0
            # adresse nouvelle : le certificat est refait, la CA reste
            with open(os.path.join(d, "serveur.json"), encoding="utf-8") as f:
                info = json.load(f)
            info["adresses"] = ["127.0.0.1"]
            _cnf(os.path.join(d, "serveur.json"), json.dumps(info))
            assert _a_refaire(d, adresses_poste() + ["10.9.9.9"])
            g2 = Garde("TLS", afficher=lambda t: None)
            g2.ce_poste = lambda h: not distant[0]
            distant[0] = True

            class H2(H):
                def parse_request(self):
                    return http.server.BaseHTTPRequestHandler.parse_request(self) and g2.filtrer(self)
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H2)
            assert g2.brancher(srv) == "https" and g2.adresse("h", 1) == "https://h:1/"
            assert os.path.getmtime(os.path.join(d, "ca.pem")) == date_ca
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            port = srv.server_address[1]
            client = ssl.create_default_context(cafile=os.path.join(d, "ca.pem"))

            def reqs(chemin, methode="GET", entetes=None, corps=None):
                c = http.client.HTTPSConnection("127.0.0.1", port, timeout=5, context=client)
                c.request(methode, chemin, body=corps, headers=entetes or {})
                r = c.getresponse()
                r.corps = r.read()
                c.close()
                return r
            r = req("/outil?a=1")                                 # autre appareil en HTTP
            assert r.status == 307 and r.getheader("Location") == "https://127.0.0.1:%d/outil?a=1" % port
            r = req(CA)                                           # la CA, même en HTTP, sans jeton
            assert r.status == 200 and r.corps.startswith(b"-----BEGIN CERTIFICATE")
            r = reqs("/")                                         # certificat vérifié par la CA
            assert (r.status, r.getheader("Location")) == (303, PAGE)
            assert b'id="ca"' in reqs(PAGE).corps
            r = reqs("/?jeton=" + j)
            assert "; Secure" in r.getheader("Set-Cookie")
            assert reqs("/", entetes={"Cookie": BISCUIT + "=" + j}).corps == b"ok"
            distant[0] = False                                    # ce poste : HTTP reste ouvert
            assert req("/").corps == b"ok" and reqs("/").corps == b"ok"
            c = socket.create_connection(("127.0.0.1", port), timeout=5)
            c.close()                                             # client muet : rien ne casse
            assert req("/").corps == b"ok"
            srv.shutdown()
            srv.server_close()
            # IPv6 : les contraintes de la CA couvrent Tailscale et les ULA, pas Internet
            assert g2.adresse("fd7a::1", 1) == "https://[fd7a::1]:1/"
            assert "https://[fd7a::1]:8080" + CA in ligne_https("fd7a::1", 8080)

            def signe(adresses):
                """Le certificat du serveur pour ces adresses passe-t-il les contraintes de la CA ?"""
                _generer(d, _openssl(), adresses, False)
                return subprocess.run([_openssl(), "verify", "-CAfile", "ca.pem", "serveur.pem"],
                                      cwd=d, capture_output=True).returncode == 0
            r = subprocess.run([_openssl(), "x509", "-in", "ca.pem", "-noout", "-text"],
                               cwd=d, capture_output=True, text=True)
            assert "IP:FC00:0:0:0:0:0:0:0/FE00:0:0:0:0:0:0:0" in r.stdout.upper(), r.stdout
            assert signe(["127.0.0.1", "fd7a:115c:a1e0::1", "fd12:3456::1"])
            assert not signe(["2a01:cb19:690:a100::1"]) and not signe(["fe80::1"])
            # une CA d'avant IPv6 (sans ca.json) refuse fd7a:… : elle est refaite, une fois
            PLAGES, plages = PLAGES[:5], PLAGES
            _generer(d, _openssl(), ["127.0.0.1"], True)
            PLAGES = plages
            os.remove(os.path.join(d, "ca.json"))
            assert not signe(["fd7a:115c:a1e0::1"])
            vieille = empreinte_ca()
            assert _a_refaire(d, ["127.0.0.1"]) and contexte_tls()[0]
            assert empreinte_ca() != vieille and not _a_refaire(d, adresses_poste())
            assert signe(["fd7a:115c:a1e0::1"])
        else:
            print("(openssl absent : HTTPS non vérifié)")
    print("OK")
