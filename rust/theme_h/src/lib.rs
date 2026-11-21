//! Le moteur : l'écran virtuel (Xvfb), x11vnc et CorsixTH, lancés et surveillés ; le son du jeu
//! lu dans un tube ; un client VNC minimal qui injecte la souris et le clavier.
//! Linux seulement (Raspberry Pi OS, Debian, Ubuntu...).

pub mod serveur;

use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::net::{Shutdown, SocketAddr, TcpStream};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{SyncSender, TrySendError};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use nix::sys::signal::{kill, killpg, Signal};
use nix::unistd::Pid;
use serde_json::{json, Value};

/// Son du jeu : PCM 16 bits signé petit-boutiste, stéréo (format de SDL_mixer).
pub const SON_FREQUENCE: u32 = 22050;

/// Un verrou empoisonné (panique dans un autre fil) se reprend : l'état reste lisible.
pub fn prendre<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

// ----------------------------------------------------------------- recherche des fichiers

pub fn trouver_novnc(racine: &Path) -> Option<PathBuf> {
    let env = PathBuf::from(std::env::var("NOVNC_DIR").unwrap_or_default());
    [env, "/usr/share/novnc".into(), "/usr/share/javascript/novnc".into(), racine.join("vendor").join("novnc")]
        .into_iter()
        .find(|c| !c.as_os_str().is_empty() && c.join("core").join("rfb.js").is_file())
}

fn nom(p: &Path) -> String {
    p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
}

/// Dossier (base ou sous-dossier) qui contient Data/VBlk-0.dat (casse libre), sinon None.
pub fn trouver_donnees(base: &Path) -> Option<PathBuf> {
    trouver_donnees_sous(base, 3)
}

fn trouver_donnees_sous(base: &Path, profondeur: u32) -> Option<PathBuf> {
    let mut enfants: Vec<PathBuf> = fs::read_dir(base).ok()?.flatten().map(|e| e.path()).collect();
    enfants.sort();
    for e in &enfants {
        if e.is_dir() && nom(e).eq_ignore_ascii_case("data") {
            if let Ok(d) = fs::read_dir(e) {
                if d.flatten().any(|f| f.file_name().to_string_lossy().eq_ignore_ascii_case("vblk-0.dat")) {
                    return Some(base.to_path_buf());
                }
            }
        }
    }
    if profondeur > 0 {
        for e in &enfants {
            if e.is_dir() && !nom(e).starts_with('.') {
                if let Some(r) = trouver_donnees_sous(e, profondeur - 1) {
                    return Some(r);
                }
            }
        }
    }
    None
}

fn est_executable(p: &Path) -> bool {
    fs::metadata(p).map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0).unwrap_or(false)
}

/// Le programme `nom` dans le PATH (et /usr/games, hors du PATH sous Debian).
pub fn dans_le_path(nom: &str) -> Option<PathBuf> {
    let path = format!("{}:/usr/games", std::env::var("PATH").unwrap_or_default());
    path.split(':').filter(|d| !d.is_empty()).map(|d| Path::new(d).join(nom)).find(|c| est_executable(c))
}

pub fn commande_jeu(racine: &Path) -> Option<Vec<String>> {
    if let Ok(env) = std::env::var("TH_JEU") {
        if !env.is_empty() {
            return shlex::split(&env);
        }
    }
    let compile = racine.join("vendor").join("corsixth").join("bin").join("corsix-th"); // installé par raspberry/corsixth.sh
    if compile.is_file() {
        return Some(vec![compile.to_string_lossy().into_owned()]);
    }
    ["corsix-th", "CorsixTH", "corsixth"].iter().find_map(|n| dans_le_path(n)).map(|c| vec![c.to_string_lossy().into_owned()])
}

/// TH_XVFB, TH_X11VNC : une autre commande pour les essais ; sinon le programme lui-même.
fn commande_env(variable: &str, defaut: &str) -> Vec<String> {
    std::env::var(variable).ok().and_then(|v| shlex::split(&v)).filter(|c| !c.is_empty()).unwrap_or_else(|| vec![defaut.to_string()])
}

