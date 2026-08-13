# HiveMCP — Implementierungsplan

**Status:** Entwurf v1 · **Datum:** 2026-08-12 · **Zielumgebung:** OpenWebUI ≥ 0.6.31, Kubernetes

HiveMCP ist ein Python-Service, der OpenWebUI die Erzeugung und Bearbeitung von PowerPoint-,
Word- und Excel-Dateien beibringt — template-gestützt, über eine iframe-GUI konfigurierbar,
mit Zugriff auf Dateien aus dem Chat, und als Container in Kubernetes betreibbar.

---

## 1. Entscheidungen

| # | Entscheidung | Gewählt | Begründung |
|---|---|---|---|
| D1 | Integrationspfad | **MCP + OpenAPI aus einer App** | OpenWebUI rendert Rich-UI-iframes nur, wenn ein Tool-Ergebnis den Header `Content-Disposition: inline` trägt. Dieser Pfad existiert für native Python-Tools und für **OpenAPI**-Tool-Server — der native MCP-Pfad hat das UI-Event-System **nicht** (explizit in den Docs vermerkt). „MCP Apps" (`ui://`-Resources via `_meta.ui.resourceUri`) sind bisher nur Discussion, nicht Core. Also: MCP für Protokoll-Kompatibilität, OpenAPI für Anforderung 5. |
| D2 | Content-Erzeugung | **Hybrid, immer mit dem im Chat gewählten Modell** | Default: das OWUI-Modell liefert eine vollständige JSON-Spec, HiveMCP rendert deterministisch. Bei reinem Briefing ruft HiveMCP OpenWebUIs eigene `/api/chat/completions` mit **dem Modell zurück, das der Nutzer ausgewählt hat** — kein zweiter Endpoint, kein zweiter Key. Nur so bekommen GUI-Parameter wie *Zielgruppe* und *Seitenzahl* Wirkung, ohne dass hinter dem Rücken des Nutzers ein anderes Modell antwortet. Siehe D9 zur Modell-Auflösung. |
| D3 | Datei-Rückgabe | **OpenWebUI Files-API** | Datei landet nativ in der Dateiverwaltung des Nutzers. ⚠️ Ownership-Risiko, siehe R1. |
| D4 | Storage | **PVC (ReadWriteMany)** | Kein Object-Store nötig. Setzt RWX-fähige StorageClass voraus (NFS/CephFS/Azure Files). |
| D5 | Templates | **Upload + Platzhalter-Syntax** | Nutzer laden eigene `.potx/.dotx/.xltx` (oder `.pptx/.docx/.xlsx`) hoch; HiveMCP inspiziert Layouts/Styles/Named-Ranges und exponiert sie als Schema. Neue Corporate Designs ohne Codeänderung. |
| D6 | Auth | ~~Bearer-Token + Identität via Header~~ → **OpenWebUI-Session-Token** | *Abgelöst nach M0.* OpenWebUI leitet bei Auth-Typ `Session` das Session-Token des angemeldeten Nutzers weiter. HiveMCP validiert es gegen `/api/v1/auths/` und nutzt es anschließend gegenüber der Files-API. Zwei Gewinne: die Identität ist ein **Nachweis** statt einer Header-Behauptung, die jeder mit dem Shared Secret fälschen könnte; und erzeugte Dateien gehören dem Nutzer, der sie angefordert hat. Der Container hält damit gar keine fremden Credentials mehr — nur noch `HIVE_SIGNING_KEY`. Kein Token, kein Dienst: Kontexte ohne Chat-Sitzung werden abgelehnt, statt still auf ein Dienstkonto zurückzufallen. Details in `docs/M0_SPIKES.md`. |
| D7 | GUI-Flow | **Formular → `input:prompt:submit`** *(vereinfacht nach S7)* | Das Formular schickt die gewählten Parameter per `postMessage` als Folge-Prompt in den Chat; das Modell ruft daraufhin `hive_create_*` auf. Funktioniert **ohne** `allowSameOrigin` (nur ein Bestätigungsdialog). **Nach S7 vereinfacht:** das HTML reist im Tool-Ergebnis selbst, es gibt also weder eine `/ui/`-Route noch signierte Links. Preis: die Karte ist vollständig sandboxed und kann nichts nachladen — alles, was sie braucht, wird beim Rendern eingebettet. **Theme:** kein API-Kanal nötig, `prefers-color-scheme` spiegelt im iframe das `color-scheme` des Einbetters (CSSWG-Beschluss, auch cross-origin); Voraussetzung ist `color-scheme: light dark` in der Karte, sonst bekommt sie eine opake Canvas. **Sprache:** liegt wie das Theme clientseitig, kommt daher als Tool-Parameter vom Modell. |
| D8 | Skill | **`SKILL.md` als Nutzungsanleitung** | Eine Skill, die dem Modell erklärt, *wie* HiveMCP zu bedienen ist: wann welches Tool, wie eine gute Spec aussieht, wie Templates befüllt werden. Ausgeliefert über drei Kanäle (siehe §6). |
| D9 | Modell-Auflösung | **Header → Chat-Lookup → Fallback** | OpenWebUI verrät einem externen Tool-Server das gewählte Modell nicht: es gibt kein `{{MODEL}}`-Template, und `__model__` erreichen nur native Python-Tools. Kette: (1) `X-Hive-Model`, falls ein Admin eines festnagelt; (2) `GET /api/v1/chats/{{CHAT_ID}}` — der Pfad, der der Auswahl des Nutzers tatsächlich folgt; (3) `HIVE_LLM_FALLBACK_MODEL`. Stufe 3 ist **standardmäßig leer**: lieber ein Fehler, der es sagt, als still ein anderes Modell als das gewählte. Weicht die Quelle von (2) ab, hängt HiveMCP eine Warnung ans Ergebnis. |

