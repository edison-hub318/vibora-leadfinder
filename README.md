# Vibora Leadfinder

A tool for Vibora that finds local UK businesses with no website or a weak one, ranks them as
leads, helps you call them, and drafts and sends cold emails within UK law (PECR).

It has two halves that share one database (`leads.db`):

- **`leadfinder` commands** you type in PowerShell
- **a dashboard in your browser** (`leadfinder ui`) for dialling, pipeline, outreach and team stats

Everything runs on your own PC. Nothing is hosted on the internet.

---

## 1. Setup on Windows 10/11 (step by step)

You only do this once. Open **PowerShell**: press the Windows key, type `PowerShell`, press Enter.

### 1.1 Install Python

1. Go to <https://www.python.org/downloads/> and download Python 3.12 (or any 3.11+).
2. Run the installer. **On the first screen, tick "Add python.exe to PATH"**, then click *Install Now*.
3. Close PowerShell and open a new one, then check it worked:

```powershell
py --version
```

You should see something like `Python 3.12.6`.

### 1.2 Get the code

Download the project (or `git clone` it) into a folder, for example `C:\Users\you\vibora-leadfinder`,
then move into that folder:

```powershell
cd C:\Users\you\vibora-leadfinder
```

### 1.3 Create a virtual environment and install

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
```

If `Activate.ps1` is blocked with *"running scripts is disabled on this system"*, run this once and
answer `Y`, then try the activate line again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

When the environment is active your prompt starts with `(.venv)`. You need to activate it each time
you open a new PowerShell window, or just use **`leadfinder.bat`** (see 1.6), which does it for you.

### 1.4 Create your `.env` file (settings and keys)

```powershell
copy .env.example .env
notepad .env
```

Fill in what you have. Only `GOOGLE_API_KEY` is needed to start searching:

| Setting | What it is |
|---|---|
| `GOOGLE_API_KEY` | Google Cloud key with **Places API (New)** and **PageSpeed Insights API** enabled |
| `COMPANIES_HOUSE_API_KEY` | Optional, free from <https://developer.company-information.service.gov.uk/>. Needed before cold emails can be sent (only limited companies can be emailed) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` | Your email provider's SMTP details (port 587 = STARTTLS, 465 = SSL) |
| `FROM_NAME`, `FROM_EMAIL` | Who the emails come from |
| `DAILY_SEND_LIMIT` | Max emails per day (default 30) |
| `MY_NAME`, `MY_PHONE`, `BUSINESS_ADDRESS` | Used in call openers and the email footer (the footer must show a real address) |
| `BACKUP_DIR` | A OneDrive or Google Drive folder, e.g. `C:\Users\you\OneDrive\LeadfinderBackups` |
| `NETLIFY_AUTH_TOKEN` | Optional: publish preview sites to a public link |
| `ANTHROPIC_API_KEY`, `AI_MODEL`, `AI_MAX_PER_SEARCH` | Optional AI judgement of websites |
| `FREE_TIER_MODE`, `FREE_TIER_SAFETY`, `MONTHLY_API_BUDGET_GBP` | Google cost protection (see section 4) |
| `BIND_HOST` | Leave as `127.0.0.1` (this PC only) unless you're sharing via Tailscale |
| `SECRET_KEY` | Leave empty: it's generated automatically on first run |

The tool never prints your keys or passwords, and `.env` is in `.gitignore` so it can't be committed.

### 1.5 Create the two accounts and log in

Each person gets their own account (you'll be asked to type the password twice; it isn't shown):

```powershell
leadfinder user add "Edison Young"
leadfinder user add "Jack Alcock"
```

Then log in on this PC so your command-line actions are recorded as yours:

```powershell
leadfinder login "Edison Young"
leadfinder whoami
```

Passwords need at least 8 characters. To reset one: `leadfinder user passwd "Jack Alcock"`.

### 1.6 Run it

```powershell
leadfinder ui
```

This opens the dashboard at <http://localhost:8765>. Log in with your full name and password.

**Shortcut:** double-click **`leadfinder.bat`** in the project folder to open the dashboard without
activating anything. You can also run any command through it, e.g. `.\leadfinder.bat list --top 10`.

