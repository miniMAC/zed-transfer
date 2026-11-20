# Zed Transfer

Carica e scarica singoli file da Zed via SFTP, FTPS esplicito o FTP. Puoi riusare il tuo `.vscode/sftp.json` e avviare i trasferimenti dal pannello **Tasks** di Zed o dal terminale.

Il progetto si ispira a [zed-sftp](https://github.com/andreyc0d3r/zed-sftp) e usa i task integrati di Zed. I trasferimenti partono su richiesta e il tool mostra la destinazione prima di chiedere conferma.

## Installazione su macOS e Linux

Servono Python 3.11 o successivo, `pipx`, Git e OpenSSH per SFTP. Su macOS puoi usare Homebrew per installare Python e `pipx`.

```sh
brew install python pipx
```

Installa il tool e aggiungi i comandi di `pipx` al `PATH` della shell.

```sh
pipx ensurepath
pipx install git+https://github.com/miniMAC/zed-transfer.git
```

Se hai il sorgente come archivio ZIP, estrailo, entra nella cartella che contiene `pyproject.toml` ed esegui `pipx install .`.

Apri un nuovo terminale e riavvia Zed per caricare il percorso aggiornato. Esegui questi comandi dalla cartella del sito o del progetto.

```sh
zed-transfer install-zed
zed-transfer check
```

`install-zed` crea `.zed/tasks.json`. Se il file esiste già, aprilo con **Zed: Open Project Tasks** e copia i quattro oggetti dal [modello dei task](zed_transfer/templates/tasks.json) nell'array già presente, separandoli con virgole. `check` legge il profilo attivo e mostra la destinazione senza collegarsi al server.

## Configurazione e migrazione da VS Code

Mantieni `.vscode/sftp.json` nella radice del progetto. Sono supportati commenti e virgole finali. In assenza di quel file viene cercato `.zed/sftp.json`. Per usare un file diverso all'interno del progetto, puoi specificarlo con `--config`. Tieni i file di connessione fuori da Git aggiungendoli al `.gitignore` del progetto.

Questa configurazione mappa la cartella `wp-content` di un sito WordPress.

```jsonc
{
  "name": "WordPress staging",
  "protocol": "sftp",
  "host": "sftp.example.com",
  "port": 22,
  "username": "deploy",
  "privateKeyPath": "~/.ssh/id_ed25519",
  "context": "site/wp-content",
  "remotePath": "/public_html/wp-content",
  // Opzionale, se la cartella esiste sul server: "remoteTempPath": "/private/deploy-tmp",
  "ignore": ["node_modules", "**/*.log"],
}
```

Con questo profilo `site/wp-content/themes/mio-tema/style.css` corrisponde a `/public_html/wp-content/themes/mio-tema/style.css` sul server. Usa il percorso assoluto visto dal tuo utente SFTP. Su un hosting con accesso limitato, `public_html` può trovarsi direttamente sotto `/`, anche se SFTP mostra una struttura diversa da quella del pannello hosting.

| Opzione | Uso |
| --- | --- |
| `host`, `port`, `username`, `protocol` | Connessione. Il protocollo predefinito è `sftp`. |
| `remotePath`, `context` | Cartella remota e sottocartella locale da mappare. |
| `remoteTempPath` | Cartella remota opzionale per i file temporanei degli upload. Usa un percorso assoluto verso una cartella già esistente, scrivibile e sullo stesso filesystem della destinazione. |
| `privateKeyPath`, `agent` | Percorso della chiave privata, oppure agente SSH e chiavi OpenSSH predefinite. Puoi lasciare `"agent": "$SSH_AUTH_SOCK"` nei JSON che già usano l'agente. |
| `password` | `true` chiede la password nel terminale. Anche se il valore è una stringa, per impostazione predefinita il tool chiede di digitarla. |
| `profiles`, `defaultProfile` | Profili che ereditano i campi comuni. `--profile staging` seleziona esplicitamente `staging`. |
| `name` | Per un array di server, `--name "WordPress staging"` seleziona l'elemento corrispondente. |
| `ignore`, `connectTimeout` | Esclusioni relative a `context` con `*`, `?` e `**` come segmento intero. Timeout di connessione in millisecondi. |
| `secure` | Con `protocol: "ftp"`, imposta `true` per FTPS esplicito. |

`uploadOnSave`, `downloadOnOpen`, i watcher e le opzioni di sincronizzazione di VS Code riguardano operazioni automatiche. Zed Transfer lavora su un singolo file alla volta e avvia le operazioni solo dopo il tuo comando. `check` segnala le opzioni inattive e restituisce un errore per modalità di connessione incompatibili, come salti SSH o opzioni TLS personalizzate.

### Prima connessione SFTP

Ottieni dal fornitore hosting l'impronta della chiave SSH del server. Apri una volta la connessione da terminale con gli stessi host e porta della configurazione.

```sh
sftp -F /dev/null -o StrictHostKeyChecking=ask -P 22 deploy@sftp.example.com
```

Confronta l'impronta mostrata con quella del fornitore prima di confermare. OpenSSH salva la chiave in `~/.ssh/known_hosts`. Se la chiave cambia, chiedi al fornitore di verificare la nuova impronta prima di aggiornare `known_hosts`. Usa nel JSON il nome reale o l'indirizzo IP del server, perché il tool usa i parametri del progetto senza leggere gli alias da `~/.ssh/config`.

Per una chiave SSH protetta da passphrase, caricala nell'agente prima di aprire Zed. Su macOS puoi usare `ssh-add --apple-use-keychain ~/.ssh/id_ed25519`. Su Linux puoi usare `ssh-add ~/.ssh/id_ed25519` con un agente attivo.

Se il server usa l'autenticazione con password, usa OpenSSH 8.4 o successivo. Imposta `"password": true` per i task di Zed o richiedi la password al momento dal terminale.

```sh
zed-transfer --password list
```

### FTPS e FTP

Per un hosting che supporta **FTP esplicito su TLS**, usa `"protocol": "ftp"`, `"secure": true` e la porta indicata dal fornitore, di solito `21`. Puoi anche usare `"protocol": "ftps"`. Il certificato deve essere valido per il nome dell'host. Il tool chiede la password nel terminale senza mostrarla mentre digiti.

Il vecchio **FTP in chiaro** espone la password e il contenuto dei file sulla rete. Per un server che offre solo quel protocollo, serve `--allow-insecure-ftp` a ogni invocazione dal terminale.

```sh
zed-transfer --allow-insecure-ftp list
```

Se devi riusare esattamente la password già contenuta nel JSON, puoi aggiungere `--allow-config-password` prima del comando. Per i progetti che usi spesso, puoi sostituire la stringa con `true` oppure passare a una chiave SSH.

## Uso in Zed

1. Apri in Zed la cartella che contiene la configurazione del progetto.
2. Apri il file su cui vuoi lavorare e usa **⌘⇧R** su macOS o **Ctrl+Shift+R** su Linux. Puoi anche cercare `task: spawn` nella palette dei comandi.
3. Cerca **Zed Transfer** e scegli **Verifica configurazione**, **Elenca file remoti**, **Carica file corrente** o **Scarica file corrente**.
4. Controlla il server e il percorso nel terminale di Zed, poi conferma. Il task di upload salva prima il file attivo. Prima di scaricare una copia già aperta in Zed, salva o scarta eventuali modifiche della scheda.

Dal terminale puoi passare il percorso del file relativo alla radice del progetto. Le cartelle che contengono il file devono esistere sia in locale sia sul server. Il comando `download` può creare un nuovo file locale.

```sh
zed-transfer list
zed-transfer upload site/wp-content/themes/mio-tema/style.css
zed-transfer download site/wp-content/themes/mio-tema/style.css
```

Se il JSON ha un oggetto `profiles`, usa `--profile` per scegliere un profilo o imposta `defaultProfile` per i task di Zed. Se contiene un array di server, usa `--name` per scegliere il server e, se serve, `--profile` per specificarne anche il profilo. Puoi passare gli stessi argomenti nei task del progetto prima del comando, per esempio `"args": ["--profile", "staging", "upload"]`.

```sh
zed-transfer --profile staging list
zed-transfer --name "WordPress staging" check
```

Se i task non trovano il comando, esegui `pipx ensurepath`, riavvia Zed e controlla che `zed-transfer --help` funzioni nel suo terminale integrato.

## Come vengono protetti i trasferimenti

- SFTP richiede una chiave host già conosciuta. FTPS verifica il certificato e cifra il canale di controllo e quello dati.
- I task avviano comandi fissi. Il percorso del file attivo viene letto dall'ambiente di Zed ed è validato come percorso del progetto.
- I controlli locali bloccano i percorsi esclusi, i collegamenti simbolici e nomi sensibili comuni come `.env`, `wp-config.php` e le chiavi private.
- Gli upload scrivono un file temporaneo con nome casuale `.zed-transfer-*.tmp`, poi lo rinominano. Per un sito web, imposta `remoteTempPath` su una cartella fuori dalla radice pubblica. Se usi la cartella di destinazione predefinita, configura il server web perché blocchi l'accesso HTTP ai file con prefisso `.zed-transfer-`.
- Quando sostituisci un file, proprietario e permessi remoti possono cambiare. I download vengono completati in un file temporaneo locale prima di sostituire la destinazione.
- Usa un account di trasferimento con accesso limitato sul server. Il confinamento remoto e la risoluzione dei collegamenti simbolici remoti dipendono dai permessi e dalla configurazione del server.

Per i dettagli delle integrazioni, vedi i [task di Zed](https://zed.dev/docs/tasks) e il [formato di configurazione SFTP per VS Code](https://github.com/Natizyskunk/vscode-sftp/wiki/Configuration).
