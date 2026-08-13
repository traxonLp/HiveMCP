# M0 — Spikes vor dem Umbau auf Session-Token-Auth

> **M0 ist abgeschlossen.** S1, S2, S3, S4 und S7 sind grün, S5 ist entschieden
> (verweigern), S6 für den OpenAPI-Pfad beantwortet. Der Umbau auf Session-Token-Auth
> ist umgesetzt.
>
> **Risiko R1 aus dem Implementierungsplan entfällt ersatzlos.** Dateien gehören
> nachweislich dem aufrufenden Nutzer; HiveMCP hält keine fremden Credentials mehr.
>
> Einzig offen: ob Session-Auth auch bei Verbindungen vom Typ `MCP (Streamable HTTP)`
> greift. Für die OpenAPI-Surface ist es bestätigt, und die trägt derzeit alles.


**Ziel:** HiveMCP wird ein reiner Renderer. Es hält keine fremden Credentials mehr,
sondern reicht das Session-Token des aufrufenden Nutzers an OpenWebUIs Files-API durch.
Damit gehört jede erzeugte Datei dem Nutzer, der sie angefordert hat.

Diese Spikes klären, was tatsächlich über die Leitung geht, bevor Code umgebaut wird.
Jeder hat ein Abbruchkriterium — schlägt einer fehl, ändert sich der Zielentwurf, nicht
nur ein Detail.

---

## Warum das die Architektur ändert

| | heute | nach dem Umbau |
|---|---|---|
| Wer authentifiziert sich? | Shared Bearer, für alle Nutzer derselbe | Der Nutzer selbst, mit seinem OpenWebUI-Session-Token |
| Woher kommt die Identität? | `X-Hive-User-Id`-Header — vertraut, spoofbar für jeden mit dem Bearer | Aus OpenWebUIs Antwort auf die Token-Validierung — nicht spoofbar |
| Wem gehört die erzeugte Datei? | dem Besitzer des Service-Keys (Risiko R1) | dem aufrufenden Nutzer |
| Secrets im Container | `HIVE_AUTH_TOKEN`, `HIVE_OWUI_API_KEY` | nur noch `HIVE_SIGNING_KEY` |

Der zweite Punkt ist der eigentliche Gewinn und geht leicht unter: heute ist die
Identität eine *Behauptung*, die HiveMCP glaubt. Danach ist sie ein *Nachweis*, den
OpenWebUI bestätigt. Damit fällt Risiko R1 aus dem Plan ersatzlos weg.

---

## S1 — Kommt das Token überhaupt an, und wie?

**Frage.** Welchen Header setzt OpenWebUI bei `auth_type: session`, und was steht drin —
ein JWT, ein Opaque-Token, mit welcher Lebensdauer?

**Vorgehen.** Diagnose-Endpunkt `/_debug/whoami` bauen (nur bei `HIVE_ENVIRONMENT=dev`),
der die eingehenden Header ausgibt. Dann in OpenWebUI die Verbindung auf
Authentifizierung **Session** stellen und aus dem Chat ein Tool aufrufen.

**Ergebnis: grün.** OpenWebUI schickt das Token als `Authorization: Bearer <JWT>` und
zusätzlich als `token`-Cookie. Der JWT trägt `id`, `jti`, `iat`, `exp`.

Nebenbei zwei Dinge geklärt, die als Annahmen im Plan standen:

- Der Aufruf kommt aus dem OpenWebUI-**Backend** (`User-Agent: Python/3.11 aiohttp`,
  `Host: host.docker.internal:8080`). Session-Auth funktioniert also auch für
  admin-konfigurierte Global Tool Servers, nicht nur für browserseitige User Tool
  Servers. Die Sorge aus S6 ist für den OpenAPI-Pfad damit erledigt.
- Die `X-Hive-*`-Header kommen parallel weiter an. Sie werden nur noch für `chat_id`
  und `model` benutzt; die Identität stammt aus der Validierung.

---

## S2 — Wie validiert HiveMCP das Token?

**Frage.** Welcher OpenWebUI-Endpunkt nimmt ein Session-Token entgegen und gibt die
Nutzeridentität zurück?

**Vorgehen.** Mit dem aus S1 gewonnenen Token gegen die Kandidaten testen:

