# Auto Approve Join Request Bot

## New features
- 🖼 **Per-channel welcome media** (photo / video / GIF / document) — Channel → Welcome → Add Photo/Video/GIF
- 📊 **Stats dashboard** — Owner Panel → Stats Dashboard (approvals, DMs, follow-ups, 7-day trend, server health)
- 📣 **Safe broadcast** — preview + confirm, live progress, Stop button, FloodWait handling, dead-user cleanup (`/broadcast` or Owner Panel)
- 🌐 **Web server / webhook mode** — `/health` endpoint for Railway, optional `/webhook/<secret>` for join requests
- 📨 **Multi-channel posting** — `/send -100111 -100222 -100333` or `/send all`
- ⏰ **Follow-up message after 3 days** — text & buttons set by owner (Owner Panel → Follow-up Msg)
- 👋 **Bye message** — set by owner (Owner Panel → Bye Message)
- 🧪 **Unit tests + logging** — `python -m unittest discover -s tests -v`, `LOG_LEVEL` / `LOG_FILE`

## Deploy on Railway
1. Push this repo to GitHub, create a Railway project from it.
2. Add the variables from `.env.example` (at least `API_ID`, `API_HASH`, `BOT_TOKEN`, `DATABASE_URL`, `OWNER_ID`).
3. Start command is `python3 -m bot` (see `railway.json`). GitHub Actions runs the tests on every push.

> ⚠️ Never commit real secrets. Keep `API_HASH`, `BOT_TOKEN`, `DATABASE_URL` only in Railway variables.
