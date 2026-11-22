//! Le serveur : sert la page (web/) et noVNC, l'API du jeu, et trois WebSocket :
//! /vnc relaie l'écran vers noVNC, /input transforme les gestes en souris et clavier VNC,
//! /son envoie le son du jeu. En mode réseau, l'appairage des web tools (web_tools_commun)
//! protège l'accès.
//!
//! Lancement :  theme_h            (puis ouvrir l'adresse affichée)
//! Options   :  --port 8791  --lan  --local  --resolution 1024x768  --sans-jeu  --affichage 99  --vnc-port 5999

use std::fs;
use std::io::{self, Read, Write};
use std::net::{Shutdown, TcpStream};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{sync_channel, TryRecvError};
use std::sync::{Arc, OnceLock};
use std::time::Duration;

use serde_json::Value;
use web_tools_commun::appairage::{self, Garde};
use web_tools_commun::production;
use web_tools_commun::serveur::{decoder_chemin, Duplex, Reponse, Requete, Serveur, Service};

use crate::{commande_jeu, dans_le_path, prendre, trouver_novnc, Jeu};

pub const PORT: u16 = 8791;
const DEPOT: &str = "https://github.com/pilou33620/web_theme_H";

/// La racine de l'outil (web/, theme_hospital/, donnees/) : le dossier courant s'il contient
/// web/index.html, sinon le premier dossier au-dessus de l'exécutable qui le contient.
pub fn racine() -> &'static Path {
    static R: OnceLock<PathBuf> = OnceLock::new();
    R.get_or_init(|| {
        let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
        let outil = |d: &Path| d.join("web").join("index.html").is_file();
        if outil(&cwd) {
            return cwd;
        }
        let exe = std::env::current_exe().ok().and_then(|e| fs::canonicalize(e).ok());
        exe.and_then(|e| e.ancestors().find(|d| outil(d)).map(Path::to_path_buf)).unwrap_or(cwd)
    })
}

/// Types servis : tout autre fichier est refusé.
fn type_mime(p: &Path) -> Option<&'static str> {
    Some(match p.extension()?.to_str()? {
        "html" => "text/html; charset=utf-8",
        "js" | "mjs" => "text/javascript; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "json" => "application/json",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "ico" => "image/x-icon",
        "gif" => "image/gif",
        "jpg" | "jpeg" => "image/jpeg",
        "webp" => "image/webp",
        "woff" => "font/woff",
        "woff2" => "font/woff2",
        "oga" | "ogg" => "audio/ogg",
        "mp3" => "audio/mpeg",
        "wav" => "audio/wav",
        "wasm" => "application/wasm",
        _ => return None,
    })
}

/// Fichier de `base` demandé par `chemin`, jamais en dehors de `base` (ni fichier caché).
pub fn fichier_sous(base: &Path, chemin: &str) -> Option<PathBuf> {
    let rel = decoder_chemin(chemin.trim_start_matches('/'));
    let p = Path::new(&rel);
    if p.components().any(|c| !matches!(c, Component::Normal(n) if !n.to_string_lossy().starts_with('.'))) {
        return None;
    }
    let base = fs::canonicalize(base).ok()?;
    let cible = fs::canonicalize(base.join(p)).ok()?;
    (cible.starts_with(&base) && cible.is_file()).then_some(cible)
}

// ----------------------------------------------------------------- WebSocket (RFC 6455, trames)

/// Les messages qui arrivent, découpés au fil des lectures : avec un délai court, une lecture
/// peut s'arrêter au milieu d'une trame.
#[derive(Default)]
struct Trames {
    tampon: Vec<u8>,
    message: Vec<u8>,
    op: u8,
}

