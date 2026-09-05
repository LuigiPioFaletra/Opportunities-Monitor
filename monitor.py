#!/usr/bin/env python3
"""
Opportunities Monitor
=============

Monitors a set of public web pages for new PhD programs, job/competition
announcements and civil service ("Servizio Civile") calls, and sends a
Telegram notification whenever something new appears.

Sources are declared in sources.json. Each source is checked independently;
state (which items were already seen, error counters, ...) is persisted in
state.json so that only genuinely new items are reported.

Environment variables (set as GitHub Actions secrets, see README.md):
    BOT_TOKEN   Telegram bot token
    CHAT_ID     Telegram chat/channel id to notify

Designed to be run on a schedule (e.g. via GitHub Actions, twice a day),
independent of local machine availability.
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCES_FILE = os.path.join(BASE_DIR, "sources.json")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
HEARTBEAT_FILE = os.path.join(BASE_DIR, "heartbeat.json")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5

# Minimum time between two requests to the *same* host, regardless of which
# source triggered them. A single run can hammer the same domain with many
# back-to-back requests (e.g. pg_sc_site_scan alone hits 7 different
# politichegiovanili.gov.it URLs one after another, on top of the 3 other
# listing sources on that same host) with no session/cookie continuity
# between them - a pattern that reads as automated traffic to most WAFs and
# can trigger a temporary, silent block (connection attempts just get
# dropped) even though a single isolated request to the same page succeeds
# without any issue. Spacing requests out and reusing one Session (below)
# so cookies persist across requests makes this look much more like normal
# browsing and much less like a scraper.
MIN_HOST_INTERVAL_SECONDS = 2.5

ERROR_NOTIFY_THRESHOLD = 2   # consecutive failures before alerting
HEARTBEAT_DAYS = 7           # "still alive" confirmation every N days
MAX_DEGREE_CHECKS_PER_RUN = 15  # safety cap on detail-page fetches per source

# Degree classes Luigi cares about most (see README.md).
DEGREE_PATTERNS = {
    "L-8": re.compile(r"\bL[\s\-]?8\b", re.IGNORECASE),
    "LM-32": re.compile(r"\bLM[\s\-]?32\b", re.IGNORECASE),
}

SKIP_LINK_TEXT = {
    "leggi tutto", "read more", "continua a leggere", "home", "homepage",
    "vai al contenuto", "skip to content", "menu", "cerca", "search",
    "accedi", "login", "privacy", "cookie", "cookie policy",
    "note legali", "accessibilita", "accessibilità",
    "amministrazione trasparente", "contatti", "mappa del sito",
    "torna su", "condividi", "share", "stampa", "print",
}
SKIP_HREF_MARKERS = (
    "mailto:", "tel:", "javascript:", "facebook.com", "twitter.com",
    "x.com", "instagram.com", "linkedin.com", "youtube.com", "t.me",
    "play.google.com", "apps.apple.com", "wa.me", "whatsapp.com",
)

# AsmeLab (asmelab.it) publishes its "interpelli" (calls to fill a post from
# the ASMEL elenchi di idonei) as a small dhtmlx grid on the homepage. The
# grid's data comes from a plain GET endpoint that returns an XML feed and
# accepts the same filters as the on-page search boxes (campo1..campo5 map
# to ENTE/REGIONE/PROFILO/APERTURA/CHIUSURA left to right). This lets the
# feed be queried already filtered server-side (campo2=<regione>) instead of
# downloading every interpello nationwide.
#
# The endpoint always returns a fixed 12 rows per page regardless of the
# requested RecordXPage value (verified by requesting RecordXPage=500 and
# still getting 12 rows back), so pages have to be walked one at a time
# using the "page"/"prima" parameters until the "pagGrid" total reported in
# the feed itself is reached.
ASMELAB_XML_URL = "https://www.asmelab.it/anagrafiche/brInterpelliHomeXML.php"
ASMELAB_RECORDS_PER_PAGE = 12
ASMELAB_MAX_PAGES_SAFETY = 80  # hard stop in case pagGrid is ever wrong/missing
ASMELAB_COMUNI_PROVINCIA_FILE = os.path.join(BASE_DIR, "sicily_municipality_provinces.json")

ASMELAB_ROW_RE = re.compile(r'<row id="([^"]*)">(.*?)</row>', re.DOTALL)
ASMELAB_CELL_RE = re.compile(r"<cell[^>]*>\s*<!\[CDATA\[(.*?)\]\]>\s*</cell>", re.DOTALL)
ASMELAB_LINK_RE = re.compile(r"foo\('([^']+)'\)")
ASMELAB_PAGGRID_RE = re.compile(r'<userdata name="pagGrid">\s*(\d+)\s*</userdata>')


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)


def digest(obj):
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def clean_text(text):
    return re.sub(r"\s+", " ", text or "").strip()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

# One shared session for the whole run: connections are pooled/kept-alive
# and, importantly, cookies a site sets on the first request (session id,
# CSRF/verification tokens, ...) are carried over to the next request to
# the same host instead of every fetch looking like a brand-new, cookie-less
# visitor.
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9",
})

_last_request_at = {}  # hostname -> time.monotonic() of the last request sent

# GitHub-hosted runners draw from a large, rotating pool of shared IPs, so
# whether a given host blocks us or not can depend entirely on which IP this
# particular run happened to get - not on anything about the target site or
# our code. Fetching it once per run and embedding it in error messages
# means every future "unreachable" alert already carries the one piece of
# evidence needed to correlate failures with specific IPs/ASNs over time,
# without having to catch a failure with a manually-triggered diagnostic run.
_runner_ip_cache = None


def get_runner_ip():
    global _runner_ip_cache
    if _runner_ip_cache is None:
        try:
            _runner_ip_cache = requests.get("https://ifconfig.me", timeout=10).text.strip()
        except requests.RequestException:
            _runner_ip_cache = "sconosciuto"
    return _runner_ip_cache


def _throttle_for_host(host):
    last = _last_request_at.get(host)
    now = time.monotonic()
    if last is not None:
        wait = MIN_HOST_INTERVAL_SECONDS - (now - last)
        if wait > 0:
            time.sleep(wait)
    _last_request_at[host] = time.monotonic()


def fetch_html(url):
    """Downloads a page with retry logic. Returns (html, final_url)."""
    host = urlsplit(url).netloc
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        _throttle_for_host(host)
        try:
            resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            resp.encoding = resp.encoding or "utf-8"
            return resp.text, resp.url
        except requests.RequestException as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                # Back off a bit more on each retry instead of a fixed
                # delay, to ease off further if the previous attempt's
                # failure was itself a sign of throttling.
                time.sleep(RETRY_DELAY_SECONDS * attempt)
    raise RuntimeError(
        f"unreachable after {MAX_RETRIES} attempts from runner IP "
        f"{get_runner_ip()}: {last_error}"
    )


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def _strip_chrome(soup):
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "form"]):
        tag.decompose()
    return soup


def extract_listing_items(html, base_url):
    """
    Generic extractor for a "listing" page (announcements, PhD cycles, job
    postings, ...). Returns a dict {absolute_url: title}.

    Two passes are combined on purpose, because the sites monitored here use
    different markup for their listings (heading + separate "Leggi tutto"
    link on some pages, plain inline links on others) and no fixed set of
    CSS selectors can be assumed to stay accurate over time:

    1. heading-based: a <h2>-<h6> whose enclosing block contains a link is
       treated as one item, titled after the heading.
    2. link-based fallback: any remaining link with substantial text is
       treated as an item titled after the link text.
    """
    soup = _strip_chrome(BeautifulSoup(html, "lxml"))

    container = None
    for selector in ("main", "#content", ".entry-content", ".site-content", "article", "#primary"):
        candidate = soup.select_one(selector)
        if candidate and len(clean_text(candidate.get_text())) > 200:
            container = candidate
            break
    if container is None:
        container = soup.body or soup

    items = {}
    seen_hrefs = set()

    headings = container.find_all(re.compile(r"^h[2-6]$"))
    for idx, heading in enumerate(headings):
        title = clean_text(heading.get_text(" "))
        if len(title) < 6:
            continue

        link = heading.find("a", href=True)

        if link is None:
            # Look inside the closest wrapping block (typical "card"/"article" markup).
            parent = heading.find_parent(["article", "div", "li", "section"])
            if parent:
                link = parent.find("a", href=True)

        if link is None and heading.name in ("h4", "h5", "h6"):
            # Flat markup: heading and its link are siblings, not wrapped
            # together (h2/h3 are left out here on purpose: those levels
            # are commonly used as *section* headers grouping several plain
            # links below them - e.g. a PhD cycle heading followed by
            # several programs - rather than titling a single item, and
            # would otherwise be mis-captured as one bogus item each).
            next_heading = headings[idx + 1] if idx + 1 < len(headings) else None
            for node in heading.find_all_next():
                if node is next_heading:
                    break
                if node.name == "a" and node.has_attr("href"):
                    link = node
                    break

        if not link:
            continue
        href = urljoin(base_url, link["href"])
        if href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        items[href] = title

    for a in container.find_all("a", href=True):
        text = clean_text(a.get_text(" "))
        href = a["href"]
        if len(text) < 8 or text.lower() in SKIP_LINK_TEXT:
            continue
        if any(marker in href.lower() for marker in SKIP_HREF_MARKERS):
            continue
        full = urljoin(base_url, href)
        if full in seen_hrefs:
            continue
        seen_hrefs.add(full)
        items[full] = text

    return items


def extract_keyword_links(html, base_url, keywords):
    """Returns {absolute_url: text} for every link whose visible text or
    href contains one of the given (lowercase) keywords.

    Unlike extract_listing_items, this intentionally does NOT strip
    header/nav/footer: the whole point of the keyword scan is to catch a
    newly published Servizio Civile link wherever it appears on a hub page,
    including in a navigation menu or a "latest news" sidebar.
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    found = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(marker in href.lower() for marker in SKIP_HREF_MARKERS):
            continue
        text = clean_text(a.get_text(" "))
        haystack = f"{text} {href}".lower()
        if any(k in haystack for k in keywords):
            full = urljoin(base_url, href)
            found[full] = text or full
    return found