/// Met à jour le config.txt de CorsixTH : impose ce dont le serveur a besoin, garde le reste
/// (langue, volumes... réglés dans le jeu, qui réécrit lui-même ce fichier).
pub fn ecrire_config_jeu(donnees: &Path, dossier_jeu: &Path, largeur: u32, hauteur: u32) -> io::Result<PathBuf> {
    let dossier = donnees.join("corsixth");
    fs::create_dir_all(dossier.join("saves"))?;
    fs::create_dir_all(dossier.join("captures"))?;
    let chemin = dossier.join("config.txt");
    let imposes: Vec<(&str, String)> = vec![
        ("theme_hospital_install", format!("[[{}]]", dossier_jeu.display())),
        ("width", largeur.to_string()),
        ("height", hauteur.to_string()),
        ("fullscreen", "false".into()),
        ("play_intro", "false".into()),
        ("check_for_updates", "false".into()),
        ("audio", "true".into()),
        ("audio_frequency", SON_FREQUENCE.to_string()),
        ("audio_channels", "2".into()),
        ("audio_buffer_size", "1024".into()),
        ("prevent_edge_scrolling", "true".into()), // au doigt, le curseur reste au bord : la carte défilerait sans fin
        ("debug", "true".into()),                  // F11 ouvre la fenêtre des triches (bouton « Triches » de la page)
        ("savegames", format!("[[{}]]", dossier.join("saves").display())),
        ("screenshots", format!("[[{}]]", dossier.join("captures").display())),
    ];
    // absent ou illisible : on repart d'un fichier neuf
    let mut lignes: Vec<String> = fs::read_to_string(&chemin).map(|t| t.lines().map(String::from).collect()).unwrap_or_default();
    let mut vus: Vec<String> = Vec::new();
    for ligne in lignes.iter_mut() {
        let Some((cle, _)) = ligne.split_once('=') else { continue };
        let cle = cle.trim().to_string();
        if let Some((_, v)) = imposes.iter().find(|(c, _)| *c == cle) {
            *ligne = format!("{} = {}", cle, v);
            vus.push(cle);
        }
    }
    for (c, v) in &imposes {
        if !vus.iter().any(|x| x.as_str() == *c) {
            lignes.push(format!("{} = {}", c, v));
        }
    }
    let tmp = dossier.join(".config.txt.tmp");
    fs::write(&tmp, lignes.join("\n") + "\n")?;
    fs::rename(&tmp, &chemin)?;
    Ok(chemin)
}

/// Supprime le verrou d'un Xvfb mort (arrêt brutal) qui empêcherait de redémarrer.
fn nettoyer_affichage(num: &str) {
    let verrou = PathBuf::from(format!("/tmp/.X{}-lock", num));
    let Some(pid) = fs::read_to_string(&verrou).ok().and_then(|t| t.trim().parse::<i32>().ok()) else { return };
    if pid <= 0 {
        return;
    }
    match kill(Pid::from_raw(pid), None::<Signal>) {
        Ok(()) => return, // encore vivant : on n'y touche pas
        Err(nix::errno::Errno::ESRCH) => {}
        Err(_) => return,
    }
    let _ = fs::remove_file(&verrou);
    let _ = fs::remove_file(format!("/tmp/.X11-unix/X{}", num));
}

// ----------------------------------------------------------------- entrées : client VNC minimal

fn lire_n(f: &mut TcpStream, n: usize) -> io::Result<Vec<u8>> {
    let mut b = vec![0u8; n];
    f.read_exact(&mut b)?;
    Ok(b)
}

/// Second client VNC, qui n'affiche rien : il injecte la souris et le clavier.
pub struct Entree {
    flux: TcpStream,
}