impl Trames {
    /// Une lecture (bornée par le délai de la connexion) ; rend les messages complets (opcode, octets).
    /// Err : la connexion est finie (fermée, trame de fermeture, trame de plus de 8 Mo).
    fn lire(&mut self, c: &mut dyn Duplex) -> Result<Vec<(u8, Vec<u8>)>, ()> {
        let mut bloc = vec![0u8; 65536];
        match c.read(&mut bloc) {
            Ok(0) => return Err(()),
            Ok(n) => self.tampon.extend_from_slice(&bloc[..n]),
            Err(e) if matches!(e.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut | io::ErrorKind::Interrupted) => {}
            Err(_) => return Err(()),
        }
        let mut messages = Vec::new();
        loop {
            let t = &self.tampon;
            if t.len() < 2 {
                break;
            }
            let (fin, op, masque) = (t[0] & 0x80 != 0, t[0] & 0x0F, t[1] & 0x80 != 0);
            let (mut n, mut tete) = ((t[1] & 0x7F) as u64, 2usize);
            if n == 126 {
                if t.len() < 4 {
                    break;
                }
                (n, tete) = (u16::from_be_bytes([t[2], t[3]]) as u64, 4);
            } else if n == 127 {
                if t.len() < 10 {
                    break;
                }
                let mut o = [0u8; 8];
                o.copy_from_slice(&t[2..10]);
                (n, tete) = (u64::from_be_bytes(o), 10);
            }
            if n > 8 << 20 {
                return Err(());
            }
            let debut = tete + if masque { 4 } else { 0 };
            let total = debut + n as usize;
            if t.len() < total {
                break;
            }
            let mut d = t[debut..total].to_vec();
            if masque {
                let cle = [t[tete], t[tete + 1], t[tete + 2], t[tete + 3]];
                for (i, o) in d.iter_mut().enumerate() {
                    *o ^= cle[i % 4];
                }
            }
            self.tampon.drain(..total);
            match op {
                8 => return Err(()),
                9 => envoyer(c, 10, &d).map_err(|_| ())?,
                10 => {}
                1 | 2 => (self.op, self.message) = (op, d),
                _ => self.message.extend_from_slice(&d),
            }
            if fin && matches!(op, 0..=2) {
                messages.push((self.op, std::mem::take(&mut self.message)));
            }
        }
        Ok(messages)
    }
}

fn envoyer(c: &mut dyn Duplex, op: u8, d: &[u8]) -> io::Result<()> {
    let n = d.len();
    let mut t = vec![0x80 | op];
    if n < 126 {
        t.push(n as u8);
    } else if n < 65536 {
        t.push(126);
        t.extend((n as u16).to_be_bytes());
    } else {
        t.push(127);
        t.extend((n as u64).to_be_bytes());
    }
    t.extend_from_slice(d);
    c.write_all(&t)?;
    c.flush()
}

/// L'écran : octets bruts entre noVNC et x11vnc, dans les deux sens. Un seul fil écrit et lit
/// la page (en TLS, la connexion ne se partage pas) ; un second lit x11vnc.
fn relais_vnc(c: &mut dyn Duplex, jeu: &Jeu) {
    let Ok(mut tcp) = TcpStream::connect(("127.0.0.1", jeu.port_vnc)) else { return };
    let Ok(mut lecture) = tcp.try_clone() else { return };
    let (tx, rx) = sync_channel::<Vec<u8>>(64);
    std::thread::spawn(move || {
        let mut b = vec![0u8; 65536];
        while let Ok(n) = lecture.read(&mut b) {
            if n == 0 || tx.send(b[..n].to_vec()).is_err() {
                break;
            }
        }
    });
    let _ = c.delai_lecture(Some(Duration::from_millis(10)));
    let mut trames = Trames::default();
    'relais: while let Ok(messages) = trames.lire(c) {
        for (_, d) in messages {
            if tcp.write_all(&d).is_err() {
                break 'relais;
            }
        }
        loop {
            match rx.try_recv() {
                Ok(d) => {
                    if envoyer(c, 2, &d).is_err() {
                        break 'relais;
                    }
                }
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => break 'relais,
            }
        }
    }
    let _ = tcp.shutdown(Shutdown::Both);
}

