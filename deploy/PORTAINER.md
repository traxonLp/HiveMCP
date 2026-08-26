# HiveMCP als Portainer-Stack

Für den Fall: OpenWebUI läuft bereits als eigener Stack in Portainer, HiveMCP kommt als
zweiter Stack daneben und wird von Portainer aus dem Git-Repository gebaut.

## 1. Netzwerknamen herausfinden

Das ist der Schritt, an dem es sonst scheitert. Zwei Stacks teilen sich standardmäßig
kein Netzwerk — ohne diesen Schritt löst der OpenWebUI-Servicename in HiveMCP nicht auf,
und weil jede Anfrage mit einer Token-Prüfung gegen OpenWebUI beginnt, schlägt danach
buchstäblich alles fehl.

In Portainer unter **Networks** nachsehen. Der Name folgt dem Muster
`<stackname>_<netzwerkname>` — ein Stack namens `openwebui` ohne eigene Netzwerkdefinition
ergibt `openwebui_default`.

Gleich mit notieren: den **Servicenamen** des OpenWebUI-Containers (unter **Containers**,
oft `open-webui`) und dass dessen interner Port **8080** ist — nicht der veröffentlichte
Host-Port, den du im Browser benutzt.

## 2. Signaturschlüssel erzeugen

```
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Einmal erzeugen und behalten. Ohne ihn erfindet jeder Start einen neuen, und alle vorher
ausgegebenen Download-Links hören auf zu funktionieren.

## 3. Stack anlegen

**Stacks → Add stack → Repository**

| Feld | Wert |
|---|---|
| Name | `hivemcp` |
| Repository URL | dein HiveMCP-Repo |
| Repository reference | `refs/heads/main` |
| Compose path | `portainer-stack.yml` |

Die Compose-Datei liegt im Wurzelverzeichnis, nicht in `deploy/`. Das ist kein
Ordnungsgeschmack: Compose löst `build.context` relativ zur Compose-Datei auf, aus
`deploy/` heraus müsste der Kontext also `..` sein — und diesen Pfad setzt Portainer
falsch zusammen. Es verschluckt die Stack-ID und bricht ab mit
`resolve : lstat /data/compose/deploy: no such file or directory`. Vom Wurzelverzeichnis
aus ist der Kontext `.`, es gibt nichts hochzuklettern.

Darunter bei **Environment variables** anlegen:

| Variable | Beispiel | Anmerkung |
|---|---|---|
| `HIVE_OWUI_URL` | `http://open-webui:8080` | Servicename + **interner** Port |
| `HIVE_SIGNING_KEY` | *(aus Schritt 2)* | |
| `HIVE_PUBLIC_URL` | `http://192.168.1.50:8080` | Adresse, die **Nutzer** im Browser erreichen |
| `OWUI_NETWORK` | `openwebui_default` | exakt aus Schritt 1 |
| `HIVE_PORT` | `8080` | optional, falls 8080 auf dem Host belegt ist |
| `HIVEMCP_TAG` | `1.0.4` | optional, Default `latest` |
| `HIVE_DELIVERY_MODE` | `both` | optional, siehe unten |

### Auslieferungsweg

`HIVE_DELIVERY_MODE` bestimmt, wie eine fertige Datei beim Nutzer landet.

`both` (Default) lädt sie in die OpenWebUI-Dateien des Aufrufers **und** erzeugt
zusätzlich einen signierten Link auf HiveMCP. Der einzige Modus, in dem ein
fehlgeschlagener Upload das Dokument trotzdem noch ausliefert.

`owui` lädt nur hoch. Nichts wird auf das Artefakt-Volume geschrieben, und HiveMCP muss
für keinen Browser erreichbar sein — `HIVE_PUBLIC_URL` verliert damit seine Bedeutung und
der Port muss nicht mehr veröffentlicht werden. Preis: schlägt der Upload fehl, ist das
gerenderte Dokument verloren.

`link` erzeugt nur den signierten Link; die Datei taucht in der Dateiliste gar nicht auf.

Für `owui` kommt eine zweite Variable dazu: **`HIVE_OWUI_PUBLIC_URL`**, die Adresse, unter
der ein *Browser* OpenWebUI erreicht — etwa `http://192.168.1.50:3000`. Nicht zu
verwechseln mit `HIVE_OWUI_URL`, das der Containername ist und im Browser nichts
auflöst. Bleibt sie leer, werden Links aus `HIVE_OWUI_URL` gebaut und führen ins Leere;
das Log sagt das beim Start.

Der Grund für die zweite Variable: die Download-Karte läuft als cross-origin iframe. Ein
relativer Link darin löst gegen HiveMCPs Adresse auf, nicht gegen OpenWebUI — der Button
liefe auf einen 404. Deshalb absolute URL.

Vor dem Umstieg auf `owui` einmal prüfen, ob ein Download-Link ohne Bearer-Header
funktioniert: im angemeldeten Browser `http://<owui>/api/v1/files/<file_id>/content`
aufrufen. Lädt die Datei, ist der Link klickbar; kommt 401, liegt die Datei zwar korrekt
in der Dateiliste, aber der Button im Chat funktioniert nicht.