impl Entree {
    pub fn ouvrir(port: u16) -> io::Result<Entree> {
        let delai = Some(Duration::from_secs(5));
        let mut f = TcpStream::connect_timeout(&SocketAddr::from(([127, 0, 0, 1], port)), Duration::from_secs(5))?;
        f.set_read_timeout(delai)?;
        f.set_write_timeout(delai)?;
        lire_n(&mut f, 12)?; // "RFB 003.008\n"
        f.write_all(b"RFB 003.008\n")?;
        let n = lire_n(&mut f, 1)?[0] as usize;
        if n == 0 {
            return Err(io::Error::other("connexion refusée par x11vnc"));
        }
        if !lire_n(&mut f, n)?.contains(&1) {
            return Err(io::Error::other("x11vnc demande un mot de passe"));
        }
        f.write_all(&[1])?; // sécurité : aucune
        if lire_n(&mut f, 4)? != [0u8; 4] {
            return Err(io::Error::other("poignée de main VNC refusée"));
        }
        f.write_all(&[1])?; // ClientInit : partagé
        lire_n(&mut f, 4 + 16)?; // taille + format de pixels
        let b = lire_n(&mut f, 4)?;
        let taille_nom = u32::from_be_bytes([b[0], b[1], b[2], b[3]]) as usize;
        if taille_nom > 65536 {
            return Err(io::Error::other("nom du bureau VNC trop long"));
        }
        lire_n(&mut f, taille_nom)?;
        // on ignore tout ce que x11vnc envoie ; ce fil finit quand la connexion se ferme
        let mut vider = f.try_clone()?;
        vider.set_read_timeout(None)?;
        std::thread::spawn(move || {
            let mut b = vec![0u8; 65536];
            while matches!(vider.read(&mut b), Ok(n) if n > 0) {}
        });
        Ok(Entree { flux: f })
    }

    pub fn pointeur(&mut self, x: i64, y: i64, masque: i64, largeur: u32, hauteur: u32) -> io::Result<()> {
        let x = x.clamp(0, largeur as i64 - 1) as u16;
        let y = y.clamp(0, hauteur as i64 - 1) as u16;
        let mut m = vec![5, (masque & 0x1F) as u8];
        m.extend(x.to_be_bytes());
        m.extend(y.to_be_bytes());
        self.flux.write_all(&m)
    }

    pub fn touche(&mut self, keysym: u32, appuyee: bool) -> io::Result<()> {
        let mut m = vec![4, appuyee as u8, 0, 0];
        m.extend(keysym.to_be_bytes());
        self.flux.write_all(&m)
    }
}

impl Drop for Entree {
    fn drop(&mut self) {
        let _ = self.flux.shutdown(Shutdown::Both);
    }
}

fn invalide() -> io::Error {
    io::ErrorKind::InvalidData.into()
}

/// int() de Python : un nombre (tronqué), un booléen ou un texte de chiffres.
fn entier(v: &Value) -> io::Result<i64> {
    match v {
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().filter(|f| f.is_finite()).map(|f| f as i64)),
        Value::Bool(b) => Some(*b as i64),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
    .ok_or_else(invalide)
}