/// Le son : la page n'envoie rien, la lecture ne sert qu'à voir la fermeture.
fn relais_son(c: &mut dyn Duplex, jeu: &Jeu) {
    static NUMERO: AtomicU64 = AtomicU64::new(0);
    let numero = NUMERO.fetch_add(1, Ordering::SeqCst);
    let (tx, rx) = sync_channel::<Vec<u8>>(20); // au plus ~1 s de son en attente
    prendre(&jeu.auditeurs).push((numero, tx));
    let _ = c.delai_lecture(Some(Duration::from_millis(10)));
    let mut trames = Trames::default();
    'relais: while trames.lire(c).is_ok() {
        while let Ok(d) = rx.try_recv() {
            if envoyer(c, 2, &d).is_err() {
                break 'relais;
            }
        }
    }
    prendre(&jeu.auditeurs).retain(|(n, _)| *n != numero);
}

/// Les gestes : un message JSON par geste, appliqué au jeu.
fn relais_entree(c: &mut dyn Duplex, jeu: &Jeu) {
    let _ = c.delai_lecture(Some(Duration::from_secs(1))); // une page sans geste n'est pas une page partie
    let mut trames = Trames::default();
    while let Ok(messages) = trames.lire(c) {
        for (_, d) in messages {
            if let Ok(msg @ Value::Object(_)) = serde_json::from_slice::<Value>(&d) {
                jeu.saisir(&msg);
            }
        }
    }
}

// ----------------------------------------------------------------- serveur HTTP

pub static GARDE: OnceLock<Garde> = OnceLock::new();

fn garde() -> &'static Garde {
    GARDE.get().expect("garde non initialisée")
}

/// Le service, à donner au serveur (GARDE doit être posée avant la première requête).
pub fn service(jeu: Arc<Jeu>) -> Arc<Service> {
    Arc::new(move |r: &mut Requete| {
        traiter(r, &jeu).entete("X-Content-Type-Options", "nosniff").entete("Content-Security-Policy", "frame-ancestors 'self'")
    })
}

fn traiter(r: &mut Requete, jeu: &Arc<Jeu>) -> Reponse {
    // Host, Internet, TLS, jeton : avant tout
    if let Some(rep) = garde().filtrer(r) {
        return rep;
    }
    let chemin = r.chemin().to_string();
    if r.entete("Upgrade").is_some_and(|u| u.eq_ignore_ascii_case("websocket")) {
        if !["/vnc", "/input", "/son"].contains(&chemin.as_str()) || r.entete("Sec-WebSocket-Key").is_none() {
            return Reponse::texte(404, "Introuvable");
        }
        if jeu.etat() != "en_marche" {
            return Reponse::texte(503, "Le jeu n'est pas démarré");
        }
        let jeu = jeu.clone();
        // la caisse refuse un Origin d'un autre site (un WebSocket ignore la règle de même origine)
        return r.websocket(&["binary"], move |c| match chemin.as_str() {
            "/vnc" => relais_vnc(c, &jeu),
            "/son" => relais_son(c, &jeu),
            _ => relais_entree(c, &jeu),
        });
    }
    if chemin.starts_with("/api/") {
        if r.methode == "POST" {
            if r.entete("X-Theme") != Some("1") {
                return Reponse::texte(403, "Requête refusée"); // un autre site ne peut pas l'ajouter
            }
            match r.lire_corps(4096) {
                Ok(c) if c.is_empty() || matches!(serde_json::from_slice::<Value>(&c), Ok(Value::Object(_))) => {}
                Ok(_) => return Reponse::texte(400, "JSON invalide"),
                Err(_) => return Reponse::texte(413, "Requête trop grande"),
            }
        }
        return match (r.methode.as_str(), chemin.as_str()) {
            ("GET", "/api/status") => Reponse::json(200, &jeu.statut()),
            ("POST", "/api/demarrer" | "/api/arreter" | "/api/redemarrer") => {
                let (j, quoi) = (jeu.clone(), chemin.clone());
                std::thread::spawn(move || match quoi.as_str() {
                    "/api/demarrer" => j.demarrer(),
                    "/api/arreter" => j.arreter(),
                    _ => j.redemarrer(),
                });
                std::thread::sleep(Duration::from_millis(50));
                Reponse::json(202, &jeu.statut())
            }
            _ => Reponse::texte(404, "Introuvable"),
        };
    }
    if r.methode != "GET" {
        return Reponse::texte(405, "Méthode non permise");
    }
    let (cible, cache) = match chemin.strip_prefix("/novnc/") {
        Some(reste) => (trouver_novnc(&jeu.racine).and_then(|b| fichier_sous(&b, reste)), "max-age=86400"),
        None => (fichier_sous(&jeu.racine.join("web"), if chemin == "/" { "index.html" } else { chemin.as_str() }), "no-cache"),
    };
    let Some((cible, type_)) = cible.and_then(|c| type_mime(&c).map(|t| (c, t))) else {
        return Reponse::texte(404, "Introuvable");
    };
    match fs::read(&cible) {
        Ok(corps) => Reponse { code: 200, entetes: vec![("Content-Type".into(), type_.into()), ("Cache-Control".into(), cache.into())], corps, suite: None },
        Err(_) => Reponse::texte(404, "Introuvable"),
    }
}