### Verworfene Alternativen

- **Nur MCP.** Scheitert an Anforderung 5: keine Inline-iframes über den MCP-Pfad.
- **Nur OpenAPI.** Erfüllt alles, aber der Server wäre kein MCP-Server — nicht mit Claude/Cursor/anderen MCP-Clients nutzbar, und der Projektname wäre irreführend.
- **SQLite auf dem RWX-PVC.** Datei-Locking über NFS ist unzuverlässig; bei mehreren Replicas Korruptionsrisiko. Stattdessen: `meta.json` pro Template + In-Memory-Cache mit mtime-Invalidierung. Kein DB-Dependency in v1.
- **LibreOffice im Haupt-Image** (für PDF-Export). +~700 MB Image-Größe für ein Feature, das nicht gefordert ist. Später als optionales Profil / Sidecar.

---

## 2. Systemüberblick

```
┌──────────────────────────── OpenWebUI ─────────────────────────────┐
│                                                                    │
│  Admin → Integrations                                              │
│    ├─ Typ "MCP (Streamable HTTP)" → https://hive/mcp               │
│    └─ Typ "OpenAPI"               → https://hive/openapi.json      │
│  Headers: {"Authorization":"Bearer ***",                           │
│            "X-Hive-User-Id":"{{USER_ID}}",                         │
│            "X-Hive-User-Email":"{{USER_EMAIL}}",                   │
│            "X-Hive-Groups":"{{USER_GROUPS}}",                      │
│            "X-Hive-Chat-Id":"{{CHAT_ID}}"}                         │
│                                                                    │
│  Chat  ──tool call──▶ HiveMCP ──Files-API──▶ zurück in den Chat    │
│  Chat  ◀──iframe─────  /ui/config?t=<signed>                       │
└────────────────────────────────────────────────────────────────────┘
                                 │
┌──────────────────────────── HiveMCP (FastAPI) ─────────────────────┐
│  Surfaces                                                          │
│    /mcp             MCP Streamable HTTP (stateless)                │
│    /tools/*         OpenAPI-Tool-Endpunkte (HTMLResponse-fähig)    │
│    /ui/*            Konfigurations-GUI (server-rendered, signiert) │
│    /healthz /readyz /metrics                                       │
│    /skills/*        SKILL.md als Text/Markdown                     │
│                                                                    │
│  Core (surface-agnostisch)                                         │
│    models · render/{pptx,docx,xlsx} · templates/{store,inspect,fill}│
│    editing/* · files/owui_client · llm/expand (optional)           │
│                                                                    │
│  PVC (RWX) /data/{templates,tmp,audit}                             │
└────────────────────────────────────────────────────────────────────┘
```

