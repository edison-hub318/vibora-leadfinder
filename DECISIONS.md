# Decisions and assumptions

These are the choices I made where the brief left room, so you can check or change them.

## Platform and packaging

- **Python 3.11+**, packaged with `pyproject.toml`; `pip install -e .` provides `leadfinder`. The
  install must be *editable* (`-e`): the dashboard's `static/` files and the editable config files
  (`trades.yaml`, `templates/` etc.) live in the project folder, not inside the Python package.
- **Project folder = data folder.** `.env`, `leads.db`, `backups/` (default), `previews/` and the
  config files all live in the repo folder. `LEADFINDER_HOME` can point somewhere else.
- **`tzdata` is a dependency** because Windows has no time-zone database and `zoneinfo` needs one
  for Europe/London and Pacific time.
- `leadfinder.bat` opens the dashboard when double-clicked, or runs any command
  (`leadfinder.bat list`). `.gitattributes` keeps it with Windows (CRLF) line endings.
- CLI output uses rich with `safe_box=True`, no emoji, and "stars" instead of ★ (the old PowerShell
  console fonts don't have ★). The web UI uses ★, which every browser renders.
- SQLite runs in WAL mode so the dashboard and the CLI can use the database at the same time.
- All times are stored in UTC. "Today", callbacks, meetings and the heatmap use Europe/London. The
  Google billing month uses America/Los_Angeles.

## Search pipeline

- **Field mask:** I added `places.location` to the field mask you specified. The out-of-area filter
  needs each result's coordinates. `location` is a Pro-tier field, so it doesn't raise the price of a
  request that already bills as Enterprise.
- **Geocoding** uses a Text Search with a minimal field mask (id, name, address, location), which
  bills as **Text Search Pro**, a separate SKU (5,000 free/month, in `free_tier.yaml`). Towns in
  `towns.yaml` (a Crawley-area starter list with coordinates) need no request at all. Towns Google
  can't find are cached as "not found", so they never cost a second request.
- **`--max N` stop rule:** qualification is re-run after every page, and the search stops once N
  leads qualify. Any extra qualified leads on that last page are saved too: they've already been
  paid for. The summary says "found X of N".
- **Per-search budget:** 15 Google requests by default (`per_search_request_cap` in
  `free_tier.yaml`, `--budget` to override), on top of the monthly free-tier limit. Each query fetches
  at most 3 pages (Google's 60-result limit) and waits 2 seconds before each next page.
- **Widening order:** main query → each synonym in `trades.yaml` → the main query in each nearby
  town or postcode district within the radius (from `towns.yaml` plus towns already geocoded),
  nearest first. The out-of-area filter is always measured from the original town.
- **Category check:** a listing passes if its `primaryType` is one of the trade's `types`, *or* its
  name contains one of the trade's `keywords`, *or* it has no `primaryType` at all (many small
  businesses don't; dropping them would lose good leads). Trades not in `trades.yaml` skip the check.
- **Chains:** name contains a `chains.txt` entry (whole-word match, ignoring case and punctuation);
  or the website domain is shared by 3+ listings (database + this search; social and free-builder
  domains such as facebook.com don't count); or the same normalised name appears in 3+ different
  towns. Chains are not stored.
- **Duplicates:** same E.164 phone number, or a near-identical name (≥92% similar and the same
  numbers in the name) at the same postcode. Within one search the listing with the most reviews is
  kept. A new listing that duplicates an existing lead is linked to that lead (the existing lead
  stays primary, so IDs and history never move). Duplicates are stored with `duplicate_of` so their
  place IDs are never re-imported.
- **Already handled:** any listing whose place ID is already in the database is never "new". The
  report breaks these down: client/won, Do Not Call, TPS-registered, not interested in the last 90
  days, owned by the partner with activity in the last 30 days, or simply known. Known leads have
  their Google fields refreshed by the search.
- **New listings whose number is on the DNC list** are stored immediately with status `dnc` so they
  can never be served.
- **Phone check:** leads with no number *or an invalid number* are dropped unless
  `--include-no-phone`. Mobiles set `entity_type=likely_sole_trader` until Companies House finds a
  limited company.
- **Only qualified leads are stored** (plus linked duplicates and DNC-blocked listings). Results
  dropped as out of area, wrong category or chain are not saved.
- **"N new leads likely"** in the repeat-search warning is a rough estimate: about 5% of the earlier
  raw results per week since that search. Google's results barely change in two weeks.
- `--no-audit` skips both the website audit *and* the email lookup (both fetch websites). Companies
  House still runs if the key is set.
- Google request usage is committed as each request is made, outside the final save transaction, so
  the free-tier counts stay accurate even if a search fails partway.

## Website audit

- Social/link-in-bio, directory and booking pages (Facebook, Instagram, Linktree, Yell,
  Checkatrade, Bark, Google Business, plus a few similar ones such as Fresha and Treatwell) count as
  `social_only`. The lists are in `website_rules.yaml`.
- An `http://` URL that fails to load is tried once over `https://`.
- **"Poor" (major) issues:** fails to load, times out, bad certificate, no HTTPS, no viewport,
  PageSpeed below 50, copyright more than 3 years old, parked or for sale, 3+ broken links, Flash or
  frames. **Minor issues:** copyright more than 2 years old, 1-2 broken links, PageSpeed 50-69, no
  title, no meta description, no visible phone, no contact form, table-based layout. 1-2 minor
  issues = `ok`, none = `good`, 3+ minor = `poor`. All thresholds are in `website_rules.yaml`.
- Weak "coming soon"/"under construction" phrases only count as parked when the page has very
  little text, so a real site saying "new menu coming soon" isn't flagged.
- When `robots.txt` blocks us, we don't fetch the page. PageSpeed still runs (Google fetches it, not
  us), and the status is `ok` rather than `good`, since we couldn't check it.
- "Website by/designed by …" credits naming a platform (WordPress, Wix, Squarespace…) are ignored.
  Only agency names are stored as `existing_agency`, which lowers the score slightly.
- **AI judgement** calls the Anthropic Messages API with raw `httpx` (as the brief asked for httpx),
  using structured JSON output (`output_config.format` with a JSON schema) and `effort: low`.
  Responses are validated (score 1-10, short strings). Any failure is ignored silently. AI calls
  happen after the free checks, capped by `AI_MAX_PER_SEARCH`.
- **Hooks** say "stars" rather than ★ so they read well in emails and the old console. The Dial page
  shows the AI verdict as the main hook when there is one. `{hook}` in emails uses the rule-based
  hook, so the wording is predictable.

## Scoring

- Website: none/social 60, free builder 48, poor 45, unaudited 30, ok 25, good 8. Reviews 20-200
  +20, 10-19 +12, 201-500 +10, 1-9 +4, 500+ nothing (too big). Rating ≥4.5 +10, ≥4.0 +6, ≥3.5 +2,
  <3.5 -15. No phone -25, invalid -15, mobile +3. Ghost listing -20, existing agency -8, AI dated
  score ≥7 +5. Clamped to 0-100. It's all in `leadfinder/scoring.py`.

## Calling

- **Outcome → status:** no answer / gatekeeper → `called`; connected → `interested`; callback →
  `callback`; meeting booked → `meeting_booked`; not interested → `not_interested`; wrong number →
  `wrong_number` (closed). A routine call doesn't move a `proposal_sent` or `won` lead backwards.
- **"Connects"** (connect rate, leaderboard) = connected, meeting booked and not interested: the
  outcomes where you spoke to the decision-maker. Callback and gatekeeper don't count (often the
  gatekeeper asks you to ring back).
- **Conversion to interested** (`stats`) = leads with a connected or meeting-booked outcome ÷ leads
  dialled.
- A callback without a date defaults to 10:00 tomorrow. **Meeting booked requires a date/time**, so
  the meeting and calendar entries are always real. A bare date means 10:00. All entered times are
  London time.
- **Queue order for `next` and the Dial page:** 1) my callbacks due by end of today; 2) new leads
  that are mine or unowned, by score; 3) leads called before (no answer/gatekeeper/interested), not
  called in the last 4 hours, by score; 4) the partner's new leads untouched for 30+ days; 5) leads
  with 3+ no-answers in a row. Never: closed, TPS-registered, DNC, no phone, duplicates, or leads
  soft-locked by the other person.