// ----------------------------------------------------------------- démarrage

#[derive(Debug, PartialEq)]
pub struct Args {
    pub port: u16,
    pub lan: bool,
    pub local: bool,
    pub largeur: u32,
    pub hauteur: u32,
    pub affichage: u32,
    pub vnc_port: u16,
    pub sans_jeu: bool,
}

pub const USAGE: &str = "usage: theme_h [--port 8791] [--lan | --local] [--resolution 1024x768] [--affichage 99] \
                         [--vnc-port 5999] [--sans-jeu] [--sans-navigateur] [--sans-pause] [--sans-maj]";

/// « 1024x768 » : largeur et hauteur de l'écran du jeu.
pub fn resolution(v: &str) -> Option<(u32, u32)> {
    let v = v.to_lowercase();
    let (l, h) = v.split_once('x')?;
    let (l, h): (u32, u32) = (l.trim().parse().ok()?, h.trim().parse().ok()?);
    ((1..=8192).contains(&l) && (1..=8192).contains(&h)).then_some((l, h))
}

fn nombre<T: std::str::FromStr>(v: &str, quoi: &str) -> Result<T, String> {
    v.trim().parse().map_err(|_| format!("{quoi} invalide : {v}"))
}

pub fn lire_args(argv: &[String]) -> Result<Args, String> {
    let mut a = Args { port: PORT, lan: false, local: false, largeur: 1024, hauteur: 768, affichage: 99, vnc_port: 5999, sans_jeu: false };
    let mut i = 0;
    while i < argv.len() {
        let x = argv[i].as_str();
        let (cle, val) = match x.split_once('=') {
            Some((k, v)) => (k, Some(v.to_string())),
            None => (x, None),
        };
        let mut valeur = || -> Result<String, String> {
            match &val {
                Some(v) => Ok(v.clone()),
                None => {
                    i += 1;
                    argv.get(i).cloned().ok_or(format!("{cle} attend une valeur"))
                }
            }
        };
        match cle {
            "--port" => a.port = nombre(&valeur()?, "port")?,
            "--vnc-port" => a.vnc_port = nombre(&valeur()?, "port VNC")?,
            "--affichage" => a.affichage = nombre(&valeur()?, "affichage")?,
            "--resolution" => {
                let v = valeur()?;
                (a.largeur, a.hauteur) = resolution(&v).ok_or(format!("résolution invalide : {v} (exemple : 1024x768)"))?;
            }
            "--lan" | "--local" | "--sans-jeu" | "--sans-navigateur" | "--sans-pause" | "--sans-maj" if val.is_some() => {
                return Err(format!("{cle} ne prend pas de valeur"))
            }
            "--lan" => a.lan = true, // ouvert au réseau local : c'est déjà le défaut
            "--local" => a.local = true,
            "--sans-jeu" => a.sans_jeu = true,
            // sans effet : ce serveur n'ouvre pas de navigateur et ne pose pas de question ;
            // --sans-maj est lu par maj_production (copie de production)
            "--sans-navigateur" | "--sans-pause" | "--sans-maj" => {}
            _ => return Err(format!("option inconnue : {x}")),
        }
        i += 1;
    }
    Ok(a)
}

