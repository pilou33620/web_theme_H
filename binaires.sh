#!/bin/sh
# Met l'exécutable du serveur dans bin/ (à inclure par « . ./binaires.sh » depuis la racine).
# Avec cargo sur le poste : compilé ici, toujours à jour avec les sources.
# Sinon : téléchargé depuis GitHub (publication « binaires », compilée par la CI à chaque
# envoi sur main), une fois par version : bin/commit dit de quelle version il vient.
DEPOT_BINAIRES="https://github.com/pilou33620/web_theme_H/releases/download/binaires"

# L'archive téléchargée doit avoir l'empreinte SHA-256 publiée avec elle : tronquée ou altérée en
# route, elle est refusée. (L'empreinte vient du même dépôt : elle ne protège pas d'un compte volé.)
empreinte_juste() {
  if command -v sha256sum >/dev/null 2>&1; then recu=$(sha256sum bin/.archive); else recu=$(shasum -a 256 bin/.archive); fi
  [ -n "$recu" ] && [ "${recu%% *}" = "$(cat bin/.empreinte)" ] && return 0
  echo "  [!] Empreinte de l'archive incorrecte : archive refusée."
  return 1
}

obtenir_binaires() {
  mkdir -p bin
  if command -v cargo >/dev/null 2>&1; then
    cargo build --release -p theme_h --bins || return 1
    for f in target/release/*; do
      if [ -f "$f" ] && [ -x "$f" ]; then
        # copie puis renommage : un exécutable en cours d'usage ne se réécrit pas
        cp "$f" "bin/.neuf" && mv -f "bin/.neuf" "bin/$(basename "$f")"
      fi
    done
    return 0
  fi
  ici=$(git rev-parse HEAD 2>/dev/null)
  if [ -x bin/theme_h ] && [ -n "$ici" ] && [ "$(cat bin/commit 2>/dev/null)" = "$ici" ]; then
    return 0
  fi
  case "$(uname -s)" in Linux) os=linux ;; Darwin) os=macos ;; *) os=inconnu ;; esac
  case "$(uname -m)" in x86_64|amd64) cpu=x86_64 ;; aarch64|arm64) cpu=aarch64 ;; *) cpu=inconnu ;; esac
  echo "  Téléchargement du serveur ($os-$cpu)..."
  archive="theme_h-$os-$cpu.tar.gz"
  if curl -fsSL -o bin/.archive "$DEPOT_BINAIRES/$archive" \
     && curl -fsSL -o bin/.empreinte "$DEPOT_BINAIRES/$archive.sha256" \
     && empreinte_juste && tar -xzf bin/.archive -C bin; then
    rm -f bin/.archive bin/.empreinte
    return 0
  fi
  rm -f bin/.archive bin/.empreinte
  if [ -x bin/theme_h ]; then
    echo "  [!] Téléchargement impossible : on garde l'exécutable déjà présent."
    return 0
  fi
  echo "[X] Pas d'exécutable pour ce poste ($os-$cpu) et téléchargement impossible."
  echo "    Installer Rust (https://rustup.rs) puis relancer : il sera compilé ici."
  return 1
}