/// bool() de Python.
fn vrai(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

// ----------------------------------------------------------------- le jeu et son écran virtuel

struct Etat {
    etat: &'static str, // arrete | demarrage | en_marche | erreur
    message: String,
    procs: Vec<(&'static str, Child)>,
    son: Option<Arc<AtomicBool>>, // arrête la pompe du son
    session: u64,
}

pub type Auditeurs = Arc<Mutex<Vec<(u64, SyncSender<Vec<u8>>)>>>;

pub struct Jeu {
    pub largeur: u32,
    pub hauteur: u32,
    pub affichage: String,
    pub port_vnc: u16,
    /// theme_hospital/, donnees/, logs/, vendor/ sont dans ce dossier.
    pub racine: PathBuf,
    verrou: Mutex<()>,
    etat: Mutex<Etat>,
    entree: Mutex<Option<Entree>>,
    /// Les pages qui écoutent le son (numéro, file bornée).
    pub auditeurs: Auditeurs,
}

impl Jeu {
    pub fn new(largeur: u32, hauteur: u32, affichage: u32, port_vnc: u16, racine: PathBuf) -> Arc<Jeu> {
        Arc::new(Jeu {
            largeur,
            hauteur,
            affichage: affichage.to_string(),
            port_vnc,
            racine,
            verrou: Mutex::new(()),
            etat: Mutex::new(Etat { etat: "arrete", message: String::new(), procs: Vec::new(), son: None, session: 0 }),
            entree: Mutex::new(None),
            auditeurs: Arc::default(),
        })
    }

    fn dossier_jeu(&self) -> PathBuf {
        self.racine.join("theme_hospital")
    }

    fn donnees(&self) -> PathBuf {
        self.racine.join("donnees")
    }

    pub fn etat(&self) -> &'static str {
        prendre(&self.etat).etat
    }

    pub fn message(&self) -> String {
        prendre(&self.etat).message.clone()
    }

    pub fn statut(&self) -> Value {
        let (novnc, jeu_trouve) = (trouver_novnc(&self.racine).is_some(), trouver_donnees(&self.dossier_jeu()).is_some());
        let e = prendre(&self.etat);
        json!({"etat": e.etat, "message": e.message, "largeur": self.largeur, "hauteur": self.hauteur,
               "son": SON_FREQUENCE, "novnc": novnc, "jeu_trouve": jeu_trouve})
    }

    pub fn demarrer(self: &Arc<Self>) {
        let _verrou = prendre(&self.verrou);
        {
            let mut e = prendre(&self.etat);
            if e.etat == "demarrage" || e.etat == "en_marche" {
                return;
            }
            e.etat = "demarrage";
            e.message.clear();
        }
        let erreur = match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| self.lancer_tout())) {
            Ok(Ok(())) => return,
            Ok(Err(m)) => m,
            Err(_) => "Erreur inattendue au démarrage.".to_string(),
        };
        self.tout_arreter();
        let mut e = prendre(&self.etat);
        e.etat = "erreur";
        e.message = erreur;
    }

    fn lancer_tout(self: &Arc<Self>) -> Result<(), String> {
        let dossier = trouver_donnees(&self.dossier_jeu())
            .ok_or("Dossier de Theme Hospital introuvable : copie-le dans « theme_hospital/ » (il doit contenir le dossier Data).")?;
        let jeu = commande_jeu(&self.racine).ok_or("CorsixTH est introuvable : lance « sh raspberry/installer.sh --dependances ».")?;
        let xvfb = commande_env("TH_XVFB", "Xvfb");
        let x11vnc = commande_env("TH_X11VNC", "x11vnc");
        let config = ecrire_config_jeu(&self.donnees(), &dossier, self.largeur, self.hauteur)
            .map_err(|e| format!("Impossible d'écrire la configuration du jeu : {}", e))?;
        let tube = self.donnees().join("son.fifo");
        let ecran = format!(":{}", self.affichage);
        // rendu 2D de SDL et affichage X11 direct : sans GPU, l'OpenGL logiciel coûtait ~5 fois plus de processeur
        let mut env: Vec<(&str, String)> = vec![
            ("DISPLAY", ecran.clone()),
            ("SDL_VIDEODRIVER", "x11".into()),
            ("SDL_RENDER_DRIVER", "software".into()),
            ("SDL_FRAMEBUFFER_ACCELERATION", "0".into()),
            ("LIBGL_ALWAYS_SOFTWARE", "1".into()),
        ];
        if self.ouvrir_son(&tube) {
            env.push(("SDL_AUDIODRIVER", "disk".into()));
            env.push(("SDL_DISKAUDIOFILE", tube.to_string_lossy().into_owned()));
            env.push(("SDL_DISKAUDIODELAY", "0".into()));
        } else {
            env.push(("SDL_AUDIODRIVER", "dummy".into()));
        }

        nettoyer_affichage(&self.affichage);
        let mut args = xvfb;
        args.extend([ecran.clone(), "-screen".into(), "0".into(), format!("{}x{}x24", self.largeur, self.hauteur), "-nolisten".into(), "tcp".into()]);
        self.lancer("xvfb", &args, &[])?;
        let socket_x = PathBuf::from(format!("/tmp/.X11-unix/X{}", self.affichage));
        self.attendre(|| socket_x.exists(), "Xvfb", "xvfb")?;

        let port = self.port_vnc.to_string();
        let mut args = x11vnc;
        args.extend(["-display", ecran.as_str(), "-rfbport", port.as_str(), "-localhost", "-nopw", "-forever", "-shared", "-quiet"].map(String::from));
        self.lancer("x11vnc", &args, &env)?;
        let port_vnc = self.port_vnc;
        self.attendre(|| TcpStream::connect(("127.0.0.1", port_vnc)).is_ok(), "x11vnc", "x11vnc")?;

        let mut args = jeu;
        args.push(format!("--config-file={}", config.display()));
        self.lancer("jeu", &args, &env)?;
        std::thread::sleep(Duration::from_secs(2)); // un jeu qui plante au chargement le fait dans les deux premières secondes
        if self.sorti("jeu") {
            return Err("Le jeu s'est arrêté au démarrage (voir logs/jeu.log).".into());
        }
        let session = {
            let mut e = prendre(&self.etat);
            e.etat = "en_marche";
            e.session += 1;
            e.session
        };
        let jeu = self.clone();
        std::thread::spawn(move || jeu.surveiller(session));
        Ok(())
    }

    fn lancer(&self, nom: &'static str, commande: &[String], env: &[(&str, String)]) -> Result<(), String> {
        let (prog, args) = commande.split_first().ok_or("commande vide")?;
        let impossible = |e: io::Error| format!("Impossible de lancer {} : {}", prog, e);
        let logs = self.racine.join("logs");
        let _ = fs::create_dir_all(&logs);
        let log = File::create(logs.join(format!("{}.log", nom))).map_err(impossible)?;
        let sortie = log.try_clone().map_err(impossible)?;
        let enfant = Command::new(prog)
            .args(args)
            .envs(env.iter().cloned())
            .stdin(Stdio::null())
            .stdout(sortie)
            .stderr(log)
            .process_group(0) // son propre groupe : arrêté en entier, sous-processus compris
            .spawn()
            .map_err(impossible)?;
        prendre(&self.etat).procs.push((nom, enfant));
        Ok(())
    }

    fn attendre(&self, pret: impl Fn() -> bool, quoi: &str, nom: &str) -> Result<(), String> {
        let fin = Instant::now() + Duration::from_secs(10);
        while Instant::now() < fin {
            if self.sorti(nom) {
                return Err(format!("{} s'est arrêté au démarrage (voir logs/).", quoi));
            }
            if pret() {
                return Ok(());
            }
            std::thread::sleep(Duration::from_millis(200));
        }
        Err(format!("{} ne répond pas (voir logs/).", quoi))
    }

    fn sorti(&self, nom: &str) -> bool {
        prendre(&self.etat).procs.iter_mut().find(|(n, _)| *n == nom).is_some_and(|(_, p)| !matches!(p.try_wait(), Ok(None)))
    }

    /// Tube où le jeu écrit son son (sortie « disk » de SDL, sans pause : DISKAUDIODELAY=0).
    fn ouvrir_son(&self, tube: &Path) -> bool {
        let _ = fs::remove_file(tube);
        if nix::unistd::mkfifo(tube, nix::sys::stat::Mode::from_bits_truncate(0o600)).is_err() {
            return false; // pas de son, le jeu tourne quand même
        }
        // lecture et écriture : l'ouverture ne bloque pas, et pas de fin de fichier tant que le jeu n'écrit pas encore
        let Ok(f) = OpenOptions::new().read(true).write(true).custom_flags(nix::libc::O_NONBLOCK).open(tube) else {
            return false;
        };
        #[cfg(target_os = "linux")]
        {
            use std::os::fd::AsRawFd;
            let _ = nix::fcntl::fcntl(f.as_raw_fd(), nix::fcntl::FcntlArg::F_SETPIPE_SZ(8192)); // petit tube : peu de retard
        }
        let arret = Arc::new(AtomicBool::new(false));
        prendre(&self.etat).son = Some(arret.clone());
        let auditeurs = self.auditeurs.clone();
        std::thread::spawn(move || pomper_son(f, arret, auditeurs));
        true
    }

    /// Si le jeu (ou son écran) s'arrête, on range tout : la page propose de redémarrer.
    fn surveiller(self: Arc<Self>, session: u64) {
        loop {
            std::thread::sleep(Duration::from_millis(300));
            let mut fini = None;
            {
                let mut e = prendre(&self.etat);
                if e.session != session || e.etat != "en_marche" {
                    return; // arrêt demandé, ou une autre session a pris le relais
                }
                for (n, p) in e.procs.iter_mut() {
                    if !matches!(p.try_wait(), Ok(None)) {
                        fini = Some(*n);
                        break;
                    }
                }
            }
            let Some(nom) = fini else { continue };
            let _verrou = prendre(&self.verrou);
            {
                let e = prendre(&self.etat);
                if e.session != session || e.etat != "en_marche" {
                    return;
                }
            }
            self.tout_arreter();
            let mut e = prendre(&self.etat);
            e.etat = "arrete";
            e.message = if nom == "jeu" { "Partie terminée.".into() } else { format!("L'écran virtuel s'est arrêté ({}).", nom) };
            return;
        }
    }

    fn tout_arreter(&self) {
        drop(prendre(&self.entree).take());
        let (procs, son) = {
            let mut e = prendre(&self.etat);
            (std::mem::take(&mut e.procs), e.son.take())
        };
        for (_, mut p) in procs.into_iter().rev() {
            for sig in [Signal::SIGTERM, Signal::SIGKILL] {
                if !matches!(p.try_wait(), Ok(None)) || killpg(Pid::from_raw(p.id() as i32), sig).is_err() {
                    break;
                }
                let fin = Instant::now() + Duration::from_secs(3);
                while Instant::now() < fin && matches!(p.try_wait(), Ok(None)) {
                    std::thread::sleep(Duration::from_millis(50));
                }
            }
        }
        if let Some(arret) = son {
            arret.store(true, Ordering::SeqCst);
        }
    }

    pub fn arreter(&self) {
        let _verrou = prendre(&self.verrou);
        {
            let mut e = prendre(&self.etat);
            e.etat = "arrete";
            e.message.clear();
        }
        self.tout_arreter();
    }

    pub fn redemarrer(self: &Arc<Self>) {
        self.arreter();
        self.demarrer();
    }

    /// Applique un message de la page : {t:'p',x,y,b} (pointeur) ou {t:'k',k,d} (touche).
    pub fn saisir(&self, msg: &Value) {
        if self.etat() != "en_marche" {
            return;
        }
        let mut entree = prendre(&self.entree);
        let r = (|| -> io::Result<()> {
            if entree.is_none() {
                *entree = Some(Entree::ouvrir(self.port_vnc)?);
            }
            let e = entree.as_mut().ok_or_else(invalide)?;
            match msg.get("t").and_then(Value::as_str) {
                Some("p") => {
                    let b = msg.get("b").map(entier).transpose()?.unwrap_or(0);
                    e.pointeur(entier(&msg["x"])?, entier(&msg["y"])?, b, self.largeur, self.hauteur)
                }
                Some("k") => e.touche(u32::try_from(entier(&msg["k"])?).map_err(|_| invalide())?, vrai(&msg["d"])),
                _ => Ok(()),
            }
        })();
        if r.is_err() {
            *entree = None; // rouvert au prochain message
        }
    }
}