### macOS / Linux (short version)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env    # then edit it
leadfinder user add "Edison Young"
leadfinder login "Edison Young"
leadfinder ui
```

---

## 2. The five commands you'll use most

```powershell
leadfinder search "plumbers" --town "Crawley" --max 30
leadfinder next
leadfinder log 12 --outcome callback --callback 2026-10-20 --note "Ring after 2pm"
leadfinder stats
leadfinder ui
```

---

## 3. Every command, with examples

Commands that change data need you to be logged in (`leadfinder login "Your Name"`).

### Accounts

| Command | Example |
|---|---|
| Create an account | `leadfinder user add "Jack Alcock"` |
| Reset a password | `leadfinder user passwd "Jack Alcock"` |
| List accounts | `leadfinder user list` |
| Log in / out on this PC | `leadfinder login "Edison Young"` / `leadfinder logout` |
| Who am I? | `leadfinder whoami` |

### Finding leads

```powershell
leadfinder search "plumbers" --town "Crawley"                 # up to 60 new callable leads
leadfinder search "barbers" --town "RH10" --max 20 --radius 5
leadfinder search "electricians" --town "Horley" --force      # repeat a search done in the last 14 days
leadfinder search "plumbers" --town "Crawley" --no-audit      # skip website checks (faster)
leadfinder search "saddlers" --town "Crawley" --save-trade    # a trade not in trades.yaml, and save it
leadfinder search "plumbers" --town "Crawley" --include-no-phone --budget 5
leadfinder trades                                              # list known trades
```

`--max N` means **N new, qualified, callable leads**, not N Google results. Each search:

1. Finds the town (once; it's cached forever) and searches within the radius, dropping anything
   further than 1.5 x the radius away.
2. Widens automatically with synonyms from `trades.yaml` (e.g. "emergency plumber", "boiler repair"),
   then nearby towns, until it has N leads or hits the per-search request cap.
3. Drops wrong categories (e.g. B&Q for "plumbers"), chains and franchises (`chains.txt`, a domain
   shared by 3+ listings, or a name in 3+ towns), duplicate listings (same phone, or the same name
   at the same postcode), anyone already handled (clients, Do Not Call, TPS-registered, not
   interested in the last 90 days, your partner's active leads, or already in the database), and
   leads with no phone number.
4. Flags ghost listings and low ratings, audits each website, looks for a business email on the
   lead's own site, checks Companies House (if the key is set), scores everything and saves it.

The summary shows each step's count, the website breakdown, the cost and the top 10 leads.

### Website audits and data refresh

```powershell
leadfinder audit --limit 50            # audit older leads that were never checked
leadfinder audit --limit 50 --recheck  # re-audit everything
leadfinder refresh --older-than 30     # re-fetch Google details for leads not refreshed in 30 days
```

**Google's terms limit how long Places content may be cached** (place IDs can be kept). Run
`leadfinder refresh` regularly, for example monthly. It only refreshes active leads (not won, lost or
Do Not Call), oldest first, and stops at your free Place Details allowance. Leads that Google now
marks as closed are moved to *lost*.

### Looking at leads

```powershell
leadfinder list --top 25
leadfinder list --town Crawley --trade plumbers --no-website
leadfinder list --status callback
leadfinder show 12
leadfinder export leads.csv --town Crawley
```

Statuses: `new`, `called`, `callback`, `interested`, `meeting_booked`, `proposal_sent`, `won`
(also `client`), `lost`, `not_interested`, `wrong_number`, `dnc`. The CSV opens directly in Excel
and never includes Do Not Call or TPS-registered numbers.

### Calling

```powershell
leadfinder next
leadfinder tps 12 --clear           # you checked the number against TPS/CTPS: not registered
leadfinder tps 12 --registered      # registered: it will never be served for calling
leadfinder log 12 --outcome no_answer --duration 0:25
leadfinder log 12 --outcome callback --callback 2026-10-20T14:00 --note "Ask for Dave"
leadfinder log 12 --outcome meeting_booked --meeting 2026-10-21T10:00
leadfinder log 12 --outcome not_interested --objection too_expensive --objection use_socials
leadfinder log 12 --outcome gatekeeper --objection-other "Wife does the admin"
leadfinder stage 12 proposal_sent
leadfinder stage 12 won --setup 800 --retainer 150
leadfinder stage 12 lost --reason "Went with a cheaper quote"
leadfinder assign 12 "Jack Alcock"
leadfinder meetings
leadfinder stats
```

Outcomes: `no_answer`, `gatekeeper`, `connected` (you spoke to the decision-maker), `callback`,
`meeting_booked`, `not_interested`, `wrong_number`. Objection tags: `too_expensive`, `use_socials`,
`word_of_mouth`, `already_have_someone`, `bad_timing`, `not_decision_maker`, `no_value`, `other`
(editable in Settings).

`next` serves your own callbacks due today first, then the best new leads, then retries. Three
no-answers in a row sends a lead to the bottom. It warns when a number hasn't been checked against
TPS. A lead you open is reserved for you for 10 minutes so your partner isn't served the same one.

### Company type (for email rules)

```powershell
leadfinder classify --limit 50                 # needs COMPANIES_HOUSE_API_KEY
leadfinder set-type 12 limited_company
leadfinder set-type 12 sole_trader
```

### Cold email (UK PECR rules are built in)

```powershell
leadfinder find-emails --limit 20       # look on each lead's own website (searches do this too)
leadfinder add-email 12 info@smithplumbing.co.uk
leadfinder draft --limit 20             # writes drafts from templates\cold_email.txt
leadfinder review                       # a = approve, e = edit (opens Notepad), s = skip, d = delete, then Enter
leadfinder send --dry-run               # shows exactly what would be sent
leadfinder send
leadfinder followup                     # one follow-up, 5+ days after the first email, if no reply
leadfinder replied 12                   # they replied: no follow-up
leadfinder suppress bob@example.co.uk   # never email again
leadfinder unsubscribe-check            # paste opt-out replies; their addresses are suppressed
```

The rules the tool enforces (you can't turn them off):

- Emails go **only to limited companies**. Sole traders and partnerships need prior consent under
  PECR, so they're refused (call them instead). Unknown company types are refused too: run
  `classify` or `set-type` first.
- Never more than `DAILY_SEND_LIMIT` a day (counted from the database), with a random 60-180 second
  wait between emails.
- Never to a suppressed address; never twice to the same lead (except one follow-up).
- Plain text, one recipient, a `List-Unsubscribe` header, and a footer with your name,
  `BUSINESS_ADDRESS` and "Reply STOP or 'unsubscribe' and I won't contact you again."
- Email addresses are only ever ones you add or ones published on the business's own website. They
  are never guessed.

### Preview sites

```powershell
leadfinder preview 12             # writes previews\<name>\index.html
leadfinder preview 12 --publish   # also deploys it to Netlify (needs NETLIFY_AUTH_TOKEN)
```

Previews use only the business name, trade, town and phone number with generic copy; never Google
reviews or photos. Each page says "Concept design by Vibora" and isn't indexed by search engines.
Add `{preview_url}` to your email template to mention it.

### Do Not Call

```powershell
leadfinder dnc add "01293 123456" --reason "Asked not to be called"
leadfinder dnc add "+44 1293 123456" --no-email   # also suppress their email
leadfinder dnc list
leadfinder dnc remove "01293 123456"              # asks you to confirm
```

Numbers are stored in international format, so `01293 123456`, `+44 (0)1293 123456` and
`0044 1293 123456` all match. This list is separate from TPS.

### Backups

```powershell
leadfinder backup                              # back up now
leadfinder backup list
leadfinder backup restore leads-20261016-091500.db   # asks you to type RESTORE
```

A backup is taken automatically once a day (the first time you use the tool that day). It keeps the
last 30 daily backups and one per month for 12 months. **Set `BACKUP_DIR` to a OneDrive or Google
Drive folder** so your leads survive a dead laptop. Restoring always takes a safety backup first.

### The dashboard

```powershell
leadfinder ui                  # opens http://localhost:8765
leadfinder ui --no-browser
```

Pages: **Dashboard** (KPIs, best time to call, funnel, leaderboard, free Google allowance),
**Dial** (power dialling: keys `C` to start the timer, `1`-`7` for outcomes, `N` for next; works on
a phone), **Leads** (filters, side panel, New search, coverage), **Pipeline** (drag cards between
stages), **Meetings** (`.ics` and Google Calendar), **Outreach**, **Preview Sites**, **Objections**,
**Do Not Call**, **Backups**, **Settings**, and **My grafting** (from the account menu).

---

## 4. Google costs: free-tier mode (on by default)

Google gives a free number of requests per SKU each month (the month runs on Pacific time and resets
on the 1st). The searches here bill as **Text Search Enterprise** and refresh bills as **Place
Details Enterprise**: about 1,000 free each per month at the time of writing. **Check the current
numbers on Google's pricing page and update `free_tier.yaml` if they've changed.**

- Every Google request is logged. Before each one, the tool checks this month's count and stops at
  90% of the allowance (`FREE_TIER_SAFETY=0.9`), keeping what it has found so far:
  *"Free monthly Google allowance reached for Text Search (900/1000). Resets on 01 November 2026."*
- Each search makes at most 15 requests (`per_search_request_cap` in `free_tier.yaml`); a normal
  30-lead search needs about 2-15.
- New towns cost one cheap lookup the first time, then they're cached forever.
- PageSpeed Insights and Companies House are free.
- `FREE_TIER_MODE=false` lifts the stop and uses a monthly £ cap instead (`MONTHLY_API_BUDGET_GBP`).

The dashboard shows a meter (amber at 70%, red at 90%), and Settings shows usage per person and per
search.

---

## 5. Sharing the dashboard with your partner (privately, with Tailscale)

Both of you can use the same dashboard from your laptops and phones without putting it on the
internet:

1. Install **Tailscale** (<https://tailscale.com/download>) on the PC that runs leadfinder, and on
   each partner's laptop and phone. Sign in to the same Tailscale account (or share the PC with your
   partner's account).
2. On the host PC, find its Tailscale IP: click the Tailscale icon, or run `tailscale ip -4`. It
   looks like `100.101.102.103`.
3. Put that IP in `.env`: `BIND_HOST=100.101.102.103`
4. Run `leadfinder ui` on the host PC.
5. On any of your devices with Tailscale on, open `http://100.101.102.103:8765`.

