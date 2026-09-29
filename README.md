# looped-in

An email digest for Gmail + Outlook, delivered to Telegram, with replies you
can send from your phone. Runs on this laptop; sorting and drafting go through
your own Claude Code install.

**Status:** step 2 of 6 — Gmail fetched and sorted by Claude into Needs you / Read / FYI / Noise, with
digest blurbs. Sorting runs through your local Claude Code (`claude -p`, Haiku), so it uses your Claude
plan, not an API key.

## Setup

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m loopedin ui
```

`ui` opens a control panel at <http://localhost:8765> where you connect
accounts and preview the combined inbox. Everything below can also be done
from the command line.

Nothing personal is stored in this folder. Config, the Outlook token cache and
(later) the database live in `%LOCALAPPDATA%\looped-in`; the Gmail app password
lives in Windows Credential Manager.

### Gmail

1. Turn on 2-Step Verification for the Google account.
2. Create an app password at <https://myaccount.google.com/apppasswords>.
3. Run, and paste the password when asked:

```powershell
.venv\Scripts\python.exe -m loopedin add-gmail you@gmail.com
```

### Outlook / Hotmail

Microsoft no longer allows app passwords for personal accounts, so this needs a
free app registration once:

1. Go to <https://portal.azure.com> → **App registrations** → **New registration**.
2. Name it `looped-in`. Supported account types: **Accounts in any
   organizational directory and personal Microsoft accounts**. No redirect URI.
3. Copy the **Application (client) ID** from the overview page.
4. **Authentication** → **Allow public client flows** → **Yes** → Save.
5. Run, then open the link it prints and enter the code:

```powershell
.venv\Scripts\python.exe -m loopedin add-outlook --client-id <the id>
```

## Check it works

```powershell
.venv\Scripts\python.exe -m loopedin accounts
.venv\Scripts\python.exe -m loopedin fetch --since 24h -v
```

`*` marks unread. Fetching never marks anything as read.

## Sorting

```powershell
.venv\Scripts\python.exe -m loopedin sort --since 24h -v
```

Or press **Sort with Claude** in the control panel. Each email is sorted once and cached in
`%LOCALAPPDATA%\looped-in\loopedin.db`. Open an email in the panel to correct its category or urgency;
corrections are never overwritten and are shown to Claude as examples on later runs.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest -q
```
