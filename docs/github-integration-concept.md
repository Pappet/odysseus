# Konzept: GitHub / Git-Anbindung für Odysseus

> Status: Entwurf / Diskussionsgrundlage
> Ziel: Eine erste Klasse-Integration für GitHub, die (a) eine Übersicht über
> die eigenen Repos liefert und (b) GitHub als Datenquelle/Aktionsfläche für die
> übrigen Odysseus-Funktionen (Agent, Chat, Notes, Tasks, Research, Editor)
> verfügbar macht.

## 1. Leitidee

Odysseus hat bereits ein etabliertes Muster für externe Dienste: eine Route-
Schicht (FastAPI `APIRouter`), eine optionale Service-Schicht, verschlüsselte
Credential-Speicherung in der DB und – entscheidend – die Möglichkeit, jede
Fähigkeit als **Agent-Tool** auszustellen. Die GitHub-Anbindung soll sich exakt
in dieses Muster einfügen, statt einen Sonderweg zu bauen.

Drei Ebenen, von "nur ansehen" bis "Agent handelt selbst":

1. **Connect** – GitHub-Account per OAuth Device-Flow verbinden (wie Copilot).
2. **Browse** – Repo-/Issue-/PR-Übersicht als eigenes Tool-Panel im UI.
3. **Act** – GitHub als Agent-Tools, damit Chat/Agent/Tasks darauf zugreifen.

## 2. Wie es sich in die bestehende Architektur einfügt

| Bereich | Bestehendes Muster | GitHub-Umsetzung |
|---|---|---|
| Auth/OAuth | `routes/device_flow.py` + `routes/copilot_routes.py` (GitHub Device-Flow gibt es bereits für Copilot!) | `routes/github_routes.py` mit `create_device_flow_router(...)` |
| Credential-Storage | `ProviderAuthSession` (Fernet-verschlüsselt via `EncryptedText`) | Token in `ProviderAuthSession(provider="github")` |
| Verbindungs-Metadaten | `Integration`-Tabelle (`type`, `config` JSON, `owner`) | `Integration(type="github")` für Account-Metadaten/Settings |
| Service-Logik | `services/research`, `services/memory` (Klasse + `__init__` Export) | `services/github/service.py` → `GitHubService` |
| UI-Panel | `static/index.html` `tools-section` + `static/js/<feature>.js` | `tool-github-btn` + `static/js/github.js` |
| Agent-Tools | `src/agent_tools/` + `TOOL_TAGS` + `tool_schemas.py` | `github_tools.py` + Schemas + Tags |
| Deep-Link | `@app.get("/calendar")` → SPA | `@app.get("/github")` → SPA |

Wichtig: Der **GitHub Device-Flow ist im Repo schon vorhanden** (Copilot nutzt
ihn für `github.com`). Wir können denselben Mechanismus mit anderen Scopes
wiederverwenden – das senkt den Aufwand erheblich.

## 3. Authentifizierung (Connect)

**Variante A – OAuth Device-Flow (empfohlen, self-host-freundlich):**
Kein Redirect-URI / kein öffentlicher Callback nötig – passt zum lokal-zuerst
Charakter von Odysseus und spiegelt exakt den Copilot-Flow.

```
POST /api/github/auth/start   → { user_code, verification_uri, poll_id }
POST /api/github/auth/poll    → { status: pending|authorized }, bei Erfolg Token speichern
GET  /api/github/status       → { connected, login, scopes, rate_limit }
DELETE /api/github/auth       → Token widerrufen + Zeile löschen
```

Scopes (minimal, erweiterbar): `repo` (private Repos lesen/schreiben),
`read:org`, `read:user`. Optional zwei Stufen anbieten: "read-only"
(`public_repo`/`read:*`) vs. "full" (`repo`).

**Variante B – Personal Access Token (PAT):**
Simpler Fallback für Nutzer, die kein OAuth-App-Setup wollen: Feld im UI,
Token landet ebenfalls in `ProviderAuthSession`. Sinnvoll als zweiter,
manueller Pfad.

Empfehlung: **Beide** anbieten (Device-Flow als Default-Button, PAT als
"Advanced"). Tokens immer über `EncryptedText` / `secret_storage` verschlüsselt,
nie im Klartext loggen.