> **Never port-forward 8765 on your router**, and never set `BIND_HOST=0.0.0.0`. That would put your
> leads and the login page on the public internet. Tailscale keeps it private to your devices.

The PC must be on (and `leadfinder ui` running) for others to reach it.

---

## 6. Customising

| File | What to change |
|---|---|
| `trades.yaml` | Trades, synonyms, Google place types, name keywords, preview services |
| `chains.txt` | National chains and franchises to skip |
| `towns.yaml` | Starter towns for suggestions and widening |
| `website_rules.yaml` | Social/builder domains and the audit thresholds |
| `free_tier.yaml` | Google free allowances and per-search cap |
| `templates\cold_email.txt`, `templates\followup_email.txt` | Email templates (also editable in Settings) |
| `templates\call_script.md` | Call script (also editable in Settings) |
| `templates\previews\` | Preview site designs |
| `leadfinder\scoring.py` | The scoring formula (one commented function) |

---

## 7. Troubleshooting

- **`leadfinder` is not recognised**: activate the environment (`.venv\Scripts\Activate.ps1`) or use
  `leadfinder.bat`.
- **"Nobody is logged in"**: run `leadfinder login "Your Name"`.
- **"No accounts yet"** when starting the UI: run `leadfinder user add "Your Name"` for each person.
- **Locked out after 5 wrong passwords**: wait 15 minutes, or reset with `leadfinder user passwd`.
- **Google errors**: check the key has *Places API (New)* and *PageSpeed Insights API* enabled and
  billing is set up on the Google Cloud project (needed even for the free tier).

## 8. For developers

```powershell
pip install -e ".[dev]"
pytest
```

The tests use fake API clients and temporary databases; they make no network calls.
