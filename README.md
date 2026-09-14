# ArchivePilot

> **Your data is scattered. Your archive should be one place.**

WhatsApp chats, Google Takeout, CSVs, notes — ArchivePilot puts them all in
one **local-first, zero-dependency** archive with full-text search in
**English and Arabic**, and **AI answers over your own data**.

**Nothing leaves your machine.** No cloud. No telemetry. No accounts.
Just SQLite + Python stdlib.

---

## ✨ Why ArchivePilot?

| Problem | ArchivePilot |
|---|---|
| Your data lives in 10 apps, 10 formats | One archive, one query |
| Search tools break on Arabic (diacritics, ال-, أ/إ/آ) | Arabic-aware normalization built in |
| "Ask AI" tools upload your data to strangers | AI reads *your* local context, cites sources |
| Heavy tools need Docker + 20 dependencies | **Zero dependencies. `python3 -m archivepilot`** |
| Questions like "meeting kab hai?" | bm25-ranked OR search → natural questions just work |

## 🚀 Quick start

```bash
git clone https://github.com/Amz34/archivepilot
cd archivepilot

# 1. Import your WhatsApp export
python3 -m archivepilot import --whatsapp ~/Downloads/chat.txt

# 2. Import that leads CSV
python3 -m archivepilot import --csv leads.csv

# 3. Search — English
python3 -m archivepilot search "meeting"

# 4. Search — Arabic (diacritics, ال-, variants — all handled)
python3 -m archivepilot search "موقع جديد"

# 5. Ask AI over YOUR data (DeepSeek by default, any OpenAI-compatible API)
export AP_API_KEY=sk-...          # or OPENAI_API_KEY / DEEPSEEK_API_KEY
python3 -m archivepilot ask "meeting kab hai?"
# → "Meeting 5 baje hai, site deploy ke liye. (Source: WhatsApp archive, 20/08/2026)"
```

No API key? `ask` falls back to local mode and shows the top matching
entries — the archive works fully offline.

## 📥 What you can import

| Source | Command | Notes |
|---|---|---|
| WhatsApp export | `--whatsapp chat.txt` | `.txt` format, media lines skipped |
| Google Takeout | `--takeout ~/Takeout/` | walks all JSON, any depth |
| CSV / TSV | `--csv file.csv` | one searchable record per row |
| Notes / Markdown | `--text notes.md` | one item per paragraph |
| Anything | `--stdin` | pipe it in |
| Microsoft 365 | `ingest --source sharepoint` | SharePoint / OneDrive drives + Teams messages via Graph |

## 🏢 Microsoft 365 ingestion (SharePoint · OneDrive · Teams)

Enterprise knowledge lives in M365. `ingest` pulls it into the same local
archive, so client document libraries and Teams conversations are searchable
in **English and Arabic** alongside everything else.

App-only (client credentials) auth — read-only, no user sign-in, no OAuth
browser dance:

```bash
export MSGRAPH_TENANT_ID=...      # directory (tenant) id
export MSGRAPH_CLIENT_ID=...      # app registration client id
export MSGRAPH_CLIENT_SECRET=...  # app registration secret (env only)

# 1. See what would be indexed — no writes, no database needed
python3 -m archivepilot ingest --source sharepoint --site contoso.sharepoint.com,<site-guid>,<web-guid> --dry-run

# 2. Index a whole site (every document library)
python3 -m archivepilot ingest --source sharepoint --site <site-id>

# 3. Index one drive (e.g. a user's OneDrive)
python3 -m archivepilot ingest --source onedrive --drive <drive-id>

# 4. Index Teams channel messages (replies included)
python3 -m archivepilot ingest --source teams --team <team-id> [--channel <channel-id>] [--no-replies]

# Then it's all just ArchivePilot
python3 -m archivepilot search "العقد الجديد"
python3 -m archivepilot ask "what did we agree on the Riyadh deployment?"
```

Setup (Microsoft Entra ID / Azure AD → App registrations → your app):

1. Add **application** permissions: `Files.Read.All`, `Sites.Read.All`,
   `ChannelMessage.Read.All` (add `Chat.Read.All` for chats).
2. **Grant admin consent** for the tenant.
3. Export the three environment variables above — that is the only place
   credentials ever exist.

What it does, and what it deliberately does not:

- Reads text-extractable documents (`.txt`, `.md`, `.csv`, `.json`, `.html`,
  `.xml`, `.log`, and `.docx` via stdlib `zipfile` + XML parsing) and Teams
  messages with replies; binary/media files are skipped.
- Stores only normalized archive records — **raw Graph JSON never enters the
  index**.
- Honors Graph throttling: `429` waits the `Retry-After` header, `5xx`
  retries with exponential backoff and a bounded attempt count.
- Caches the access token in memory and refreshes it before expiry.
- Missing credentials print one actionable line and exit `2` — non-zero, no
  traceback, no partial run. Secrets are never written to disk or logged.

CLI flags: `--db`, `--site`, `--site-name`, `--drive`, `--team`, `--channel`,
`--limit`, `--no-replies`, `--dry-run`.

## 🗣️ Arabic, done right

Raw Arabic search is broken in most tools because of script quirks.
ArchivePilot normalizes both the archive **and** your query:

- Diacritics removed: `مُحَمَّد` → `محمد`
- Alef variants unified: `أحمد إبراهيم آدم` → `احمد ابراهيم ادم`
- Definite article stripped: `الموقع` ≈ `موقع` (but `الله` stays intact)
- Hamza + teh marbuta handled: `مسؤولة` → `مسووله`, `شركة` → `شركه`

## 🔧 Commands

```
import    add data (whatsapp / takeout / csv / text / stdin)
search    full-text search, EN + AR
ask       AI answer over your archive (cites sources)
stats     archive statistics
ingest    pull SharePoint / OneDrive / Teams via Microsoft Graph
--db      choose archive location (default ~/.archivepilot/archive.db)
```

## 🛡️ Privacy by construction

- Single SQLite file — copy it, back it up, delete it. It's yours.
- AI calls send **only search results as context** — never your whole archive.
- No analytics, no tracking, no "phone home".
- Works 100% offline without an API key.

## 🧩 Architecture

```
archivepilot/
├── cli.py        command-line interface
├── db.py         SQLite + FTS5 storage layer
├── importers.py  WhatsApp / Takeout / CSV / text / stdin
├── graph.py      Microsoft Graph transport: token cache, paging, retries
├── ingest.py     SharePoint / OneDrive / Teams → archive records + CLI
├── arabic.py     Arabic normalization for search
├── ask.py        AI answers over local context (OpenAI-compatible)
└── tests/        52 unit tests, stdlib unittest (Graph tests fully mocked)
```

## ✅ Roadmap

- [ ] `export` command (archive → JSON/Markdown/HTML)
- [ ] Encrypted archives (SQLCipher)
- [ ] Telegram/WhatsApp bot front-end
- [ ] Docker image (optional — zero-deps means you won't need it)
- [ ] Outlook mail + calendar ingestion (Microsoft Graph)

## 🤝 Contributing

PRs welcome. Keep it zero-dependency, keep it local-first, keep it private.

## 📄 License

MIT © Amz34

---

Part of [my always-on agent stack](https://github.com/Amz34) · [Awesome Agent Infrastructure](https://github.com/Amz34/awesome-agent-infrastructure) (135 live-checked building blocks).
