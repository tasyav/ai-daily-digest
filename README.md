# Daily AI/DS Digest

Sends you a daily email with one "Story of the Day" (3-4 sentences) plus 5-7 other
AI/data-science updates — all sourced from official AI lab blogs, arXiv, mainstream
tech journalism, and Hacker News, restricted to items from the last 48 hours.

## How it stays reliable and hallucination-free

- **Fixed source allowlist** — no random blogs, no Medium, no low-quality aggregators.
- **48-hour freshness filter** — enforced in code before anything reaches the LLM.
- **Grounded summarization** — Gemini is only allowed to summarize the text it's given,
  with an explicit instruction not to use outside knowledge.
- **Link validation** — after Gemini responds, the script checks every link it cited
  against the actual fetched articles. Any link that doesn't match exactly is dropped.
- **Honest fallback** — if too few good items are found, the email says so instead of
  padding with filler.

## One-time setup

### 1. Create a Gmail App Password
Your normal Gmail password won't work for SMTP. You need an "App Password":
1. Go to your Google Account → Security → 2-Step Verification (must be enabled).
2. Go to https://myaccount.google.com/apppasswords
3. Create a new app password (name it e.g. "AI Digest"), copy the 16-character code.

### 2. Get a free Gemini API key
1. Go to https://aistudio.google.com/apikey
2. Create an API key (free tier is generous — plenty for one summarization call/day).

### 3. Create a GitHub repo
Push this folder to a new **private** GitHub repository.

### 4. Add repo secrets
In your repo: Settings → Secrets and variables → Actions → New repository secret.
Add these four:

| Secret name | Value |
|---|---|
| `GEMINI_API_KEY` | Your Gemini API key from step 2 |
| `GMAIL_ADDRESS` | Your Gmail address (used to send) |
| `GMAIL_APP_PASSWORD` | The 16-character app password from step 1 |
| `RECIPIENT_EMAIL` | The email(s) you want the digest sent to (can be the same Gmail). For multiple recipients, separate with commas, e.g. `a@x.com,b@y.com` |

### 5. Enable the workflow
Go to the **Actions** tab in your repo → you should see "Daily AI Digest" →
click "Enable workflow" if prompted.

### 6. Test it manually
Actions tab → "Daily AI Digest" → "Run workflow" button → run it once to confirm
you get an email. Check the run logs if something fails — they'll tell you exactly
which step broke.

### 7. Adjust the send time (optional)
Edit the cron line in `.github/workflows/daily-digest.yml`:
```yaml
- cron: '0 13 * * *'  # minute hour day month weekday, all in UTC
```
Use https://crontab.guru to convert your local time zone to UTC.

## Local testing (optional)

You can also run it on your own machine before pushing to GitHub:

```bash
pip install -r requirements.txt
export GEMINI_API_KEY="..."
export GMAIL_ADDRESS="you@gmail.com"
export GMAIL_APP_PASSWORD="..."
export RECIPIENT_EMAIL="you@gmail.com"
python daily_digest.py
```

## Customizing sources

Edit the `RSS_SOURCES` dictionary at the top of `daily_digest.py` to add or remove
feeds. Any source with a valid RSS/Atom feed works — just make sure it's something
you'd actually trust.

## Files

- `daily_digest.py` — the full pipeline (fetch → dedup → summarize → validate → email)
- `requirements.txt` — Python dependencies
- `sent_log.json` — tracks previously-sent article URLs so you don't get repeats
  (auto-updated and committed back by the GitHub Action after each run)
- `.github/workflows/daily-digest.yml` — the daily cron schedule
