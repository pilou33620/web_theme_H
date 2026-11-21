//! Mode réseau : un autre appareil sans jeton n'atteint ni la page, ni l'API, ni les WebSocket.
//! Fichier à part : WEB_TOOLS_BANC_DISTANT=1 fait passer ce poste pour un autre appareil,
//! pour tout le processus.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::Path;
use std::time::Duration;

use theme_h::serveur::{service, GARDE};
use theme_h::Jeu;
use web_tools_commun::appairage::{Garde, BISCUIT, PAGE};
use web_tools_commun::serveur::Serveur;

fn brut(port: u16, requete: &str) -> (u16, String) {
    let mut c = TcpStream::connect(("127.0.0.1", port)).unwrap();
    c.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    c.write_all(requete.as_bytes()).unwrap();
    let mut tout = Vec::new();
    let _ = c.read_to_end(&mut tout);
    let texte = String::from_utf8_lossy(&tout).into_owned();
    (texte.split(' ').nth(1).and_then(|c| c.parse().ok()).unwrap_or(0), texte)
}

#[test]
fn appareil_non_appaire() {
    let tmp = std::env::temp_dir().join(format!("theme_h-garde-{}", std::process::id()));
    std::fs::create_dir_all(&tmp).unwrap();
    std::env::set_var("WEB_TOOLS_JETON_FICHIER", tmp.join("jeton.txt"));
    std::env::set_var("WEB_TOOLS_BANC_DISTANT", "1");
    let _ = GARDE.set(Garde::new("TEST", true, None, None));
    let racine = Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join("..");
    let jeu = Jeu::new(1280, 720, 98, 1, racine);
    let srv = Serveur::ouvrir(0, false, 1).unwrap();
    srv.servir_en_fond(service(jeu));
    let port = srv.port();
    let demande = |methode: &str, chemin: &str, entetes: &str| {
        brut(port, &format!("{methode} {chemin} HTTP/1.1\r\nHost: 127.0.0.1\r\n{entetes}\r\n"))
    };

    assert_eq!(brut(port, "GET / HTTP/1.1\r\nHost: piege.example\r\n\r\n").0, 403);
    let (code, texte) = demande("GET", "/", "");
    assert!(code == 303 && texte.contains(&format!("Location: {PAGE}")), "{texte}");
    assert_eq!(demande("GET", "/api/status", "").0, 403);
    assert_eq!(demande("POST", "/api/arreter", "X-Theme: 1\r\n").0, 403);
    assert_eq!(demande("GET", "/vnc", "Upgrade: websocket\r\nSec-WebSocket-Key: abc\r\n").0, 403);
    assert_eq!(demande("GET", PAGE, "").0, 200);

    // avec le jeton (cookie), tout passe la garde
    let biscuit = format!("Cookie: {}={}\r\n", BISCUIT, GARDE.get().unwrap().jeton.as_ref().unwrap());
    assert_eq!(demande("GET", "/", &biscuit).0, 200);
    assert_eq!(demande("GET", "/vnc", &format!("{biscuit}Upgrade: websocket\r\nSec-WebSocket-Key: abc\r\n")).0, 503); // jeu arrêté
    let _ = std::fs::remove_dir_all(&tmp);
}
