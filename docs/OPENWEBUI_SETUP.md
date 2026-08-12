# HiveMCP in OpenWebUI einbinden

Voraussetzung: OpenWebUI **≥ 0.6.31** (davor gibt es keine native MCP-Unterstützung) und
ein **Admin-Account**. MCP-Server dürfen ausschließlich Admins anlegen; normale Nutzer
können nur OpenAPI-Server hinzufügen.

---

## Schritt 0 — Läuft beides?

```bash
make owui      # startet HiveMCP + OpenWebUI zusammen
```

- HiveMCP: <http://localhost:8080/healthz> → `{"status":"ok"}`
- OpenWebUI: <http://localhost:3000>

Hast du bereits eine eigene OpenWebUI-Instanz, reicht `make up` für HiveMCP allein.

---

## Schritt 1 — OpenWebUI-API-Key hinterlegen

**Ohne diesen Schritt bekommst du nur Download-Links statt Dateien im Chat.** HiveMCP
braucht den Key, um erzeugte Dokumente über die Files-API hochzuladen.

**1a. Master-Schalter prüfen.** API-Keys hängen an einem globalen Toggle. Ist er aus,
erscheint der Bereich in den Account-Settings gar nicht — das sieht nach einem fehlenden
Feature aus, ist aber nur ein deaktiviertes.

**Settings → Admin → System → General → Authentication → API Keys** einschalten, speichern.

Im mitgelieferten Compose ist das über `ENABLE_API_KEYS: "true"` schon voreingestellt.

**1b. Key erzeugen.**

1. Profilbild unten links → **Settings** → **Account**
2. Abschnitt **API Keys** → **Generate New API Key**
3. Namen vergeben, z. B. `hivemcp`
4. **Sofort kopieren** — der Key lässt sich später nicht mehr anzeigen. Verloren heißt
   löschen und neu erzeugen.

**1c. Eintragen — in `.env`, nicht ins Compose.** `deploy/docker-compose.yml` wird
committet; `.env` ist per `.gitignore` ausgeschlossen.

```bash
make env        # legt .env aus der Vorlage an, inkl. frischem HIVE_SIGNING_KEY
```

Dann in `.env` im Repo-Wurzelverzeichnis:

```bash
HIVE_OWUI_API_KEY=sk-dein-key-hier
```

Neu starten:

```bash
docker compose -f deploy/docker-compose.yml up -d hivemcp
```

**1d. Prüfen** — im Log muss jetzt `owui=configured` statt `owui=not configured` stehen:

```bash
make logs
```

> **Wem gehört die Datei?** Ein API-Key handelt *als der Nutzer, der ihn erzeugt hat*, und
> erbt dessen Rollen und Gruppenrechte. Hochgeladene Dokumente landen also in **deiner**
> Dateiliste, nicht in der des jeweils chattenden Nutzers. Für eine Einzelplatz-Installation
> ist das genau richtig. Sobald mehrere Leute HiveMCP nutzen, ist es Risiko R1 aus dem
> Implementierungsplan — deshalb liefert HiveMCP immer zusätzlich einen signierten
> Download-Link mit aus.

---

## Schritt 2 — Server eintragen

**Admin Settings → Integrations → + (Add Server)**

| Feld | Wert |
|---|---|
| **Type** | `MCP (Streamable HTTP)` |
| **Server URL** | siehe Tabelle unten |
| **Auth** | `None` |
| **Headers** | JSON aus Schritt 3 |
| **Function Name Filter List** | leer lassen |

### Die URL — hier scheitern die meisten Versuche

Entscheidend ist: **OpenWebUI baut die Verbindung serverseitig auf**, aus seinem eigenen
Container heraus. Eine URL, die in deinem Browser funktioniert, funktioniert dort meistens
*nicht*. `http://localhost:8080` zeigt aus dem OpenWebUI-Container auf OpenWebUI selbst.

| Setup | MCP-URL | OpenAPI-URL |
|---|---|---|
| Beide über `make owui` (ein Compose-Netz) | `http://hivemcp:8080/mcp/` | `http://hivemcp:8080` |
| **Getrennte Container** | `http://host.docker.internal:8080/mcp/` | `http://host.docker.internal:8080` |
| OpenWebUI nativ auf dem Host | `http://localhost:8080/mcp/` | `http://localhost:8080` |

Zwei Details, die je einen Fehlversuch kosten:

- **MCP braucht den abschließenden Slash**, OpenAPI **keinen Pfad** — OpenWebUI hängt
  `/openapi.json` selbst an.
- Bei getrennten Containern muss HiveMCP Port 8080 auf dem Host veröffentlichen. Das tut
  das mitgelieferte Compose (`ports: 8080:8080`).

### Vorher prüfen statt raten

Die Verbindung erreicht HiveMCP nur, wenn dieser Aufruf **aus dem OpenWebUI-Container
heraus** klappt:

```bash
docker ps --format '{{.Names}}\t{{.Ports}}'          # Containernamen finden
docker exec <openwebui-container> \
  python -c "import urllib.request;print(urllib.request.urlopen('http://host.docker.internal:8080/healthz').read())"
```

Kommt `{"status":"ok"}`, ist die URL richtig und ein Fehler liegt woanders. Kommt ein
Verbindungsfehler, hilft kein Herumprobieren im Dialog — dann stimmt das Netzwerk nicht.