- **Ownership:** a search assigns its leads to whoever ran it. Whoever logs the *first* call on a
  lead becomes its owner (this overrides the search owner). It can be reassigned in the side panel
  or with `leadfinder assign`.
- **Soft lock:** opening a lead (Dial page, `/dial?lead=…` or CLI `next`) locks it to you for 10
  minutes. Logging an outcome releases the lock. "Skip" (N) hides a lead from your queue for 30
  minutes.
- Call duration comes from the Dial page timer (it starts when you click "Calling…" or tap the
  number), or `--duration` in the CLI. Ended time = when the outcome was logged.
- The Dial page's "Do not call" button logs a call with outcome `dnc` (counted as a dial, not a
  connect), adds the number to DNC and closes the lead.

## Email

- Drafts are created for every lead with an email (sole traders included). They show a visible
  block reason and `send` refuses them, as the brief describes. Unknown company types are blocked
  too: only `limited_company` can be emailed.
- The footer is added by code, not the template, so it can't be deleted. Edits that remove the STOP
  line are rejected. The 120-word limit applies to the body excluding the footer. A draft that would
  be longer is skipped with a message telling you to shorten the template.
- Lines containing `{preview_url}` are left out automatically when the lead has no preview. Lines
  starting with `#` are template comments.
- `review`'s *skip* leaves a draft pending; *delete* marks it deleted (kept for the record).
- The daily limit is shared by both partners and counted from sent emails in the database (London
  calendar day). Settings can override `DAILY_SEND_LIMIT`.
