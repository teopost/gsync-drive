# GDrive Sync

> 🇬🇧 English version: [README.md](README.md)

Sincronizzazione bidirezionale con Google Drive per GNOME (Ubuntu 26.04+).

GNOME 49 ha rimosso l'integrazione Google Drive da GNOME Online Accounts.
GDrive Sync la ripristina in una forma più robusta: i file vivono in una
cartella locale (`~/GoogleDrive`) utilizzabile da qualsiasi applicazione, e un
servizio in background propaga le modifiche in entrambe le direzioni tramite
[rclone bisync](https://rclone.org/bisync/).

## Caratteristiche

- **File locali**: si lavora sempre su disco, anche offline; le modifiche si
  sincronizzano quando torna la rete.
- **Sync bidirezionale** automatica: rilevamento modifiche locali (inotify,
  debounce 10 s) + sincronizzazione periodica configurabile.
- **Più account Google**: ogni account ha remote, cartella locale, intervallo,
  esclusioni e gestione conflitti indipendenti.
- **App GNOME nativa** (GTK4/libadwaita): wizard con login Google nel browser,
  lista account, preferenze, log. La prima sincronizzazione gira nel servizio:
  chiudendo la finestra può proseguire in background.
- **Selezione delle cartelle**: nel wizard un albero con checkbox mostra le
  cartelle del Drive (con la dimensione di ciascuna); si possono scegliere più
  cartelle da sincronizzare, oppure nessuna per sincronizzare tutto il Drive.
- **Multilingua**: inglese, francese e italiano, selezionati automaticamente
  in base alla lingua di sistema.
- **Gestione conflitti**: i file modificati su entrambi i lati vengono
  conservati in doppia copia e risolti dall'app (tieni locale / Drive / entrambi).
- **Sicurezza dei dati**: mai resync automatici; soglia sulle cancellazioni di
  massa (`--max-delete`); ripristino guidato con anteprima dry-run.
- **Integrazione desktop**: notifiche, cartella nella barra laterale di File,
  avvio automatico via systemd utente, attivazione D-Bus.

## Installazione

```bash
./build-deb.sh
sudo apt install ./gdrive-sync_0.4.1_all.deb
```

Poi avvia **GDrive Sync** dalle attività e segui la procedura guidata.

Il pacchetto dipende da `rclone` dei repo Ubuntu (1.60): funziona, ma per una
sync più robusta è consigliato rclone ≥ 1.66 da [rclone.org](https://rclone.org/install/)
— l'app rileva la versione e abilita da sola le protezioni aggiuntive
(`--resilient`, `--recover`, `--max-lock`).

## Architettura

| Componente | Descrizione |
|---|---|
| `gdrive-sync` | App GTK4/libadwaita (wizard, stato, preferenze, conflitti) |
| `gdrive-sync-daemon` | Servizio systemd utente (`Type=dbus`) con la macchina a stati di sync |
| D-Bus | `io.github.teopost.GDriveSync.Daemon` — ListAccounts, GetAccountInfo, SyncNow, Pause/Resume, Resync, CancelSync, log (tutti per-account) |
| rclone | Motore di sync; un remote dedicato per account (`gdrive-sync-accountN:`) con OAuth gestito da rclone |

Stati del daemon: `unconfigured, idle, syncing, paused, offline, error,
needs_resync, resyncing`. Gli errori temporanei riprovano con backoff
(1 m → 5 m → 15 m → 1 h); gli stati che richiedono un intervento producono
una notifica e si risolvono dall'app.

File e directory usati (per ogni `<id>` account: `account1`, `account2`, …):

- `~/.config/gdrive-sync/filters-<id>.txt` — esclusioni e selezione cartelle
  (sintassi filtri rclone: le cartelle scelte diventano regole `+ /Cartella/**`)
- `~/.local/state/gdrive-sync/` — log e stato bisync per account
- GSettings `io.github.teopost.GDriveSync` (+ schema rilocabile `.Account` in
  `/io/github/teopost/GDriveSync/accounts/<id>/`) — preferenze
- `~/.config/rclone/rclone.conf` — credenziali dei remote `gdrive-sync-<id>`

## Sviluppo

```bash
# dipendenze: python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1, rclone; per i test:
python3 -m venv --system-site-packages .venv && .venv/bin/pip install pytest watchdog -e .
.venv/bin/pytest

# eseguire dal checkout (schema GSettings compilato in una dir locale):
mkdir -p /tmp/gds-schemas && cp data/*.gschema.xml /tmp/gds-schemas && glib-compile-schemas /tmp/gds-schemas
GSETTINGS_SCHEMA_DIR=/tmp/gds-schemas python3 -m gdrive_sync.daemon.main   # daemon
GSETTINGS_SCHEMA_DIR=/tmp/gds-schemas python3 -m gdrive_sync.gui.main      # GUI
```

Per testare senza toccare il proprio Drive: impostare la chiave nascosta
`remote` a una directory locale (`gsettings set io.github.teopost.GDriveSync
remote /tmp/fake-drive`).

Le stringhe traducibili stanno in `po/` (`it.po`, `fr.po`; template
`gdrive-sync.pot`). I cataloghi `.mo` vengono compilati durante la build
(`msgfmt` se presente, altrimenti `po/compile-mo.py`); per provarli dal
checkout: `python3 po/compile-mo.py po/it.po locale/it/LC_MESSAGES/gdrive-sync.mo`.

La build ufficiale Debian usa `dpkg-buildpackage -us -uc -b`
(richiede `debhelper`, `dh-python`, `python3-all`, `pybuild-plugin-pyproject`);
`build-deb.sh` è l'equivalente senza toolchain per uso rapido.

## Paternità del codice

Tutto il codice e la documentazione di questo progetto sono stati scritti
interamente da **Fable** (Claude Fable 5, il modello AI di Anthropic),
tramite [Claude Code](https://claude.com/claude-code), su indicazioni e
specifiche di Stefano Teodorani, che ha guidato il progetto e verificato i
risultati.

## Licenza

GPL-3.0-or-later — © 2026 Stefano Teodorani