```bash
TOKEN='...'
for p in /api/v1/auths/ /api/v1/users/user /api/v1/auths/user; do
  echo "== $p"
  curl -sS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
    "http://localhost:3000$p"
done
```

**Ergebnis: `/api/v1/auths/`** — und ein Fallstrick, der fast durchgerutscht wäre:

| Endpunkt | Status | Zeit | Inhalt |
|---|---|---|---|
| `/api/v1/auths/` | 200 | 12,8 ms | vollständig: `id`, `name`, `email`, `role` |
| `/api/v1/users/user` | 400 | 7,9 ms | — |
| `/api/v1/auths/user` | **200** | **4,0 ms** | **leer** |

Der dritte antwortet mit `200` und einem leeren Body — und ist der schnellste von allen.
Eine Prüfung nach dem Muster „200 heißt gültig" hätte ihn bevorzugt, und **jeder Aufrufer
wäre als niemand authentifiziert worden**, ohne Fehler im Log. Die Annahmebedingung im
Code ist deshalb eine brauchbare Nutzer-ID, nicht der Statuscode.

Zweiter Befund: die Antwort von `/api/v1/auths/` enthält ein Feld **`token`**, dazu
`permissions`, `date_of_birth` und `gender`. Die Payload komplett zu übernehmen hätte ein
Credential und persönliche Daten in ein Objekt gelegt, das geloggt und gecacht wird. Der
Code kopiert genau vier Felder heraus.

13 ms pro Aufruf rechtfertigen den Cache (60 s, Schlüssel ist der SHA-256 des Tokens —
nie das Token selbst), machen ihn aber nicht kritisch.

---

## S3 — Gehört die Datei danach dem richtigen Nutzer? (löst R1)

**Frage.** Ein Upload mit dem Session-Token statt dem Service-Key — landet die Datei in
der Dateiliste des Nutzers?

**Vorgehen.** Zwei Nutzer anlegen. Mit dem Token von Nutzer B hochladen:

```bash
curl -sS -X POST -H "Authorization: Bearer $TOKEN_B" \
  -F 'file=@/tmp/test.pptx' 'http://localhost:3000/api/v1/files/?process=false'
```

Dann in der Oberfläche als B **und** als A nachsehen.

**Ergebnis: grün.** Der Diagnose-Endpunkt `hive_debug_upload_check` lädt eine Testdatei
mit dem Session-Token des Aufrufers hoch, liest sie zurück und vergleicht das
`user_id`-Feld der Datei mit der validierten Identität: `owner_matches_caller: true`.

Bewusst nicht über die Dateiliste geprüft: der Testnutzer ist Admin, und „die Datei ist
sichtbar" unterscheidet nicht zwischen *gehört mir* und *Admin sieht alles*. Die auf der
Datei gespeicherte Besitzer-ID ist der Nachweis.

**Damit ist Risiko R1 erledigt.** HiveMCP ist ein reiner Renderer.

---

## S4 — Hält das Token lange genug?

**Frage.** Läuft das Token während einer langen Generierung ab? Ein Brief-Modus-Aufruf
mit Modell-Roundtrip und Rendering kann Minuten dauern.

**Vorgehen.** Ablaufzeitpunkt aus dem Token lesen (JWT: Payload dekodieren, `exp`), und
prüfen, ob OpenWebUI bei jedem Aufruf ein frisches schickt oder eines wiederverwendet.

**Ergebnis: kein Thema.** Gemessene Restlaufzeit **651 h 59 m**, also rund 27 Tage. Selbst
eine Brief-Generierung mit mehreren Modell-Roundtrips liegt Größenordnungen darunter.

Damit entfällt die geplante Restlaufzeit-Prüfung vor dem Upload. Der Fall bleibt trotzdem
behandelt, aber als normaler Fehler: der Files-Client meldet bei `401`/`403` „die Sitzung
ist möglicherweise abgelaufen, frag nochmal", statt einen generischen Fehlschlag zu
melden.

---

## S5 — Was passiert ohne Token?

**Frage.** Es gibt Kontexte, in denen kein Nutzer-Token existiert: geplante Aufgaben,
direkte API-Aufrufe, Titel- und Tag-Generierung. Für OAuth-Token-Forwarding ist genau
das ein dokumentiertes Problem.

