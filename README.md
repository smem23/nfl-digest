# nfl-digest

Automated daily NFL email digest — scores, standings, storylines, schedule and a
stat of the day. Runs on GitHub Actions on a schedule; no machine of your own
needs to stay on.

- **Runner:** GitHub Actions (free tier)
- **Schedule:** daily, targeting **11:00 Europe/Dublin**
- **Data:** ESPN's public (unofficial) API for scores/standings + RSS feeds for headlines
- **Summariser:** Anthropic API (Claude) turns the raw data into the written digest
- **Delivery:** email via the [Resend](https://resend.com) API (free tier)
- **Secrets:** GitHub encrypted repository secrets — never committed

## Digest format

Plain text, phone-readable in under two minutes, always in this order:

1. **Scores** — last slate's finals, one line each, notable items flagged
2. **Standings snapshot** — division standings, compact
3. **Storylines** — 3–5 bullets on what's being talked about
4. **This week's schedule** — upcoming games with a one-line "why it matters"
5. **Stat of the day** — one interesting number

A per-team **"Your Team"** section is stubbed out (`TEAM` + `build_team_section()`
in `digest.py`) and can be switched on later without restructuring anything.

## Setup

### 1. Resend account + API key

1. Sign up at <https://resend.com> — **use the address you want the digest
   delivered to** (e.g. `hello@sammemery.com`). Resend's shared sender
   (`onboarding@resend.dev`) only delivers to the account owner's address until
   you verify your own domain, so this needs to match `RECIPIENT_EMAIL`.
2. **API Keys → Create API Key** → name it `nfl-digest`, permission "Sending
   access" → copy the `re_...` value.
3. *(Optional, later)* verify a domain under **Domains** and then set the
   `EMAIL_FROM` secret to `NFL Digest <nfl@yourdomain.com>` to send from your own
   address and to any recipient.

### 2. Anthropic API key

Get a key from <https://console.anthropic.com/> → **API Keys**, and add a payment
method / credit under **Billing**. The digest uses the model set in `digest.py`
(`MODEL`, currently `claude-opus-5`; switch to `claude-sonnet-5` for a cheaper
run). Cost is a fraction of a cent per day.

### 3. Repository secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
| --- | --- |
| `ANTHROPIC_API_KEY` | your Anthropic key (`sk-ant-...`) |
| `RESEND_API_KEY` | your Resend key (`re_...`) |
| `RECIPIENT_EMAIL` | where the digest should land (= your Resend signup email) |
| `EMAIL_FROM` | *(optional)* custom From once you've verified a domain in Resend |

### 4. Run it manually first

Repo → **Actions → NFL Daily Digest → Run workflow**. Confirm it goes green and
the email arrives before relying on the schedule.

## Local testing

```bash
pip install -r requirements.txt

# 1. fetch data only — no Claude, no email
DIGEST_SKIP_LLM=1 python digest.py

# 2. build the digest with Claude, print it, don't email
#    (needs ANTHROPIC_API_KEY in your environment)
DIGEST_DRY_RUN=1 ANTHROPIC_API_KEY=sk-ant-... python digest.py

# 3. full run — actually sends the email
export ANTHROPIC_API_KEY=sk-ant-...
export RESEND_API_KEY=re_...
export RECIPIENT_EMAIL=you@example.com
python digest.py
```

## How the schedule handles DST

Cron in GitHub Actions is UTC and doesn't follow daylight saving. The workflow
fires at **both** 10:00 and 11:00 UTC; `digest.py` checks the actual
`Europe/Dublin` local time and the run that isn't at 11:00 exits immediately
without sending. Set `DIGEST_FORCE=1` to bypass that guard when testing.

## Error handling

If a data source fails, the digest still sends with a note saying what was
missing. If *every* source fails, or the Claude call fails, you get an email
saying so (with the raw data attached) rather than silence.