def detect_degree_classes(html):
    """Returns the subset of DEGREE_PATTERNS found in the given HTML's text."""
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ")
    hits = [label for label, pattern in DEGREE_PATTERNS.items() if pattern.search(text)]
    return hits


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram non configurato, messaggio non inviato:\n" + message)
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            data={"chat_id": CHAT_ID, "text": message, "disable_web_page_preview": True},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        print(f"Invio Telegram fallito: {exc}")


# --------------------------------------------------------------------------
# Per-source check
# --------------------------------------------------------------------------

def check_listing_source(source, old_entry):
    url = source["url"]
    html, final_url = fetch_html(url)
    new_items = extract_listing_items(html, final_url)

    old_items = (old_entry or {}).get("items", {}) if old_entry else {}
    is_cold_start = old_entry is None

    added_urls = [u for u in new_items if u not in old_items]

    degree_flags = {}
    if source.get("check_degree_class") and not is_cold_start and added_urls:
        for item_url in added_urls[:MAX_DEGREE_CHECKS_PER_RUN]:
            try:
                detail_html, _ = fetch_html(item_url)
                hits = detect_degree_classes(detail_html)
                if hits:
                    degree_flags[item_url] = hits
            except Exception:
                pass  # best effort only, never fails the run for this

    new_entry = {
        "items": new_items,
        "hash": digest(sorted(new_items.items())),
        "checked": now_iso(),
        "consecutive_errors": 0,
        "error_notified": False,
        "last_error": None,
    }

    event = None
    if not is_cold_start and added_urls:
        event = {
            "type": "update",
            "label": source["label"],
            "added": [(u, new_items[u], degree_flags.get(u, [])) for u in added_urls],
        }
    return new_entry, event