`HIVE_PUBLIC_URL` ist der Wert, der am häufigsten falsch gesetzt wird. Daraus werden die
Download-Links gebaut. Steht dort `localhost`, funktionieren sie nur auf dem Docker-Host
selbst und für niemanden sonst — und der Fehler taucht im Browser des Nutzers auf, nicht
in irgendeinem Log.

Dann **Deploy the stack**. Es wird nichts gebaut — der Stack zieht
`ghcr.io/traxonlp/hivemcp`, das GitHub Actions bei jedem Push auf `main` neu
veröffentlicht.

**Beim allerersten Mal:** GHCR-Pakete sind nach dem ersten Push privat. Auf GitHub unter
**Packages → hivemcp → Package settings → Change visibility → Public** umstellen, sonst
scheitert der Pull mit `denied`. Wenn das Paket privat bleiben soll, stattdessen in
Portainer unter **Registries** eine Registry `ghcr.io` mit einem Personal Access Token
(Scope `read:packages`) hinterlegen.

## 4. Prüfen

```
curl http://<host>:8080/healthz    # {"status":"ok"}
curl http://<host>:8080/readyz     # checks: storage/templates ok, openwebui configured
```

`/readyz` schreibt testweise in beide Volumes. Steht dort `unwritable`, stimmt etwas mit
den Volumes nicht — bei benannten Volumes praktisch nie, bei Bind-Mounts fast immer.

Im Container-Log steht eine Startzeile mit der geladenen Konfiguration, inklusive
`skills=hivemcp-usage`. Steht dort `skills=NONE FOUND`, ist die Anleitung nicht ins Image
gelangt; alles andere funktioniert dann, aber das Modell lernt nie, wie es die Tools
benutzen soll.

Erreichbarkeit in die andere Richtung, aus HiveMCP heraus:

```
docker exec hivemcp-hivemcp-1 python -c \
  "import urllib.request;print(urllib.request.urlopen('$HIVE_OWUI_URL/health').status)"
```

Scheitert das mit einem Namensauflösungsfehler, ist `OWUI_NETWORK` falsch oder der
Servicename stimmt nicht.

## 5. In OpenWebUI verbinden

**Admin Settings → Integrations →** neue Verbindung:

- **MCP (Streamable HTTP)** → `http://hivemcp:8080/mcp`
- oder **OpenAPI** → `http://hivemcp:8080/openapi.json`

Hier den **Servicenamen** benutzen, nicht `HIVE_PUBLIC_URL`: diese Anfrage kommt aus dem
OpenWebUI-Container, nicht aus dem Browser.

Die Authentifizierung dieser Verbindung auf **Session** stellen. Das ist das gesamte
Sicherheitsmodell: HiveMCP hat keine eigenen Zugangsdaten und arbeitet mit dem Token des
Aufrufers, damit erzeugte Dateien dem Nutzer gehören, der sie angefordert hat. Ohne diese
Einstellung antwortet jeder Aufruf mit einem Hinweis darauf.

Die OpenAPI-Verbindung ist die, die Konfigurations-GUI und Download-Karte als eingebettete
Karten darstellen kann — über MCP geht das nicht, weil OpenWebUIs Event-System dort nicht
zur Verfügung steht. Die Tools sind sonst identisch.

## Aktualisieren

**Stacks → hivemcp → Pull and redeploy.** Portainer zieht das aktuelle Image; gebaut wird
nichts. Bei `HIVEMCP_TAG=latest` bekommst du damit den Stand des letzten Pushs auf `main`.

Willst du kontrollieren, welche Version läuft, setz `HIVEMCP_TAG` auf eine konkrete
Nummer aus den GitHub-Releases. Ein Rollback ist dann nur eine geänderte Variable und ein
Redeploy — mit `latest` gibt es nichts, wohin man zurückrollen könnte.

Die Volumes bleiben dabei erhalten — auch die Templates. Willst du die wirklich löschen,
muss das Volume `hivemcp_hivemcp-templates` von Hand entfernt werden.

## Wenn etwas nicht geht

**`network <name> not found` beim Deploy** — `OWUI_NETWORK` stimmt nicht. Unter Networks
nachsehen, exakt übernehmen.

**Jeder Toolaufruf meldet, er brauche eine OpenWebUI-Sitzung** — die Authentifizierung
der Verbindung steht nicht auf Session.

**Modell schreibt den Folientext in den Chat statt eine Datei zu erzeugen** — HiveMCP ist
in diesem Chat nicht aktiviert. Die Verbindung muss im Chat unter den Werkzeugen
eingeschaltet sein; sonst hat das Modell die Tools gar nicht.

**Download-Links öffnen nichts** — `HIVE_PUBLIC_URL` zeigt auf eine Adresse, die der
Browser nicht erreicht.

**Template-Upload antwortet mit einem Rechtefehler** — nur Administratoren dürfen
Vorlagen hinterlegen, das ist beabsichtigt. Der Pool ist geteilt.