- "No reply" for follow-ups means you haven't marked the lead with `leadfinder replied <id>`; the
  tool doesn't read your inbox. A lead gets at most one first email and one follow-up.
- Dashboard "Send approved" sends in a background thread (60-180 s between emails), so the page
  doesn't hang.
- SMTP: port 465 uses SSL; any other port uses STARTTLS.

## Dashboard

- Session cookie = signed with `SECRET_KEY` (itsdangerous). Expiry (12 h, or 30 days with "Keep me
  logged in") is checked against the server clock. CSRF tokens live in the session; plain forms post
  a hidden field and HTMX sends an `X-CSRF-Token` header. The login form is CSRF-protected too, via
  a pre-login session.
- Lockout counts failures **per name** (whether or not the account exists), so names can't be
  probed. A non-existent name is hashed against a dummy hash to keep the timing similar.
- Type-ahead uses native HTML `<datalist>` suggestions: trades from `trades.yaml`, towns already
  searched first, then the starter list. It needs no JavaScript library.
- Searches started from the UI run in a background thread and the form polls for progress each
  second. Job progress is kept in memory, so it's lost if the server restarts mid-search (the leads
  themselves are saved by then).
- Charts are monochrome Chart.js: the outcome bars use green for meetings, red for not interested
  and amber for callbacks; everything else is neutral. The heatmaps are CSS grids with a one-hue
  scale and hover tooltips. A table view sits under the outcome chart.
- "Pipeline value" = (meetings booked + proposals sent) × average won setup fee (falls back to
  £800 until the first deal).
- "Close rate" = deals won ÷ meetings marked *held* in the period. Meetings become *held* when you
  record an outcome on the Meetings page, or when the lead moves to proposal/won/lost.
- The sidebar "Recent" list shows one line per lead (its latest action), mine by default, with a
  Team toggle remembered in a cookie.
- The theme and the collapsed sidebar are remembered in localStorage.

## Meetings, previews, backups

- `.ics` files include a full `VTIMEZONE` for Europe/London, escaped text and folded lines. Google
  Calendar links use UTC times.
- Meeting outcomes: *Proposal* (held → proposal sent), *Lost* (held → lost), *No-show* (lead goes
  back to callbacks, due now).
- Preview templates are Jinja files in `templates/previews/` (one per trade family; the family comes
  from `trades.yaml`). Inline CSS, no images or external requests, mobile-first. "Export folder"
  downloads a zip. Netlify publishing creates a site on first publish and redeploys the same site
  afterwards.
- **Backups:** the daily backup runs the first time any command (except `user`/`login`/`whoami`) or
  the dashboard is used each day. Retention keeps the newest backup of each of the last 30 days that
  have one, plus the newest of each of the last 12 months. Extra same-day backups ("Back up now")
  are trimmed to the newest per day. The pre-restore safety backup skips pruning, so the backup
  you're restoring can't be deleted by its own safety copy. Restore only accepts file names inside
  `BACKUP_DIR`.

## Accounts and CLI attribution

- `leadfinder login` stores a **signed** token (not the password) in `.leadfinder-user.json` in the
  project folder (git-ignored). So the CLI identity is per project folder, i.e. per PC.
- Read-only commands (`list`, `show`, `stats`, `export`, `meetings`, `dnc list`, `backup list`,
  `trades`) work without logging in. Everything that changes data, plus `next` (which soft-locks a
  lead), requires a login.
- Tests generate a random password at runtime. There are no default or stored passwords anywhere.

## Things not done or worth knowing

- **Live smoke test:** there was no `.env` with a real `GOOGLE_API_KEY` in this environment, so the
  `search "barbers" --town "Crawley" --max 5` smoke test was skipped. All external calls are written
  to the documented APIs and covered by fake-client tests.
- **Pushing:** the code is committed on branch `leadfinder-v1`, but pushing to GitHub from this
  session was refused (the session had read-only access to `edison-hub318/vibora-leadfinder`).
- The free allowances in `free_tier.yaml` (1,000 Enterprise requests per SKU per month, 5,000 Pro)
  match the brief. Confirm them on Google's pricing page.
- The UI has been checked at 1366×768 and phone width in Chromium. It hasn't been tested on a real
  Windows machine. The code avoids Unix-only modules and uses `pathlib` and explicit encodings
  throughout.