/// Lit le tube au rythme du temps réel : tube plein, le fil audio du jeu attend, et le son sort
/// exactement à la bonne vitesse (la pause de SDL, elle, dérive de plusieurs %).
fn pomper_son(mut tube: File, arret: Arc<AtomicBool>, auditeurs: Auditeurs) {
    let debit = (SON_FREQUENCE * 4) as i64; // octets par seconde : 2 canaux x 16 bits
    let (mut t0, mut lus, mut reste) = (Instant::now(), 0i64, Vec::new());
    let mut tampon = vec![0u8; debit as usize];
    while !arret.load(Ordering::SeqCst) {
        std::thread::sleep(Duration::from_millis(20));
        let du = (t0.elapsed().as_secs_f64() * debit as f64) as i64 - lus;
        let n = if du > 0 { tube.read(&mut tampon[..(du as usize).min(debit as usize)]).unwrap_or(0) } else { 0 };
        lus += n as i64;
        if du > debit / 5 {
            (t0, lus) = (Instant::now(), 0); // 0,2 s de retard : jeu en pause ou pas encore de son
        }
        reste.extend_from_slice(&tampon[..n]);
        let coupe = reste.len() - reste.len() % 4; // trames entières
        let d: Vec<u8> = reste.drain(..coupe).collect();
        if !d.is_empty() {
            // page trop lente : ce morceau est sauté ; page fermée : oubliée
            prendre(&auditeurs).retain(|(_, q)| !matches!(q.try_send(d.clone()), Err(TrySendError::Disconnected(_))));
        }
    }
}

