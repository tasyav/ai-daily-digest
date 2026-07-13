"""
Daily AI/DS Digest
------------------
Fetches AI/DS news from a fixed allowlist of reliable sources (official blogs,
arXiv, mainstream tech news), keeps only items from the last 48 hours,
de-duplicates against previously-sent items, summarizes with Gemini using a
strictly grounded prompt (no outside knowledge, must cite a real source URL),
validates the model's output against the fetched items to catch hallucinations,
and emails the result via Gmail SMTP.
"""

import os
import json
import smtplib
import feedparser
import requests
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import google.generativeai as genai

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

FRESHNESS_HOURS = 48
SENT_LOG_PATH = "sent_log.json"
SENT_LOG_RETENTION_DAYS = 14  # how long we remember URLs to avoid re-sending

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
RECIPIENT_EMAILS = [
    e.strip() for e in os.environ.get("RECIPIENT_EMAIL", GMAIL_ADDRESS).split(",") if e.strip()
]

# Fixed allowlist of reliable RSS sources, tagged by category so the digest can
# build separate sections instead of letting high-volume feeds (arXiv) drown out
# everything else. Add/remove freely.
RSS_SOURCES = {
    # category "tools": official lab blogs announcing products/features/models
    "OpenAI Blog": ("tools", "https://openai.com/news/rss.xml"),
    "Anthropic News": ("tools", "https://www.anthropic.com/news/rss.xml"),
    "Google DeepMind Blog": ("tools", "https://deepmind.google/blog/rss.xml"),
    "Meta AI Blog": ("tools", "https://ai.meta.com/blog/rss/"),
    "Microsoft Research": ("tools", "https://www.microsoft.com/en-us/research/feed/"),

    # category "research": arXiv papers
    "arXiv cs.AI": ("research", "http://export.arxiv.org/rss/cs.AI"),
    "arXiv cs.CL": ("research", "http://export.arxiv.org/rss/cs.CL"),
    "arXiv cs.LG": ("research", "http://export.arxiv.org/rss/cs.LG"),

    # category "news": mainstream tech journalism with editorial standards
    "Ars Technica AI": ("news", "https://arstechnica.com/tag/ai/feed/"),
    "The Verge AI": ("news", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    "Reuters Technology": ("news", "https://www.reutersagency.com/feed/?best-topics=tech"),
}

# Cap how many fresh entries we take from each individual feed, so a single
# high-volume source (arXiv routinely posts 100+ papers/day) can't flood the
# catalog and crowd out everything else before it even reaches the model.
MAX_ITEMS_PER_SOURCE = 12

HN_API_TOP = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_API_ITEM = "https://hacker-news.firebaseio.com/v0/item/{}.json"
HN_AI_KEYWORDS = ["ai", "llm", "gpt", "openai", "anthropic", "claude", "gemini",
                  "machine learning", "neural", "deepmind", "mistral", "transformer"]


# ---------------------------------------------------------------------------
# FETCHING
# ---------------------------------------------------------------------------

def parse_date(entry):
    """Return a timezone-aware UTC datetime for a feed entry, or None."""
    for field in ("published_parsed", "updated_parsed"):
        val = getattr(entry, field, None)
        if val:
            return datetime(*val[:6], tzinfo=timezone.utc)
    return None


def fetch_rss_items():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=FRESHNESS_HOURS)
    items = []
    for source_name, (category, url) in RSS_SOURCES.items():
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[warn] failed to fetch {source_name}: {e}")
            continue

        source_items = []
        for entry in feed.entries:
            pub_date = parse_date(entry)
            if not pub_date or pub_date < cutoff:
                continue
            summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
            source_items.append({
                "source": source_name,
                "category": category,
                "title": entry.title,
                "link": entry.link,
                "summary": summary[:600],  # cap length fed to the LLM
                "published": pub_date.isoformat(),
            })

        source_items.sort(key=lambda it: it["published"], reverse=True)
        items.extend(source_items[:MAX_ITEMS_PER_SOURCE])
    return items