**Kernprinzip:** MCP- und OpenAPI-Surface sind dünne Adapter über *demselben* Core.
Jede Fachlogik lebt in `core/` und kennt weder FastAPI noch MCP. Das hält die beiden
Surfaces automatisch synchron und macht den Core unit-testbar ohne HTTP.

**Stateless MCP:** Der Transport läuft im *stateless* Modus — `FastMCP(stateless_http=True)`,
gemountet als `mcp.streamable_http_app()` unter `/mcp`. Sonst bräuchten mehrere Replicas
Session-Affinität am Ingress. Alle Zustände liegen im PVC oder im Request.

⚠️ Der Lifespan des Session-Managers muss explizit in den FastAPI-Lifespan gehängt werden
(`AsyncExitStack` um `mcp.session_manager.run()`). Wird das vergessen, antwortet der Mount
still mit 404/500 — bekannte Stolperfalle (python-sdk #1367). Gehört in M1 mit einem Test abgesichert.

---

## 3. Modulstruktur

```
hivemcp/
  app.py                    # FastAPI-Factory, mountet alle Surfaces
  config.py                 # pydantic-settings, alles über HIVE_* Env
  auth.py                   # Bearer-Check (konstante Zeit), Identity aus Headern,
                            # HMAC-Signierung für /ui-Links
  surfaces/
    mcp_server.py           # MCP tools/list + tools/call → core
    openapi_tools.py        # REST-Endpunkte → core, HTMLResponse wo sinnvoll
    ui.py                   # /ui/config, /ui/templates
    skills.py               # /skills/{name}
  core/
    models.py               # DeckSpec, DocSpec, SheetSpec, RenderOptions, EditOp
    render/
      pptx.py  docx.py  xlsx.py
      theme.py              # Font-/Farb-/Master-Anwendung, Einheiten-Helfer
    templates/
      store.py              # PVC-Repo: list/get/put/delete, Sichtbarkeit
      inspect.py            # Layouts, Placeholders, Styles, Named Ranges, {{vars}}
      fill.py               # Platzhalter-Befüllung
    editing/
      pptx_edit.py  docx_edit.py  xlsx_edit.py
      ops.py                # EditOp-Dispatcher
    files/
      owui_client.py        # OpenWebUI Files-API (get content, upload)
      workdir.py            # Per-Request-Tempdir mit garantiertem Cleanup
    llm/
      client.py             # OpenAI-kompatibel, optional
      expand.py             # brief + options → Spec (structured output)
  skills/
    hivemcp-usage/SKILL.md
deploy/
  Dockerfile
  k8s/base/                 # Deployment, Service, PVC, Ingress, ConfigMap, Secret
  k8s/overlays/{dev,prod}/
  docker-compose.yml        # OWUI + HiveMCP für lokale Integrationstests
tests/
```

**Bibliotheken:** `python-pptx`, `python-docx` + `docxtpl` (Jinja2-über-docx, passt exakt
zur Platzhalter-Syntax aus D5), `openpyxl` (inkl. Charts, `keep_vba` für `.xlsm`),
`mcp` (offizielles SDK), `fastapi`, `pydantic-settings`, `httpx`, `jinja2`.
Build/Deps mit `uv`.

---

## 4. Tool-Contracts

Namen sind über beide Surfaces identisch. MCP: `hive_create_presentation`.
OpenAPI: `POST /tools/create_presentation`.

### Generierung

| Tool | Eingabe | Ausgabe |
|---|---|---|
| `hive_create_presentation` | `spec: DeckSpec \| null`, `brief: str \| null`, `options: RenderOptions` | `{file_id, filename, slide_count, warnings[]}` |
| `hive_create_document` | `spec: DocSpec \| null`, `brief: str \| null`, `options: RenderOptions` | `{file_id, filename, page_estimate, warnings[]}` |
| `hive_create_spreadsheet` | `spec: SheetSpec \| null`, `brief: str \| null`, `options: RenderOptions` | `{file_id, filename, sheets[], warnings[]}` |

Genau eines von `spec` / `brief` ist gesetzt. `brief` erfordert `HIVE_LLM_ENABLED=true`,
sonst kommt ein Fehler zurück, der dem Modell sagt: „liefere eine vollständige Spec".

**Upload-Detail:** Der Upload nach OpenWebUI läuft mit `POST /api/v1/files/?process=false`.
Default ist `process=true` — dann würde OWUI die frisch erzeugte Datei extrahieren und in die
Vektordatenbank einbetten. Für ein Generierungs-Artefakt ist das sinnlos, kostet Zeit und
erzeugt die dokumentierte Race-Condition beim asynchronen Processing. Für den *Lese*-Pfad
(`hive_read_document`) gilt das nicht — dort holen wir den Inhalt selbst über
`GET /api/v1/files/{id}/content` und parsen ihn mit der passenden OOXML-Bibliothek, statt uns
auf OWUIs Textextraktion zu verlassen (die verliert Struktur, die wir zum Patchen brauchen).

### Templates

| Tool | Zweck |
|---|---|
| `hive_list_templates(kind?)` | Für den Nutzer sichtbare Templates (global + Gruppe + privat) |
| `hive_inspect_template(template_id)` | **Wichtigstes Tool für die Modellqualität.** Gibt Layouts, Placeholder-Namen/-Indizes, Style-Namen, Named Ranges, gefundene `{{vars}}` und Theme-Fonts zurück — damit das Modell eine Spec bauen kann, die zum Template *passt*. |
| `hive_upload_template(file_id, name, kind, visibility)` | Holt die Datei per OWUI-Files-API, validiert (öffnet sie), speichert auf dem PVC |
| `hive_delete_template(template_id)` | Nur eigene bzw. als Admin |

### Bearbeitung hochgeladener Dateien

| Tool | Zweck |
|---|---|
| `hive_read_document(file_id, mode)` | Strukturierte Extraktion. `mode`: `outline` (billig, für Orientierung) oder `full` |
| `hive_edit_document(file_id, operations[])` | Wendet Patch-Operationen an, lädt Ergebnis als neue Datei hoch |

`EditOp` (diskriminierte Union über `op`):
`replace_text` · `set_cell` · `set_range` · `add_slide` · `delete_slide` · `reorder_slides`
· `replace_image` · `set_paragraph` · `insert_block` · `add_sheet` · `apply_style`
· `fill_placeholders`

### GUI & Skill

| Tool | Zweck |
|---|---|
| `hive_open_config(kind, prefill?)` | OpenAPI-Pfad: `HTMLResponse` mit `Content-Disposition: inline` → Rich-UI-Card. MCP-Pfad: signierte URL + Hinweistext (kein Inline-Rendering möglich). |
| `hive_usage_guide(topic?)` | Gibt den Inhalt der Skill zurück |

### Datenmodelle (Kern)

```python
class RenderOptions(BaseModel):
    template_id: str | None = None
    font_family: str | None = None          # siehe Font-Caveat, §7
    font_size_base: int | None = None
    theme_colors: dict[str, str] | None = None   # accent1..6, bg, text
    language: str = "de"
    audience: str | None = None             # wirkt nur im brief-Modus
    target_length: int | None = None        # Slides bzw. Seiten
    density: Literal["sparse","normal","dense"] = "normal"
    include_notes: bool = False
    include_toc: bool = False
    page_size: Literal["A4","Letter"] = "A4"
    filename: str | None = None

class Slide(BaseModel):
    layout: Literal["title","title_content","two_content","section",
                    "image","table","chart","blank"] = "title_content"
    title: str | None = None
    bullets: list[Bullet] = []              # rekursiv, max. 3 Ebenen
    body: str | None = None
    table: TableData | None = None
    chart: ChartData | None = None
    image: ImageRef | None = None
    notes: str | None = None
    placeholders: dict[str, str] = {}       # füllt {{vars}} des Templates

class DeckSpec(BaseModel):
    title: str
    subtitle: str | None = None
    slides: list[Slide]
```

`DocSpec` ist eine Block-Liste (`heading`/`paragraph`/`bullet_list`/`numbered_list`/
`table`/`image`/`page_break`/`toc`/`quote`/`code`).
`SheetSpec` ist eine Sheet-Liste mit `columns[{header,key,type,format,width}]`, `rows`,
`formulas`, `freeze_panes`, `autofilter`, `conditional_formats`, `charts`.

**Warum so explizit:** Diese Schemata gehen 1:1 als JSON-Schema in `tools/list`. Je präziser
sie sind, desto weniger muss die Skill erklären — und desto seltener halluziniert das Modell
Felder. Enums statt Freitext, wo immer möglich.

---

## 5. Konfigurations-GUI

Server-gerendertes HTML aus Jinja2, kein Frontend-Build. Route: `GET /ui/config?kind=pptx&t=<token>`.

**Warum signierte URL:** Das iframe läuft sandboxed und cross-origin — es kann den
`Authorization`-Header nicht mitschicken. Deshalb trägt der Link ein HMAC-signiertes,
kurzlebiges Token (TTL 15 min) mit `user_id`, `kind` und `prefill`.

**Pflicht-Snippets im ausgelieferten HTML:**

```html
<script>
  function reportHeight() {
    parent.postMessage({type:'iframe:height',
                        height: document.documentElement.scrollHeight}, '*');
  }
  window.addEventListener('load', reportHeight);
  new ResizeObserver(reportHeight).observe(document.body);
</script>
```

Ohne das bleibt das iframe auf Default-Höhe und der Inhalt wird abgeschnitten —
`allowSameOrigin` ist standardmäßig aus, die Parent-Seite kann die Höhe nicht messen.

**Absenden** (D7):

```js
parent.postMessage({type:'input:prompt:submit',
                    text:'Erzeuge die Präsentation mit diesen Parametern: ' + json}, '*');
```

Cross-Origin zeigt OpenWebUI dazu einen Bestätigungsdialog — akzeptabel, und der Preis dafür,
dass niemand `allowSameOrigin` einschalten muss.

**Response-Header** auf dem OpenAPI-Endpunkt:
`Content-Disposition: inline` **und** `Access-Control-Expose-Headers: Content-Disposition`.
Letzteres ist zwingend, sobald HiveMCP als *Direct Tool Server* aus dem Browser
aufgerufen wird — sonst kann OpenWebUI den Disposition-Header nicht lesen und rendert Rohtext.

---

## 6. Skill

`skills/hivemcp-usage/SKILL.md` beschreibt dem Modell die Bedienung: Tool-Auswahl-Entscheidungsbaum,
wie eine gute `DeckSpec`/`DocSpec`/`SheetSpec` aussieht (mit Positiv-/Negativbeispielen),
der Pflichtablauf `hive_inspect_template` → Spec bauen → `hive_create_*`, sowie Grenzen
(Font-Verfügbarkeit, max. Bulletebenen, Bildformate).

Drei Auslieferungskanäle, weil OpenWebUI MCP-Prompts nicht zuverlässig anzeigt:

1. **MCP `prompts/list` / `prompts/get`** — der protokollkonforme Weg, funktioniert für andere MCP-Clients.
2. **Tool `hive_usage_guide()`** — funktioniert *immer*, das Modell kann sich die Anleitung selbst holen.
3. **`GET /skills/hivemcp-usage`** (Markdown) — zum Einfügen in den System-Prompt eines OWUI-Modells oder als Knowledge-Dokument.

Die Datei ist die einzige Quelle der Wahrheit; alle drei Kanäle lesen sie beim Start ein.

---

## 7. Bekannte Grenzen, die eingeplant werden müssen

**Schriftarten.** `python-pptx`/`python-docx` schreiben nur den *Font-Namen* ins OOXML. Ist die
Schrift auf dem öffnenden Rechner nicht installiert, substituiert Office. v1 löst das mit einer
kuratierten Auswahl sicherer Fonts in der GUI plus einer Warnung im Tool-Ergebnis. Echtes
Font-Embedding ist möglich, aber fummelig — Backlog.

**Seitenzahl bei Word.** Die exakte Seitenzahl steht erst nach dem Paginieren durch Word fest,
das kann eine Bibliothek nicht wissen. `target_length` steuert deshalb die *Textmenge* über eine
kalibrierte Zeichen-pro-Seite-Heuristik; das Ergebnis wird als `page_estimate` (nicht `page_count`)
zurückgegeben.

**Bearbeitung ist Patch, kein Round-Trip.** `hive_edit_document` öffnet das Original und wendet
gezielte Operationen an. Es wird *nicht* nach Spec geparst und neu gerendert — das würde
Formatierung zerstören. Entsprechend ist die Operationsliste bewusst eng.

---

## 8. Deployment

**Image.** Multi-Stage auf `python:3.12-slim`, Deps mit `uv` im Builder, Runtime ohne Build-Tools.
Non-Root (UID 10001), `readOnlyRootFilesystem: true` mit `emptyDir` auf `/tmp`,
alle Capabilities gedroppt. Zielgröße < 300 MB.

**Kubernetes** (Kustomize, `base` + Overlays `dev`/`prod`):
Deployment (2 Replicas) · Service · Ingress · PVC (RWX) · ConfigMap · Secret · HPA (CPU 70 %) ·
PodDisruptionBudget · NetworkPolicy (Egress nur zu OWUI + optional LLM-Endpoint).

Ressourcen: Requests `250m` / `512Mi`, Limits `1` / `2Gi` — Rendering ist speicher-spitzig,
besonders bei großen Excel-Dateien.

Probes: `/healthz` (liveness, trivial) und `/readyz` (readiness, prüft PVC beschreibbar +
OWUI erreichbar). `HIVE_MAX_UPLOAD_MB` und ein Semaphore auf gleichzeitige Render-Jobs
verhindern OOM-Kills.

**Konfiguration** (Auszug): `HIVE_AUTH_TOKEN`, `HIVE_SIGNING_KEY`, `HIVE_PUBLIC_URL`,
`HIVE_DATA_DIR=/data`, `HIVE_OWUI_URL`, `HIVE_OWUI_API_KEY`, `HIVE_LLM_ENABLED`,
`HIVE_LLM_FALLBACK_MODEL`, `HIVE_MCP_HOST`, `HIVE_MAX_UPLOAD_MB`, `HIVE_TMP_TTL_MINUTES`.
Es gibt bewusst **kein** `HIVE_LLM_BASE_URL`/`HIVE_LLM_API_KEY`: der Brief-Modus läuft
über OpenWebUI (D2/D9), damit immer das im Chat gewählte Modell antwortet.

**PVC-Layout:**

```
/data/templates/global/{template_id}/{file, meta.json, preview.png}
/data/templates/groups/{group_id}/{template_id}/…
/data/templates/users/{user_id}/{template_id}/…
/data/tmp/{request_id}/          # TTL-bereinigt durch Hintergrund-Task
/data/audit/{YYYY-MM-DD}.jsonl
```

Kein Index-DB. Sichtbarkeit ergibt sich aus dem Pfad, Metadaten aus `meta.json`,
In-Memory-Cache invalidiert über mtime.

---

## 9. Teststrategie

- **Unit** — Spec → Datei, Assertion durch *Wiederöffnen* mit derselben Bibliothek. Kein Byte-Vergleich (OOXML ist nicht deterministisch: Timestamps, ZIP-Reihenfolge, Revision-IDs).
- **Contract** — Snapshot-Test auf `tools/list` und `openapi.json`. Bricht das Schema, bricht die OWUI-Integration; das soll CI merken, nicht der Nutzer.
- **Template** — Fixture-Templates (`.potx/.dotx/.xltx`) im Repo, Test auf Inspect-Ergebnis und korrekte Platzhalter-Befüllung.
- **Integration** — `docker-compose` mit echtem OpenWebUI: Server registrieren, `tools/list` abrufen, Tool aufrufen, prüfen dass die Datei in OWUI landet.
- **Smoke im Cluster** — Post-Deploy-Job, der eine Mini-Präsentation erzeugt.

---

## 10. Meilensteine

| M | Inhalt | Ergebnis |
|---|---|---|
| **M0** | **Spikes** (siehe R1–R3) — vor allem Files-API-Ownership und Rich-UI-Rendering | Drei beantwortete Fragen, ggf. korrigierter Plan |
| **M1** | Skeleton: FastAPI, Config, Auth, Health, Dockerfile, CI | Container läuft, `/healthz` grün |
| **M2** | Render-Core: pptx/docx/xlsx aus Spec, ohne Templates, mit Tests | `python -m hivemcp.render` erzeugt lokal alle drei Formate |
| **M3** | Beide Surfaces + Files-API-Anbindung | **Erste Datei erscheint im OpenWebUI-Chat** (wichtigster Meilenstein) |
| **M4** | Templates: Upload, Inspect, Fill | Eigenes Corporate-Template funktioniert |
| **M5** | Konfigurations-GUI als iframe + Prompt-Submit-Flow | Anforderung 5 erfüllt |
| **M6** | Bearbeitung hochgeladener Dateien | Anforderung 6 erfüllt |
| **M7** | Skill, Doku, K8s-Manifeste, Härtung, Lasttest | Produktionsreif |

M3 ist bewusst früh: erst wenn eine Datei nachweislich im Chat ankommt, sind alle
Integrationsannahmen bestätigt. Alles danach ist Ausbau auf bewiesenem Fundament.

---

## 11. Risiken

| # | Risiko | Bewertung | Mitigation |
|---|---|---|---|
| ~~R1~~ | **ERLEDIGT nach M0.** Die Session-Token-Auth (D6) löst das Problem an der Wurzel: der Upload läuft mit dem Token des Nutzers, die Datei gehört ihm. Nachgewiesen über das `user_id`-Feld der hochgeladenen Datei, nicht über Sichtbarkeit. Ursprüngliche Formulierung zur Nachvollziehbarkeit: *Datei-Ownership bei der OWUI-Files-API.* Der Upload erfolgt mit einem Service-API-Key; OWUI ordnet Dateien dem Besitzer des Keys zu. Die Datei gehört dann dem Service-Account, nicht dem chattenden Nutzer — der sieht sie in seiner Dateiverwaltung womöglich nicht. | **Hoch**, blockierend für D3 | **M0-Spike**: Datei mit Service-Key hochladen und prüfen, wem sie zugeordnet wird und ob der Nutzer sie im Chat öffnen kann. Fallback: zusätzlich signierte Download-URL von HiveMCP ausliefern. Der Code sieht dafür von Anfang an eine `Delivery`-Abstraktion mit zwei Implementierungen vor, damit der Wechsel eine Config-Änderung bleibt und kein Refactoring. |
| **R2** | OpenWebUI rendert unsere `HTMLResponse` über den OpenAPI-Pfad nicht wie dokumentiert | Mittel | **M0-Spike** mit einem 20-Zeilen-Dummy-Tool, bevor die GUI gebaut wird |
| **R3** | Installierte OWUI-Version < 0.6.31 (kein natives MCP) | Mittel, leicht prüfbar | **M0**: Version feststellen. Falls älter: OpenAPI-Surface allein trägt alle Anforderungen, MCP kommt via `mcpo` oder nach dem Upgrade |
| **R4** | RWX-StorageClass im Cluster nicht verfügbar | Mittel | Früh mit der Plattform klären. Fallback: 1 Replica mit RWO, oder D4 auf S3/MinIO drehen — `templates/store.py` ist dafür hinter einem Interface |
| **R5** | OOM bei großen Dateien | Mittel | Upload-Limit, Semaphore auf parallele Render-Jobs, Streaming statt Vollpuffer, großzügiges Memory-Limit |
| **R6** | Kaputte oder exotische Templates crashen den Inspector | Mittel | Validierung beim Upload (Datei muss sich öffnen lassen), defensives Parsing, aussagekräftige Fehler statt Stacktrace |
| **R7** | OOXML-Dateien aus unbekannter Quelle (Anforderung 6) als Angriffsfläche — Zip-Bomben, XXE, externe Referenzen | Mittel | `defusedxml`, entpackte Größe begrenzen, Rendering ohne Netzwerkzugriff, NetworkPolicy |
| **R8** | Prompt-Injection aus hochgeladenen Dokumenten in `hive_read_document` | Niedrig–mittel | Extrahierten Text als Daten kennzeichnen, nicht als Instruktion; in der Skill explizit adressieren |

---

## 12. Offene Punkte

1. Welche OpenWebUI-Version läuft (R3)?
2. Welche RWX-StorageClass steht im Cluster bereit (R4)?
3. Soll der Hybrid-LLM-Modus in v1 aktiv sein — und gegen welchen Endpoint? (Kann bis M4 offen bleiben.)
4. Wird PDF-Export gebraucht? Falls ja, LibreOffice-Sidecar ab M7 einplanen.
5. Gibt es bereits Corporate-Templates, gegen die entwickelt werden kann? Ein echtes Template ab M2 verbessert die Qualität von `inspect.py` erheblich.