## 4. Datenmodell

Bestehende Tabellen reichen für den Start – keine neue Migration zwingend nötig:

- `ProviderAuthSession(provider="github", owner=<user>, access_token=…, refresh_token=…, auth_mode="device|pat", scopes=…)`
- `Integration(type="github", owner=<user>, config={"login":…, "default_repo":…, "watched_repos":[…]})`

Optional später eine dedizierte `GitHubRepoCache`-Tabelle, falls Offline-/
Schnellansicht der Repo-Liste gewünscht ist (ETag-basiertes Caching). Für v1
genügt Live-Abfrage der GitHub-API + kurzlebiger In-Memory-Cache.

## 5. Service-Schicht

`services/github/service.py`:

```python
class GitHubService:
    def __init__(self, token_provider): ...        # holt Token aus ProviderAuthSession
    async def list_repos(self, owner=None, sort="updated") -> list[Repo]
    async def get_repo(self, full_name) -> Repo
    async def list_issues(self, full_name, state="open") -> list[Issue]
    async def list_prs(self, full_name, state="open") -> list[PullRequest]
    async def get_file(self, full_name, path, ref=None) -> FileContent
    async def search_code(self, query) -> list[CodeHit]
    async def create_issue(self, full_name, title, body) -> Issue
    async def comment(self, full_name, number, body) -> Comment
    async def rate_limit(self) -> RateLimit
```

Implementierung über `httpx` gegen die GitHub REST-API (`api.github.com`),
mit Token aus `ProviderAuthSession`, ETag-Caching und Rate-Limit-Handling.
Dataclasses (`Repo`, `Issue`, `PullRequest`) analog zu `ResearchSource`/
`ResearchResult`. Export über `services/github/__init__.py`.

## 6. Route-Schicht

`routes/github_routes.py` → `setup_github_routes()` gibt einen `APIRouter`
(prefix `/api/github`) zurück, registriert in `app.py` wie die übrigen Router.
Alle Endpunkte hinter dem bestehenden Auth-Gate (`require_*`).

```
GET  /api/github/repos                 # Repo-Übersicht (Hauptfunktion)
GET  /api/github/repos/{owner}/{repo}
GET  /api/github/repos/{owner}/{repo}/issues
GET  /api/github/repos/{owner}/{repo}/pulls
GET  /api/github/repos/{owner}/{repo}/contents/{path}
POST /api/github/repos/{owner}/{repo}/issues
GET  /api/github/search/code?q=...
GET  /api/github/rate_limit
```

## 7. Frontend (Browse)

Neues Tool-Panel analog zu Calendar/Notes:

1. Button in `static/index.html` `tools-section`: `<div id="tool-github-btn">GitHub</div>`
2. `static/js/github.js` registriert ein Modal/Panel via `Modals.register('github-panel', …)`
3. Sichtbarkeits-Gate in `static/js/init.js` (analog `hideOn('#tool-github-btn', privs.can_use_github)`)
4. Deep-Link `@app.get("/github")` → `serve_index`

UI-Inhalt v1:
- Repo-Liste (Name, Beschreibung, Sprache, Stars, letztes Update, privat/public)
- Klick auf Repo → Tabs: README/Files · Issues · Pull Requests
- Globale Suche (Repos + Code)
- Statuszeile mit verbundenem Account + Rate-Limit
- "Connect GitHub"-Zustand, wenn nicht verbunden

## 8. Agent-Integration (Act) – das eigentliche "Verbinden mit anderen Funktionen"

Hier entsteht der Mehrwert. GitHub wird als **Agent-Tools** ausgestellt, sodass
Chat, Agent-Modus und geplante Tasks darauf zugreifen können. Zwei Wege, die
sich nicht ausschließen:

**Weg 1 – Native Tools (empfohlen, tiefste Integration):**
- Handler in `src/agent_tools/github_tools.py`, registriert in `TOOL_HANDLERS`
- Schemas in `src/tool_schemas.py` (`FUNCTION_TOOL_SCHEMAS`)
- Tags in `TOOL_TAGS`: `github_list_repos`, `github_read_file`, `github_list_issues`,
  `github_create_issue`, `github_search_code`, `github_comment`