/// Copie de production (archive theme_h-prod, sans git) : mise à jour depuis la publication
/// « binaires » (§4.1 bis des règles). Sauf --sans-maj, et sauf à la relance qui suit une mise à jour.
fn maj_production(argv: &[String], racine: &Path) {
    let sans_maj = argv.iter().any(|x| x == "--sans-maj");
    if sans_maj || std::env::var_os("THEME_H_DEJA_MAJ").is_some() || !production::est_copie_prod(racine) {
        return;
    }
    match production::mettre_a_jour(DEPOT, "theme_h", racine, &|t| println!("{t}")) {
        // demarrer_WEB_THEME_H relance la nouvelle version ; lancé sans lui, on continue avec celle-ci
        Ok(true) if std::env::var_os("THEME_H_RELANCEUR").is_some() => std::process::exit(production::CODE_MIS_A_JOUR),
        Ok(true) => println!("Mis à jour : relancez le serveur pour en profiter."),
        Ok(false) => {}
        Err(e) => println!("[X] {e}"), // l'installation actuelle est intacte : on démarre
    }
}

fn verifier_dependances(racine: &Path) {
    let manque: Vec<&str> = ["Xvfb", "x11vnc"].into_iter().filter(|n| dans_le_path(n).is_none()).collect();
    if !manque.is_empty() {
        println!("!!! Programmes manquants : {} (sudo apt install xvfb x11vnc)", manque.join(", "));
    }
    if trouver_novnc(racine).is_none() {
        println!("!!! noVNC introuvable (sudo apt install novnc)");
    }
    if commande_jeu(racine).is_none() {
        println!("!!! CorsixTH introuvable (sudo apt install corsix-th, ou sudo snap install corsixth)");
    }
}

pub fn main() {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let args = match lire_args(&argv) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("{USAGE}\ntheme_h: {e}");
            std::process::exit(2);
        }
    };
    let racine = racine().to_path_buf();
    let _ = std::env::set_current_dir(&racine);
    maj_production(&argv, &racine);
    let reseau = !args.local;
    let srv = match Serveur::ouvrir(args.port, reseau, 1) {
        Ok(s) => s,
        Err(e) => {
            println!("Port {} indisponible : {}", args.port, e);
            std::process::exit(1);
        }
    };
    let jeu = Jeu::new(args.largeur, args.hauteur, args.affichage, args.vnc_port, racine.clone());
    let _ = GARDE.set(Garde::new("Theme Hospital", reseau, None, None));
    garde().brancher(&srv);
    // SIGTERM (launcher) ou Ctrl+C : le jeu et son écran s'arrêtent avec le serveur
    match signal_hook::iterator::Signals::new([signal_hook::consts::SIGINT, signal_hook::consts::SIGTERM]) {
        Ok(mut signaux) => {
            let j = jeu.clone();
            std::thread::spawn(move || {
                if signaux.forever().next().is_some() {
                    j.arreter();
                    std::process::exit(0);
                }
            });
        }
        Err(e) => println!("!!! Signaux non attrapés ({e}) : arrêter le serveur peut laisser le jeu tourner."),
    }
    verifier_dependances(&racine);
    let port = srv.port();
    let urls: Vec<String> = if reseau {
        let mut u: Vec<String> = appairage::adresses_poste().iter().map(|ip| garde().adresse(ip, port)).collect();
        u.push(garde().adresse(&format!("{}.local", appairage::nom_poste()), port));
        u
    } else {
        vec![format!("http://127.0.0.1:{port}")]
    };
    println!("\n   Pour jouer depuis la tablette (même Wi-Fi), ouvre l'une de ces adresses :\n");
    for u in &urls {
        println!("      {u}");
    }
    println!();
    let _ = fs::write(racine.join("adresse-reseau.txt"), urls.join("\n") + "\n");
    garde().annoncer();
    let _ = io::stdout().flush();
    if !args.sans_jeu {
        let j = jeu.clone();
        std::thread::spawn(move || j.demarrer());
    }
    srv.servir(service(jeu));
}

#[cfg(test)]
mod essais {
    use super::*;
    use serde_json::json;
    use std::sync::Mutex;