def fetch_hn_items(limit_checked=150):
    """Pull recent HN front-page stories filtered to AI-related keywords."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=FRESHNESS_HOURS)
    items = []
    try:
        top_ids = requests.get(HN_API_TOP, timeout=10).json()[:limit_checked]
    except Exception as e:
        print(f"[warn] failed to fetch HN top stories: {e}")
        return items

    for story_id in top_ids:
        try:
            story = requests.get(HN_API_ITEM.format(story_id), timeout=10).json()
        except Exception:
            continue
        if not story or "title" not in story:
            continue

        title_lower = story["title"].lower()
        if not any(kw in title_lower for kw in HN_AI_KEYWORDS):
            continue

        pub_date = datetime.fromtimestamp(story.get("time", 0), tz=timezone.utc)
        if pub_date < cutoff:
            continue

        link = story.get("url", f"https://news.ycombinator.com/item?id={story_id}")
        items.append({
            "source": "Hacker News",
            "category": "news",
            "title": story["title"],
            "link": link,
            "summary": f"Discussion score: {story.get('score', 0)} points, "
                       f"{story.get('descendants', 0)} comments.",
            "published": pub_date.isoformat(),
        })
    return items


# ---------------------------------------------------------------------------
# DEDUPLICATION
# ---------------------------------------------------------------------------

def load_sent_log():
    if os.path.exists(SENT_LOG_PATH):
        with open(SENT_LOG_PATH, "r") as f:
            return json.load(f)
    return {}


def save_sent_log(log):
    cutoff = datetime.now(timezone.utc) - timedelta(days=SENT_LOG_RETENTION_DAYS)
    trimmed = {
        url: ts for url, ts in log.items()
        if datetime.fromisoformat(ts) > cutoff
    }
    with open(SENT_LOG_PATH, "w") as f:
        json.dump(trimmed, f, indent=2)


def dedup_items(items, sent_log):
    return [item for item in items if item["link"] not in sent_log]


# ---------------------------------------------------------------------------
# SUMMARIZATION (grounded, hallucination-resistant)
# ---------------------------------------------------------------------------

def build_prompt(items):
    catalog = "\n\n".join(
        f"[{i}] CATEGORY: {it['category']}\nSOURCE: {it['source']}\nTITLE: {it['title']}\n"
        f"LINK: {it['link']}\nCONTENT: {it['summary']}"
        for i, it in enumerate(items)
    )

    return f"""You are the lead writer of a widely-read AI newsletter, in the style of
Stratechery or Platformer: opinionated, sharp, allergic to filler. You must ONLY use the
information in the ITEM CATALOG below. Do not use any outside knowledge. Do not invent
facts, sources, numbers, or links that are not explicitly present in the catalog.

ITEM CATALOG (each item tagged with a CATEGORY of "tools", "research", or "news"):
{catalog}

WRITING STYLE — this is graded, follow it closely:
- Open each summary with the concrete result, a striking number, or the real-world
  implication — never with a throat-clearing phrase like "Researchers introduce...",
  "This work proposes...", "The paper presents...", or "A new study...". If you catch
  yourself about to write one of those, rewrite the sentence.
- No two summaries in the whole email may start with the same construction. Vary your
  openings.
- Write like you're telling a sharp friend why this matters, not like you're paraphrasing
  an abstract. Use active voice and concrete language. Cut jargon where a plain word works.
- Liven up the DELIVERY, never the substance — every claim must still be traceable to the
  item's CONTENT field.

TASK — build FOUR sections:
1. "story_of_the_day": the single most significant item in the whole catalog, any
   category. 3-4 sentences, written per the style rules above, explaining what happened
   and why it actually matters. Include its exact SOURCE and LINK.
2. "ai_tools": 2 to 4 items with CATEGORY "tools" — new products, features, or model
   releases. One punchy sentence each, plus exact SOURCE and LINK. Leave the list empty
   if the catalog has no usable "tools" items.
3. "research": 2 to 3 items with CATEGORY "research" — pick the most consequential
   papers, not just the first ones listed. One punchy sentence each (translate the
   result into plain English, skip academic throat-clearing), plus exact SOURCE and LINK.
4. "the_news": 2 to 4 items with CATEGORY "news" — industry/market/policy stories. One
   punchy sentence each, plus exact SOURCE and LINK.

Do not reuse the item picked for "story_of_the_day" in any other section.

RULES:
- Every "link" you output MUST be copied exactly, character-for-character, from the
  catalog above. Never modify, guess, or shorten a link.
- If a section has fewer than 2 genuinely usable items in its category, include only what's
  real (even zero) — never pad with off-category or repeated items — and set
  "insufficient_items" to true.

Respond with ONLY valid JSON, no markdown fences, no preamble, matching this schema:
{{
  "story_of_the_day": {{
    "title": "...",
    "summary": "...",
    "source": "...",
    "link": "..."
  }},
  "ai_tools": [
    {{"summary": "...", "source": "...", "link": "..."}}
  ],
  "research": [
    {{"summary": "...", "source": "...", "link": "..."}}
  ],
  "the_news": [
    {{"summary": "...", "source": "...", "link": "..."}}
  ],
  "insufficient_items": false
}}
"""


def summarize_with_gemini(items):
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(
        "gemini-flash-latest",
        generation_config={"temperature": 0.15, "response_mime_type": "application/json"},
    )
    prompt = build_prompt(items)
    response = model.generate_content(prompt)
    return json.loads(response.text)


def validate_against_catalog(result, items):
    """Drop any story whose link wasn't actually in the fetched catalog (anti-hallucination check)."""
    valid_links = {it["link"] for it in items}

    sod = result.get("story_of_the_day")
    if sod and sod.get("link") not in valid_links:
        print(f"[warn] Story of the day link not found in catalog, dropping: {sod.get('link')}")
        result["story_of_the_day"] = None

    # Track links already used in this email so no story appears twice,
    # in any section or as a repeat of the story of the day.
    used_links = set()
    if result.get("story_of_the_day"):
        used_links.add(result["story_of_the_day"]["link"])

    for section in ("ai_tools", "research", "the_news"):
        filtered = []
        for u in result.get(section, []):
            link = u.get("link")
            if link not in valid_links:
                print(f"[warn] Dropped '{section}' item with unverifiable link: {link}")
                continue
            if link in used_links:
                print(f"[warn] Dropped '{section}' item duplicated within this email: {link}")
                continue
            used_links.add(link)
            filtered.append(u)
        result[section] = filtered
    return result