def check_keyword_scan_source(source, old_entry):
    keywords = [k.lower() for k in source["keywords"]]
    discovered = {}
    errors = []
    for hub_url in source["hub_urls"]:
        try:
            html, final_url = fetch_html(hub_url)
            discovered.update(extract_keyword_links(html, final_url, keywords))
        except Exception as exc:
            errors.append(f"{hub_url}: {exc}")

    if errors and len(errors) == len(source["hub_urls"]):
        # every single hub page failed -> treat as a full source error
        raise RuntimeError("; ".join(errors))

    old_items = (old_entry or {}).get("items", {}) if old_entry else {}
    is_cold_start = old_entry is None
    added_urls = [u for u in discovered if u not in old_items]

    new_entry = {
        "items": discovered,
        "hash": digest(sorted(discovered.items())),
        "checked": now_iso(),
        "consecutive_errors": 0,
        "error_notified": False,
        "last_error": None,
        "partial_errors": errors,
    }

    event = None
    if not is_cold_start and added_urls:
        event = {
            "type": "update",
            "label": source["label"],
            "added": [(u, discovered[u], []) for u in added_urls],
        }
    return new_entry, event


_asmelab_comuni_provincia_cache = None


def _asmelab_comuni_provincia():
    """comune (uppercase) -> province abbreviation, for every Sicilian
    comune. Loaded once from sicily_municipality_provinces.json (built from the
    public ISTAT-derived comuni-json dataset, so accents/apostrophes match
    the official comune names AsmeLab itself uses)."""
    global _asmelab_comuni_provincia_cache
    if _asmelab_comuni_provincia_cache is None:
        _asmelab_comuni_provincia_cache = load_json(ASMELAB_COMUNI_PROVINCIA_FILE, {})
    return _asmelab_comuni_provincia_cache