**Die Gegenrichtung ebenfalls prüfen.** HiveMCP muss OpenWebUI erreichen, sonst kann es
keine Dateien hochladen. Das ist eine *andere* Einstellung, `HIVE_OWUI_URL` in `.env`:

```bash
docker compose -f deploy/docker-compose.yml exec hivemcp \
  python -c "import os,urllib.request;print(urllib.request.urlopen(os.environ['HIVE_OWUI_URL']+'/health').status)"
```

Zu **Auth**: `None` ist richtig, solange `HIVE_AUTH_TOKEN` nicht gesetzt ist. Wählst du
`Bearer` ohne Key auszufüllen, sendet OpenWebUI einen leeren `Authorization`-Header, und
das lehnen die meisten Server sofort ab. Erst wenn du in der Compose-Datei ein Token
setzt, stellst du hier auf `Bearer` um und trägst denselben Wert ein.

---

## Schritt 3 — Identitäts-Header

Ins **Headers**-Feld (JSON-Objekt). OpenWebUI ersetzt die Tokens serverseitig:

```json
{
  "X-Hive-User-Id": "{{USER_ID}}",
  "X-Hive-User-Email": "{{USER_EMAIL}}",
  "X-Hive-Groups": "{{USER_GROUPS}}",
  "X-Hive-Chat-Id": "{{CHAT_ID}}"
}
```

Daraus zieht HiveMCP das Rate-Limit pro Nutzer und später die Template-Sichtbarkeit.
Weglassen funktioniert auch — dann gilt jeder Aufruf als `anonymous` und teilt sich ein
gemeinsames Rate-Limit.

**`X-Hive-Chat-Id` ist wichtiger, als es aussieht.** Sobald du den Brief-Modus
einschaltest (`HIVE_LLM_ENABLED=true`), ist das die einzige Möglichkeit, wie HiveMCP
herausfindet, *welches Modell du im Chat ausgewählt hast* — OpenWebUI liefert einem
externen Tool-Server weder ein `{{MODEL}}`-Token noch das `__model__`-Argument. Ohne
diesen Header verweigert HiveMCP die Brief-Expansion, statt heimlich ein anderes Modell
zu nehmen.

Optional lässt sich ein Modell festnageln:

```json
{ "X-Hive-Model": "qwen3:32b" }
```

Das überschreibt die Chat-Erkennung. Praktisch, um Briefings gezielt auf ein starkes
Modell zu legen — dann steht in den `warnings` des Ergebnisses aber, dass nicht das im
Chat gewählte Modell geantwortet hat.

Dann **Save**. Falls OpenWebUI einen Neustart verlangt, neu starten.

---

## Schritt 4 — Im Chat aktivieren

Neuen Chat öffnen → **+** neben dem Eingabefeld → **Integrations** → **Tools** →
HiveMCP einschalten.

Die drei Tools sollten auftauchen:

- `hive_create_presentation`
- `hive_create_document`
- `hive_create_spreadsheet`

---

## Schritt 5 — Testen

```
Erstelle mir eine PowerPoint mit 4 Folien über den Aufbau von HiveMCP:
Titelfolie, eine Folie mit den drei Dateiformaten als Bullets,
eine Tabelle mit Komponente/Port, und eine Abschlussfolie.
```

Erwartung: das Modell ruft `hive_create_presentation` mit einer vollständigen `spec`
auf, und du bekommst eine `.pptx` zurück.

---

## Wenn es nicht klappt

| Symptom | Ursache | Lösung |
|---|---|---|
| „Failed to connect to MCP server", obwohl **Verify Connection** grün war | `Bearer` ohne Key gewählt | Auf `None` stellen |
| Verbindungsfehler bei leerer Filterliste | bekannter Parsing-Bug in OpenWebUI | Ein einzelnes Komma (`,`) in **Function Name Filter List** eintragen |
| Frontend hängt in Endlos-Ladeschleife nach dem Speichern | MCP-Config in eine **OpenAPI**-Verbindung eingetragen | Verbindung in den Admin-Settings deaktivieren, Seite mit Strg+F5 neu laden, mit Type `MCP` neu anlegen |
| 404 auf dem Endpunkt | Slash am Ende der URL fehlt | `…/mcp/` statt `…/mcp` |
| Timeout beim Verbinden | Handshake langsamer als 10 s | `MCP_INITIALIZE_TIMEOUT` in OpenWebUI hochsetzen |
| Modell ruft das Tool nie auf | schwaches Modell, das kein verschachteltes JSON baut | Größeres Modell testen; die Specs sind tief verschachtelt |
| Datei kommt als Link statt als Anhang | `HIVE_OWUI_API_KEY` fehlt | Schritt 1 |
| Validierungsfehler zu unbekannten Feldern | Modell erfindet Felder | Kein Bug — die Specs sind bewusst strikt, das Modell soll die Meldung lesen und korrigieren |

Logs mitlesen, während du testest:

```bash
make logs
```

---

## Optional: OpenAPI-Surface zusätzlich

Derselbe Dialog, **Type: `OpenAPI`**, URL ohne Pfad: `http://hivemcp:8080`.

Beide parallel einzutragen ist vorgesehen. Der OpenAPI-Pfad ist der einzige, über den
OpenWebUI Rich-UI-iframes rendert — den braucht die Konfigurations-GUI in M5. Trage
solange nur eines von beiden ein, sonst sieht das Modell jedes Tool doppelt.