# ---------------------------------------------------------------------------
# EMAIL FORMATTING + SENDING
# ---------------------------------------------------------------------------

def build_section_html(label, section_items):
    if not section_items:
        return ""
    html = (
        f"<div style='font-size:11px; letter-spacing:1.5px; text-transform:uppercase; "
        f"color:#888; margin-top:28px;'>{label}</div>"
        "<ul style='font-size:15px; line-height:1.7; padding-left:20px; color:#222;'>"
    )
    for u in section_items:
        html += (
            f"<li style='margin-bottom:8px;'>{u['summary']} "
            f"<span style='color:#666; font-size:13px;'>({u['source']} — "
            f"<a href='{u['link']}' style='color:#1a1a2e;'>link</a>)</span></li>"
        )
    html += "</ul>"
    return html


def build_email_html(result, today_str):
    sod = result.get("story_of_the_day")
    FONT = "'Times New Roman', Times, serif"

    sod_html = ""
    if sod:
        sod_html = f"""
        <div style="border-left:4px solid #1a1a2e; padding:4px 20px; margin:24px 0;">
          <div style="font-size:11px; letter-spacing:1.5px; text-transform:uppercase; color:#888;">Story of the Day</div>
          <h2 style="margin:6px 0 10px 0; font-size:22px; color:#1a1a2e;">{sod['title']}</h2>
          <p style="font-size:16px; line-height:1.6; color:#222;">{sod['summary']}</p>
          <p style="font-size:13px; color:#666;">{sod['source']} —
          <a href="{sod['link']}" style="color:#1a1a2e;">{sod['link']}</a></p>
        </div>
        """
    else:
        sod_html = (
            "<p style='font-style:italic; color:#666;'>"
            "No standout story met the freshness/reliability bar today.</p>"
        )

    sections_html = (
        build_section_html("AI Tools", result.get("ai_tools", []))
        + build_section_html("What's the News", result.get("the_news", []))
        + build_section_html("Research", result.get("research", []))
    )

    if result.get("insufficient_items"):
        sections_html += (
            "<p style='color:#a00; font-size:13px;'>"
            "Note: fewer reliable, fresh items were available than usual today, "
            "so this digest is shorter than normal rather than padded with filler.</p>"
        )

    return f"""
    <html><body style="font-family:{FONT}; max-width:640px; margin:auto; padding:12px;">
      <div style="border-bottom:2px solid #1a1a2e; padding-bottom:10px;">
        <h1 style="font-size:24px; margin:0; color:#1a1a2e;">AI & Data Science Daily</h1>
        <div style="font-size:13px; color:#888;">{today_str}</div>
      </div>
      {sod_html}
      {sections_html}
      <hr style="border:none; border-top:1px solid #ddd; margin-top:28px;"/>
      <p style="font-size:11px; color:#999;">
        Sources restricted to official AI lab blogs, arXiv, and mainstream tech news.
        Only items from the last 48 hours are included.
      </p>
    </body></html>
    """


def send_email(html_body, today_str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"AI & Data Science Daily — {today_str}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ", ".join(RECIPIENT_EMAILS)
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, RECIPIENT_EMAILS, msg.as_string())


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    print("Fetching items...")
    items = fetch_rss_items() + fetch_hn_items()
    print(f"Fetched {len(items)} items within {FRESHNESS_HOURS}h window.")

    sent_log = load_sent_log()
    items = dedup_items(items, sent_log)
    print(f"{len(items)} items remain after de-duplication.")

    if not items:
        print("No fresh, un-sent items found. Skipping email today.")
        return

    print("Summarizing with Gemini...")
    result = summarize_with_gemini(items)

    print("Validating output against fetched catalog (anti-hallucination check)...")
    result = validate_against_catalog(result, items)

    today_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    html_body = build_email_html(result, today_str)

    print("Sending email...")
    send_email(html_body, today_str)
    print("Email sent.")

    # Mark everything we actually surfaced (or considered) as sent
    now_iso = datetime.now(timezone.utc).isoformat()
    for it in items:
        sent_log[it["link"]] = now_iso
    save_sent_log(sent_log)
    print("Sent log updated.")


if __name__ == "__main__":
    main()