    fn a(l: &[&str]) -> Vec<String> {
        l.iter().map(|x| x.to_string()).collect()
    }

    #[test]
    fn ligne_de_commande() {
        // la commande du catalogue, puis les options standard et celles de l'ancien serveur
        let x = lire_args(&a(&["--port", "8791"])).unwrap();
        assert_eq!((x.port, x.local, x.largeur, x.hauteur, x.affichage, x.vnc_port), (8791, false, 1024, 768, 99, 5999));
        let x = lire_args(&a(&[
            "--port=9000", "--lan", "--local", "--resolution", "1280x720", "--sans-jeu", "--affichage", "97",
            "--vnc-port=5997", "--sans-navigateur", "--sans-pause", "--sans-maj",
        ]))
        .unwrap();
        assert_eq!(x, Args { port: 9000, lan: true, local: true, largeur: 1280, hauteur: 720, affichage: 97, vnc_port: 5997, sans_jeu: true });
        assert_eq!(lire_args(&[]).unwrap().port, PORT);
        let faux: [&[&str]; 6] = [&["--port"], &["--inconnu"], &["--resolution", "1024"], &["--resolution", "0x768"], &["--local=1"], &["--port", "x"]];
        for faux in faux {
            assert!(lire_args(&a(faux)).is_err(), "{faux:?}");
        }
    }

    #[test]
    fn chemin_hors_base_refuse() {
        let web = racine_depot().join("web");
        assert_eq!(fichier_sous(&web, "../Cargo.toml"), None);
        assert_eq!(fichier_sous(&web, "/%2e%2e/Cargo.toml"), None);
        assert_eq!(fichier_sous(&web, ".cache"), None);
        assert!(fichier_sous(&web, "index.html").is_some());
    }

    // ------------------------------------------------------------- aides