**Vorgehen.** Tool über `POST /api/chat/completions` mit `tool_ids` direkt per API
aufrufen, ohne Chat-Kontext, und beobachten was ankommt.

**Erfolgskriterium.** Kein Zweifel darüber, ob ein Token kommt oder nicht.

**Entwurfsentscheidung, die daran hängt.** Bei fehlendem Token entweder sauber
verweigern („dieses Tool braucht eine Chat-Sitzung"), oder auf einen optionalen
Service-Key zurückfallen. Ich neige zum Verweigern: ein stiller Fallback, der Dateien
wieder dem Service-Account zuordnet, bringt R1 durch die Hintertür zurück.

---

## S6 — Session-Auth bei MCP oder nur bei OpenAPI?

**Frage.** Der Auth-Typ **Session** ist im OpenAPI-Verbindungsdialog sichtbar. Gilt er
auch für Verbindungen vom Typ `MCP (Streamable HTTP)`?

**Vorgehen.** Beide Verbindungstypen parallel eintragen, je einen Aufruf, Header
vergleichen.

**Warum das zählt.** Wenn Session-Auth nur bei OpenAPI funktioniert, wird die
OpenAPI-Surface der Hauptweg und MCP bleibt das Protokoll-Zugeständnis für andere
Clients. Das wäre kein Beinbruch — die Rich-UI-Oberfläche für M5 braucht ohnehin OpenAPI
— aber es verschiebt, welche Surface zuerst gepflegt wird.

---

## S7 — Rendert OpenWebUI unser iframe? (offenes R2)

**Frage.** Liefert ein Tool mit `Content-Disposition: inline` eine Rich-UI-Karte, wie die
Doku beschreibt?

**Vorgehen.** Einen Wegwerf-Endpunkt bauen, der 20 Zeilen HTML mit dem
`postMessage`-Höhenskript zurückgibt, und über die OpenAPI-Verbindung aufrufen.

**Ergebnis: grün.** OpenWebUI rendert die Karte als iframe im Chat. M5 ist tragfähig, und
die Konfigurations-GUI kann wie geplant über die OpenAPI-Surface laufen.

---

## Reihenfolge und Aufwand

| Spike | hängt ab von | Aufwand | Blockiert |
|---|---|---|---|
| S1 Token kommt an | Diagnose-Endpunkt | 30 min | alles Weitere |
| S2 Validierung | S1 | 30 min | Auth-Umbau |
| S3 Datei-Zuordnung | S1 | 30 min | R1, Delivery-Umbau |
| S4 Lebensdauer | S1 | 20 min | Fehlerbehandlung |
| S5 kein Token | S1 | 30 min | Fallback-Entscheidung |
| S6 MCP vs OpenAPI | S1 | 20 min | Surface-Priorität |
| S7 Rich UI | — | 30 min | M5 |

S1 bis S3 sind der Kern: danach steht fest, ob der Umbau trägt. S4 bis S6 verfeinern die
Fehlerbehandlung, S7 ist unabhängig und kann parallel laufen.

---

## Was sich danach ändert (Vorschau, kein Beschluss)

- `hivemcp/auth.py` — statt Bearer-Vergleich eine Token-Validierung gegen OpenWebUI, mit
  kurzlebigem Cache. `Identity` kommt aus der Validierungsantwort statt aus Headern.
- `hivemcp/core/files/owui_client.py` — Token pro Aufruf statt Client mit festem Key.
  Der `httpx.AsyncClient` verliert seinen `Authorization`-Default.
- `hivemcp/core/delivery.py` — `OwuiDelivery` bekommt das Token durchgereicht.
  `CompositeDelivery` wird vermutlich überflüssig, sobald S3 grün ist.
- `hivemcp/config.py` — `HIVE_OWUI_API_KEY` und `HIVE_AUTH_TOKEN` werden optional bzw.
  entfallen; `HIVE_OWUI_URL` bleibt und wird wichtiger, weil dagegen validiert wird.
- Plan-Entscheidung **D6** (Bearer + Identitäts-Header) wird von dieser Lösung abgelöst;
  Risiko **R1** entfällt.

**Nicht vorab umbauen.** Alle sechs Codestellen hängen an S1 bis S3. Der Diagnose-
Endpunkt ist das Einzige, was vor den Spikes gebaut werden sollte.