- Tools rufen intern `GitHubService` auf → eine Quelle der Wahrheit

**Weg 2 – Offizieller GitHub MCP-Server (schnellster Start, am breitesten):**
- Über die bestehende `McpServer`-Verwaltung (`routes/mcp_routes.py`) registrieren
- Token via `oauth_config`/`env` an den MCP-Server reichen (Muster wie Gmail-OAuth-Pfade)
- Liefert sofort eine große Tool-Menge, ohne eigene Schemas zu pflegen

Empfehlung: **MCP-Server für Reichweite + ein paar native Tools** für die
Kern-Reads (Repo-Liste, Datei lesen), weil die nativen Tools schlanker im
Kontext sind – relevant wegen der im ROADMAP genannten "Agent prompt/context
bloat".

### Verknüpfung mit konkreten Odysseus-Funktionen

| Funktion | GitHub-Verbindung |
|---|---|
| **Chat/Agent** | "Fass die offenen Issues in `repo X` zusammen", "Lies `app.py` und erkläre …", "Öffne ein Issue mit diesem Bug" |
| **Tasks** (cron) | Geplanter Task: täglich neue Issues/PRs prüfen → Notiz/Email/ntfy senden |
| **Notes** | Issue/PR als Notiz speichern; Notiz → Issue eskalieren (deckt einen Roadmap-Wunsch ab) |
| **Deep Research** | Repo-/Code-Kontext als zusätzliche Quelle in Research-Läufe einspeisen |
| **Editor/Documents** | Datei aus Repo in den Editor laden, mit AI bearbeiten, als Issue/Gist/PR-Vorschlag zurückgeben |
| **Memory** | Bevorzugte Repos / Projektkontext dauerhaft merken |
| **Webhooks** | `routes/webhook_routes.py` als GitHub-Webhook-Empfänger → Push/PR-Events lösen Tasks/Benachrichtigungen aus |

## 9. Sicherheit

- Tokens nur verschlüsselt (`EncryptedText`/`secret_storage`), nie im Klartext-Log.
- Schreibende Tools (Issue erstellen, kommentieren, Datei schreiben) als
  **admin-/permission-gated** und idealerweise mit Bestätigungsschritt – passt
  zur Roadmap-Notiz "Security hardening around admin-only tools".
- GitHub-Inhalte (Issue-Bodies, READMEs, Code) sind **untrusted input** →
  Prompt-Injection-Risiko. Im Agent-Kontext als Daten kennzeichnen, nicht als
  Instruktionen behandeln (deckt sich mit dem Roadmap-Punkt "Skill/tool
  prompt-injection audit").
- Default read-only; Schreibrechte explizit opt-in.
- Rate-Limit respektieren; ETag-Caching, um Limits zu schonen.

## 10. Umsetzung in Schritten

- **MVP (Connect + Browse):** Device-Flow-Route wiederverwenden, `GitHubService`
  mit `list_repos`/`get_repo`/Issues/PRs, einfaches `github.js`-Panel, Status-
  Endpoint. → Liefert sofort die gewünschte Repo-Übersicht.
- **Stufe 2 (Act):** GitHub-MCP-Server registrieren + 3–4 native Read-Tools;
  im Chat/Agent nutzbar.
- **Stufe 3 (Verknüpfung):** Tasks-Trigger (Issue-Watcher), Notes↔Issues,
  Editor↔Repo-Datei, optional Webhook-Empfang.
- **Stufe 4 (Politur):** Repo-Cache/Offline, PR-Reviews, Code-Suche im UI,
  Multi-Account.

## 11. Offene Entscheidungen

1. **Auth-Methode:** Device-Flow (empfohlen) vs. PAT vs. beide?
2. **Schreibrechte:** read-only v1, oder von Anfang an Issues/Comments erlauben?
3. **Agent-Anbindung:** offizieller GitHub-MCP-Server vs. native Tools vs. Hybrid (empfohlen)?
4. **Scope der Übersicht:** nur eigene Repos, oder auch Orgs/Stars/watched?
