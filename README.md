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
├── arabic.py     Arabic normalization for search
├── ask.py        AI answers over local context (OpenAI-compatible)
└── tests/        14 unit tests, stdlib unittest
```

## ✅ Roadmap

- [ ] `export` command (archive → JSON/Markdown/HTML)
- [ ] Encrypted archives (SQLCipher)
- [ ] Telegram/WhatsApp bot front-end
- [ ] Docker image (optional — zero-deps means you won't need it)

## 🤝 Contributing

PRs welcome. Keep it zero-dependency, keep it local-first, keep it private.

## 📄 License

MIT © Amz34

---

Part of [my always-on agent stack](https://github.com/Amz34) · [Awesome Agent Infrastructure](https://github.com/Amz34/awesome-agent-infrastructure) (135 live-checked building blocks).