    fn racine_depot() -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join("..")
    }

    /// Parle juste assez de RFB (sans mot de passe) pour que Entree et le relais s'y connectent ;
    /// garde ce que chaque client envoie ensuite.
    fn faux_vnc() -> (u16, Arc<Mutex<Vec<u8>>>) {
        let ecoute = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = ecoute.local_addr().unwrap().port();
        let recu: Arc<Mutex<Vec<u8>>> = Arc::default();
        let r = recu.clone();
        std::thread::spawn(move || {
            for c in ecoute.incoming().flatten() {
                let r = r.clone();
                std::thread::spawn(move || {
                    let mut c = c;
                    let mut b = [0u8; 1024];
                    let mut lire = |c: &mut TcpStream, n: usize| c.read_exact(&mut b[..n]).is_ok();
                    let _ = c.write_all(b"RFB 003.008\n");
                    if !lire(&mut c, 12) || c.write_all(b"\x01\x01").is_err() || !lire(&mut c, 1) {
                        return;
                    }
                    let _ = c.write_all(&[0, 0, 0, 0]); // sécurité acceptée
                    if !lire(&mut c, 1) {
                        return; // ClientInit
                    }
                    let mut init = vec![5, 0, 2, 208]; // 1280 x 720
                    init.extend([0u8; 16]);
                    init.extend([0, 0, 0, 4]);
                    init.extend(b"test");
                    let _ = c.write_all(&init);
                    let mut b = [0u8; 1024];
                    while let Ok(n) = c.read(&mut b) {
                        if n == 0 {
                            break;
                        }
                        r.lock().unwrap().extend_from_slice(&b[..n]);
                    }
                });
            }
        });
        (port, recu)
    }

    fn lancer(port_vnc: u16) -> (Arc<Jeu>, u16) {
        let _ = GARDE.set(Garde::new("TEST", false, None, None));
        let jeu = Jeu::new(1280, 720, 98, port_vnc, racine_depot());
        prendre(&jeu.etat).etat = "en_marche"; // on saute le lancement des programmes
        let srv = Serveur::ouvrir(0, false, 1).unwrap();
        srv.servir_en_fond(service(jeu.clone()));
        (jeu, srv.port())
    }

    fn tete(c: &mut TcpStream) -> String {
        let mut t = Vec::new();
        let mut o = [0u8; 1];
        while !t.ends_with(b"\r\n\r\n") && c.read(&mut o).unwrap_or(0) == 1 {
            t.push(o[0]);
        }
        String::from_utf8_lossy(&t).into_owned()
    }

    fn brut(port: u16, requete: &str) -> (u16, String) {
        let mut c = TcpStream::connect(("127.0.0.1", port)).unwrap();
        c.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
        c.write_all(requete.as_bytes()).unwrap();
        let mut tout = Vec::new();
        let _ = c.read_to_end(&mut tout);
        let texte = String::from_utf8_lossy(&tout).into_owned();
        (texte.split(' ').nth(1).and_then(|c| c.parse().ok()).unwrap_or(0), texte)
    }

    fn http(port: u16, methode: &str, chemin: &str) -> (u16, String) {
        brut(port, &format!("{methode} {chemin} HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Theme: 1\r\n\r\n"))
    }

    fn ouvrir_ws(port: u16, chemin: &str) -> (String, TcpStream) {
        let mut c = TcpStream::connect(("127.0.0.1", port)).unwrap();
        c.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
        let d = format!(
            "GET {chemin} HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\
             Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: binary\r\n\r\n"
        );
        c.write_all(d.as_bytes()).unwrap();
        (tete(&mut c), c)
    }

    fn trame_client(d: &[u8], op: u8) -> Vec<u8> {
        let masque = [7u8, 1, 9, 3];
        let mut t = vec![0x80 | op];
        if d.len() < 126 {
            t.push(0x80 | d.len() as u8);
        } else {
            t.push(0x80 | 126);
            t.extend((d.len() as u16).to_be_bytes());
        }
        t.extend(masque);
        t.extend(d.iter().enumerate().map(|(i, o)| o ^ masque[i % 4]));
        t
    }

    fn lire_trame(c: &mut TcpStream) -> (u8, Vec<u8>) {
        let mut t = [0u8; 2];
        c.read_exact(&mut t).unwrap();
        let mut n = (t[1] & 0x7F) as usize;
        if n == 126 {
            let mut l = [0u8; 2];
            c.read_exact(&mut l).unwrap();
            n = u16::from_be_bytes(l) as usize;
        }
        let mut d = vec![0u8; n];
        c.read_exact(&mut d).unwrap();
        (t[0] & 0x0F, d)
    }

    fn attendre(condition: impl Fn() -> bool) -> bool {
        (0..100).any(|_| {
            std::thread::sleep(Duration::from_millis(20));
            condition()
        })
    }

    // ------------------------------------------------------------- serveur

    #[test]
    fn pages_et_statut() {
        let (_, port) = lancer(1);
        let (code, texte) = http(port, "GET", "/");
        assert!(code == 200 && texte.contains("Theme Hospital") && texte.contains("nosniff"));
        let (code, texte) = http(port, "GET", "/api/status");
        let corps: Value = serde_json::from_str(texte.split("\r\n\r\n").nth(1).unwrap()).unwrap();
        assert_eq!((code, corps["etat"].as_str(), corps["son"].as_u64()), (200, Some("en_marche"), Some(22050)));
        assert_eq!(corps.as_object().unwrap().keys().collect::<Vec<_>>(), ["etat", "message", "largeur", "hauteur", "son", "novnc", "jeu_trouve"]);
        for chemin in ["/../Cargo.toml", "/inconnu", "/.git/config", "/Cargo.toml", "/%2e%2e/Cargo.toml", "/api/inconnu"] {
            assert_eq!(http(port, "GET", chemin).0, 404, "{chemin}");
        }
        assert_eq!(http(port, "DELETE", "/").0, 405);
        assert_eq!(brut(port, "GET / HTTP/1.1\r\nHost: piege.example\r\n\r\n").0, 403);
    }

    #[test]
    fn ecriture_depuis_un_autre_site() {
        let (_, port) = lancer(1);
        assert_eq!(brut(port, "POST /api/arreter HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n").0, 403);
        let ws = "GET /vnc HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: https://piege.example\r\nUpgrade: websocket\r\nSec-WebSocket-Key: abc\r\n\r\n";
        assert_eq!(brut(port, ws).0, 403);
        let corps = |c: &str| format!("POST /api/demarrer HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Theme: 1\r\nContent-Length: {}\r\n\r\n{}", c.len(), c);
        assert_eq!(brut(port, &corps("pas du json")).0, 400);
        assert_eq!(brut(port, "POST /api/demarrer HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Theme: 1\r\nContent-Length: 100000\r\n\r\n").0, 413);
    }

    #[test]
    fn refus_si_jeu_arrete() {
        let (jeu, port) = lancer(1);
        prendre(&jeu.etat).etat = "arrete";
        let ws = "GET /vnc HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nSec-WebSocket-Key: abc\r\n\r\n";
        assert_eq!(brut(port, ws).0, 503);
        let ws = "GET /autre HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nSec-WebSocket-Key: abc\r\n\r\n";
        assert_eq!(brut(port, ws).0, 404);
    }

    #[test]
    fn relais_vnc() {
        let (port_vnc, _) = faux_vnc();
        let (_, port) = lancer(port_vnc);
        let (t, mut c) = ouvrir_ws(port, "/vnc");
        assert!(t.starts_with("HTTP/1.1 101 ") && t.contains("Connection: Upgrade\r\n"), "{t}");
        assert!(t.contains("Sec-WebSocket-Protocol: binary\r\n") && !t.contains("Content-Length"));
        assert_eq!(lire_trame(&mut c), (2, b"RFB 003.008\n".to_vec())); // le serveur VNC parle au navigateur
        c.write_all(&trame_client(b"RFB 003.008\n", 2)).unwrap(); // le navigateur répond...
        let mut tampon = Vec::new();
        while tampon.len() < 2 {
            tampon.extend(lire_trame(&mut c).1);
        }
        assert_eq!(tampon, b"\x01\x01"); // ...et reçoit la suite de la poignée de main
        c.write_all(&trame_client(b"\x01", 2)).unwrap();
        let mut tampon = Vec::new();
        while tampon.len() < 4 {
            tampon.extend(lire_trame(&mut c).1);
        }
        assert_eq!(tampon, [0, 0, 0, 0]);
    }

    #[test]
    fn relais_son() {
        let (jeu, port) = lancer(1);
        let (_, mut c) = ouvrir_ws(port, "/son");
        assert!(attendre(|| !prendre(&jeu.auditeurs).is_empty()));
        for (_, q) in prendre(&jeu.auditeurs).iter() {
            q.try_send(vec![1, 0, 2, 0]).unwrap();
        }
        assert_eq!(lire_trame(&mut c), (2, vec![1, 0, 2, 0]));
        drop(c);
        assert!(attendre(|| prendre(&jeu.auditeurs).is_empty())); // page fermée : plus d'auditeur
    }

    #[test]
    fn entree_pointeur_et_touches() {
        let (port_vnc, recu) = faux_vnc();
        let (_, port) = lancer(port_vnc);
        let (_, mut c) = ouvrir_ws(port, "/input");
        for msg in [
            json!({"t": "p", "x": 640, "y": 360, "b": 1}),
            json!({"t": "p", "x": 5000, "y": -3, "b": 4}), // hors écran : ramené dans l'écran
            json!({"t": "k", "k": 0xff52, "d": 1}),
            json!({"t": "k", "k": 0xff52, "d": 0}),
            json!({"t": "p", "x": "abc"}), // messages invalides : ignorés
            json!({"t": "x"}),
        ] {
            c.write_all(&trame_client(msg.to_string().as_bytes(), 1)).unwrap();
        }
        let attendu: Vec<u8> = [&[5u8, 1, 2, 128, 1, 104][..], &[5, 4, 4, 255, 0, 0], &[4, 1, 0, 0, 0, 0, 0xff, 0x52], &[4, 0, 0, 0, 0, 0, 0xff, 0x52]].concat();
        assert!(attendre(|| *recu.lock().unwrap() == attendu), "{:?}", recu.lock().unwrap());
        std::thread::sleep(Duration::from_millis(200));
        assert_eq!(*recu.lock().unwrap(), attendu);
    }
}