def fetch_asmelab_interpelli(region_filter):
    """Fetches every interpello row from AsmeLab's public XML feed, already
    filtered server-side by REGIONE. Returns {row_id: item_dict}."""
    items = {}
    page = 1
    total_pages = None
    while True:
        prima = (page - 1) * ASMELAB_RECORDS_PER_PAGE + 1
        url = (
            f"{ASMELAB_XML_URL}?prima={prima}&page={page}"
            f"&RecordXPage={ASMELAB_RECORDS_PER_PAGE}"
            f"&campo1=&campo2={region_filter}&campo3=&campo4=&campo5=&order="
        )
        xml_text, _ = fetch_html(url)

        if total_pages is None:
            match = ASMELAB_PAGGRID_RE.search(xml_text)
            total_pages = int(match.group(1)) if match else page

        rows = ASMELAB_ROW_RE.findall(xml_text)
        if not rows:
            break
        for row_id, body in rows:
            cells = [clean_text(c) for c in ASMELAB_CELL_RE.findall(body)]
            if not row_id or len(cells) < 7:
                continue
            comune, regione, profilo, apertura, chiusura, stato, bando_cell = cells[:7]
            link_match = ASMELAB_LINK_RE.search(bando_cell)
            items[row_id] = {
                "comune": comune,
                "regione": regione,
                "profilo": profilo,
                "apertura": apertura,
                "chiusura": chiusura,
                "stato": stato,
                "link": link_match.group(1) if link_match else "https://www.asmelab.it/",
            }

        if page >= total_pages or page >= ASMELAB_MAX_PAGES_SAFETY:
            break
        page += 1
    return items


def check_asmelab_interpelli_source(source, old_entry):
    items = fetch_asmelab_interpelli(source["region_filter"])
    comuni_provincia = _asmelab_comuni_provincia()

    old_items = (old_entry or {}).get("items", {}) if old_entry else {}
    is_cold_start = old_entry is None
    added_ids = [i for i in items if i not in old_items]

    new_entry = {
        "items": items,
        "hash": digest(sorted(items.items())),
        "checked": now_iso(),
        "consecutive_errors": 0,
        "error_notified": False,
        "last_error": None,
    }

    highlight_profile = (source.get("highlight_profile") or "").upper().strip()

    event = None
    if not is_cold_start and added_ids:
        added = []
        for row_id in added_ids:
            it = items[row_id]
            provincia = comuni_provincia.get(it["comune"].upper())
            comune_label = f"{it['comune']} ({provincia})" if provincia else it["comune"]
            is_match = bool(highlight_profile) and highlight_profile in it["profilo"].upper()
            marker = "⭐ TUO PROFILO - " if is_match else ""
            title = f"{marker}{comune_label} - {it['profilo']}"
            flags = [f"{it['stato']}, chiude {it['chiusura']}"]
            added.append((it["link"], title, flags, is_match))
        # Interpelli for the candidate's own profile are surfaced first, so
        # they don't get buried in a long batch of unrelated ones.
        added.sort(key=lambda entry: not entry[3])
        added = [entry[:3] for entry in added]
        event = {
            "type": "update",
            "label": source["label"],
            "added": added,
        }
    return new_entry, event


CHECKERS = {
    "listing": check_listing_source,
    "keyword_scan": check_keyword_scan_source,
    "asmelab_interpelli": check_asmelab_interpelli_source,
}


def check_one(source, state):
    source_id = source["id"]
    old_entry = state.get(source_id)
    checker = CHECKERS[source["type"]]

    # Some hosts (politichegiovanili.gov.it and its scelgoilserviziocivile.gov.it
    # companion) are known to fail intermittently for reasons outside our
    # control: GitHub-hosted runners share a large, rotating pool of IPs, and
    # if that host's WAF/anti-bot has one of those IPs greylisted, whichever
    # run happens to draw it will fail even though the site itself is fine.
    # A per-source override lets flaky hosts require more consecutive failed
    # *scheduled runs* (not retries within one run - those already happen
    # inside fetch_html) before we bother Luigi with an alert, while other,
    # well-behaved sources keep the tighter default threshold.
    threshold = source.get("error_notify_threshold", ERROR_NOTIFY_THRESHOLD)

    try:
        new_entry, event = checker(source, old_entry)
        was_erroring = bool(old_entry and old_entry.get("consecutive_errors", 0) >= threshold)
        if was_erroring and event is None:
            event = {"type": "recovered", "label": source["label"]}
        state[source_id] = new_entry
        return event
    except Exception as exc:
        consecutive = (old_entry or {}).get("consecutive_errors", 0) + 1
        error_notified = (old_entry or {}).get("error_notified", False)
        entry = dict(old_entry or {})
        entry["consecutive_errors"] = consecutive
        entry["last_error"] = str(exc)
        entry["checked"] = now_iso()
        should_notify = consecutive >= threshold and not error_notified
        if should_notify:
            entry["error_notified"] = True
        state[source_id] = entry
        if should_notify:
            return {"type": "error", "label": source["label"], "message": str(exc)}
        return None