#[cfg(test)]
mod essais {
    use super::*;

    fn dossier(nom: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("theme_h-{}-{}", nom, std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn trouver_les_donnees() {
        let t = dossier("donnees");
        assert_eq!(trouver_donnees(&t), None);
        fs::create_dir_all(t.join("Theme Hospital").join("DATA")).unwrap();
        assert_eq!(trouver_donnees(&t), None); // Data vide : pas le jeu
        fs::write(t.join("Theme Hospital").join("DATA").join("VBLK-0.DAT"), "x").unwrap();
        assert_eq!(trouver_donnees(&t), Some(t.join("Theme Hospital"))); // trouvé dans un sous-dossier
    }

    #[test]
    fn version_compilee_prioritaire() {
        let t = dossier("commande");
        std::env::remove_var("TH_JEU");
        let bin = t.join("vendor").join("corsixth").join("bin");
        fs::create_dir_all(&bin).unwrap();
        fs::write(bin.join("corsix-th"), "x").unwrap();
        assert_eq!(commande_jeu(&t), Some(vec![bin.join("corsix-th").to_string_lossy().into_owned()]));
        std::env::set_var("TH_JEU", "autre jeu"); // TH_JEU reste le plus fort
        assert_eq!(commande_jeu(&t), Some(vec!["autre".to_string(), "jeu".to_string()]));
        std::env::remove_var("TH_JEU");
    }

    #[test]
    fn reglages_du_jeu_conserves() {
        let t = dossier("config");
        let chemin = ecrire_config_jeu(&t, Path::new("/jeu"), 1024, 768).unwrap(); // premier démarrage
        let texte = fs::read_to_string(&chemin).unwrap();
        assert!(texte.contains("width = 1024") && texte.contains("theme_hospital_install = [[/jeu]]"));
        // réglé dans le jeu
        fs::write(&chemin, texte.replace("width = 1024", "width = 640") + "language = [[French]]\nmusic_volume = 0.9\n-- debug = false\n").unwrap();
        ecrire_config_jeu(&t, Path::new("/jeu"), 1024, 768).unwrap(); // redémarrage
        let texte = fs::read_to_string(&chemin).unwrap();
        assert!(texte.contains("language = [[French]]") && texte.contains("music_volume = 0.9"));
        assert!(texte.contains("width = 1024") && !texte.contains("width = 640")); // imposé : remis
        assert_eq!(texte.matches("debug = true").count(), 1);
    }

    #[test]
    fn jeu_sans_donnees() {
        let t = dossier("sans-jeu");
        let jeu = Jeu::new(1280, 720, 96, 5996, t);
        jeu.demarrer();
        assert_eq!(jeu.etat(), "erreur");
        assert!(jeu.message().contains("theme_hospital"));
    }

    #[test]
    fn messages_de_la_page() {
        assert_eq!(entier(&json!(12.7)).unwrap(), 12);
        assert_eq!(entier(&json!("640")).unwrap(), 640);
        assert!(entier(&json!("abc")).is_err() && entier(&Value::Null).is_err());
        assert!(vrai(&json!(1)) && !vrai(&json!(0)) && !vrai(&Value::Null));
    }
}