# --------------------------------------------------------------------------
# Heartbeat
# --------------------------------------------------------------------------

def heartbeat_due(heartbeat):
    last = heartbeat.get("last_sent")
    if not last:
        return True
    last_dt = datetime.fromisoformat(last)
    return (datetime.now(timezone.utc) - last_dt).days >= HEARTBEAT_DAYS


def mark_heartbeat_sent(heartbeat):
    heartbeat["last_sent"] = now_iso()


# --------------------------------------------------------------------------
# Message building
# --------------------------------------------------------------------------

def format_update_block(event):
    lines = [f"- {event['label']}"]
    for url, title, degree_hits in event["added"]:
        flag = f" [{'/'.join(degree_hits)}]" if degree_hits else ""
        lines.append(f"  {title}{flag}")
        lines.append(f"  {url}")
    return "\n".join(lines)


def build_and_send_messages(events, sources_by_id, cold_start_summary):
    updates = [e for e in events if e and e["type"] == "update"]
    errors = [e for e in events if e and e["type"] == "error"]
    recovered = [e for e in events if e and e["type"] == "recovered"]

    if cold_start_summary:
        telegram(
            "Opportunities Monitor: nuove fonti inizializzate.\n\n"
            + cold_start_summary
            + "\n\nDa ora in poi ricevi un messaggio solo per le novita' reali su queste fonti."
        )

    if updates:
        blocks = [format_update_block(e) for e in updates]
        telegram("Nuovi bandi/avvisi trovati\n\n" + "\n\n".join(blocks))

    if errors:
        lines = [f"- {e['label']}: {e['message']}" for e in errors]
        telegram(
            "Attenzione: problemi nel controllo di alcune fonti "
            f"(dopo {ERROR_NOTIFY_THRESHOLD} tentativi falliti)\n\n" + "\n".join(lines)
        )

    if recovered:
        lines = [f"- {e['label']}" for e in recovered]
        telegram("Fonti tornate raggiungibili\n\n" + "\n".join(lines))


def build_heartbeat_message(state, sources, started_on):
    total_items = sum(len(entry.get("items", {})) for entry in state.values())
    lines = [
        "Opportunities Monitor operativo",
        "",
        f"Ultimo controllo: {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M UTC')}",
        f"Fonti monitorate: {len(sources)}",
        f"Elementi tracciati in totale: {total_items}",
        f"Monitor operativo da: {started_on}",
        "Nessun problema noto.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    sources = load_json(SOURCES_FILE, [])
    sources = [s for s in sources if s.get("enabled", True)]
    if not sources:
        print("Nessuna fonte abilitata in sources.json.")
        return 0

    state = load_json(STATE_FILE, {})
    heartbeat = load_json(HEARTBEAT_FILE, {})

    is_first_run_ever = not state
    cold_start_labels = []

    events = []
    for source in sources:
        if source["id"] not in state:
            cold_start_labels.append(source["label"])
        event = check_one(source, state)
        events.append(event)

    cold_start_summary = None
    if cold_start_labels:
        cold_start_summary = "Fonti inizializzate:\n" + "\n".join(f"- {l}" for l in cold_start_labels)

    sources_by_id = {s["id"]: s for s in sources}
    build_and_send_messages(events, sources_by_id, cold_start_summary)

    heartbeat.setdefault("started_on", datetime.now(timezone.utc).strftime("%d/%m/%Y"))
    if is_first_run_ever:
        # Start the 7-day heartbeat clock from here instead of firing one
        # right on the next run (the init message already confirmed things
        # are working).
        mark_heartbeat_sent(heartbeat)
    elif heartbeat_due(heartbeat):
        telegram(build_heartbeat_message(state, sources, heartbeat["started_on"]))
        mark_heartbeat_sent(heartbeat)

    save_json(STATE_FILE, state)
    save_json(HEARTBEAT_FILE, heartbeat)
    return 0


if __name__ == "__main__":
    sys.exit(main())
