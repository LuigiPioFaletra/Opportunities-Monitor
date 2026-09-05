# Opportunities Monitor

A Python script that watches a set of public web pages for new PhD
programs, job/competition announcements, civil service ("Servizio
Civile") calls and teacher supply-call notices, and sends a Telegram
notification whenever something new appears. Built on the same pattern as
[inPA-Monitor](https://github.com/LuigiPioFaletra/inPA-Monitor)
and [Java-Update-Monitor](https://github.com/LuigiPioFaletra/Java-Update-Monitor):
a stateless-looking script that keeps its memory in a small JSON file
committed back to the repository, run on a schedule by GitHub Actions so it
works independently of any local machine being on.

## What it monitors

All sources are declared in [`sources.json`](sources.json) and can be
edited without touching the code:

- **UKE Enna - PhD programs** (`didattica/dottorati-di-ricerca`)
- **UKE Enna - Job opportunities**, across all four `lavora-con-uke`
  sections (professors/researchers, contract lecturers, other roles,
  student opportunities). New postings on these pages are additionally
  checked for the strings `L-8` and `LM-32` (the degree classes of
  interest); a match is flagged inline in the Telegram message.
- **Politiche Giovanili - Avvisi e bandi**
- **Servizio Civile** - two category listing pages (presentation of
  programs/projects, volunteer selection calls) plus a lightweight
  site-wide scan across both `politichegiovanili.gov.it` and the dedicated
  `scelgoilserviziocivile.gov.it` portal: a handful of hub pages
  (homepages, section/news indexes) are scanned for any link whose text or
  URL contains "servizio civile" / "bando" / "avviso di selezione" /
  "operatori volontari" wording, so a brand-new call gets caught as soon as
  it's linked from anywhere on either site, without depending on a
  specific dated URL.

  Earlier drafts of this project also tracked a few individual dated call
  pages directly (e.g. `bando-ciechi-2026`, `bando_ordinario_2026`) as
  explicit sources. Those were removed once their application windows
  closed, on purpose: a dated call page is a moving target (a new URL
  every year) and, once published, should already show up as a new item
  on the `bandi-di-selezione-volontari` category listing above anyway, so
  a dedicated source for it is redundant - the category page plus the
  site-wide scan are enough to catch the next one without needing a yearly
  edit to `sources.json`.
- **Supplenze convocations** (`supplenze_convocazioni`): the MIUR page you
  originally listed (miurjb5.pubblica.istruzione.it) requires a personal
  SPID login, which cannot be automated safely or reliably from a
  scheduled script, so this source points instead at the public
  "Reclutamento docenti" notice feed of the Ufficio Scolastico Regionale
  Caltanissetta-Enna (`cl-en.usr.sicilia.it`), which lists supply-teaching
  and GPS-related notices (assegnazioni provvisorie, decreti GPS, avvisi
  supplenze, ...) without requiring login. That page is paginated (10
  items per page); the monitor only reads the first page, which is where
  new notices appear, so this is by design and not a bug.
- **AsmeLab - Interpelli elenchi idonei** (`asmelab_interpelli_sicilia`):
  AsmeLab (asmelab.it) publishes "interpelli" - individual municipalities
  drawing candidates from the national ASMEL elenchi di idonei - as a small
  grid on its homepage (ENTE / REGIONE / PROFILO / APERTURA / CHIUSURA /
  STATO / BANDO). That grid is fed by a plain, unauthenticated XML endpoint
  (`anagrafiche/brInterpelliHomeXML.php`) that accepts the same filters as
  the on-page search boxes, so this source queries it already filtered to
  `REGIONE=Sicilia` server-side (`region_filter` in `sources.json`) instead
  of pulling every interpello nationwide. New rows are matched against
  `sicily_municipality_provinces.json` (a comune -> province-abbreviation lookup
  for all 390 Sicilian comuni) so the Telegram message can show, per new
  interpello, the comune with its province and the profile being sought
  (e.g. "BOMPIETRO (PA) - ISTRUTTORE DI VIGILANZA ex Cat. C"), plus its
  status and closing date, with the announcement link taken from the
  grid's own "LEGGI" entry. If `highlight_profile` is set (a case-insensitive
  substring match against PROFILO, e.g. "FUNZIONARIO INFORMATICO"), any new
  interpello for that profile is prefixed with "⭐ TUO PROFILO -" and sorted
  to the top of the message, so it doesn't get lost in a batch of unrelated
  interpelli for other profiles.

  Being added to AsmeLab's elenchi di idonei does not mean an
  administration will reach out on its own: for each interpello a
  municipality opens, ASMEL sends a PEC to everyone on the relevant elenco,
  but it's on the candidate to actively submit an application within 15
  days of that PEC (login with SPID/CIE on asmelab.it, section "Elenco
  Interpelli"). This source exists precisely to catch a newly-opened
  Sicilian interpello as soon as it appears, as a backup to watching for
  the PEC itself.

## How it works

1. `monitor.py` fetches each enabled source (with retries and a browser
   User-Agent, same as the other two monitors).
2. For "listing" sources, it extracts the current set of items (title +
   link) from the page's main content, using a heading-based pass plus a
   link-text fallback, since these sites don't share a common markup and
   fixed CSS selectors would break too easily.
3. For the "keyword_scan" source, it extracts every link across the hub
   pages whose text or href matches the configured Servizio Civile
   keywords.
4. The resulting item set is compared against what was saved in
   `state.json` on the previous run. Items already known are ignored;
   items seen for the first time are reported.
5. On the very first run for a given source, the current items are saved
   as the baseline (so you don't get a Telegram message listing every
   historical announcement at once) and a short "source initialized"
   message is sent instead.
6. If a source is unreachable for two consecutive checks, a Telegram alert
   is sent; a further alert is sent once it becomes reachable again.
7. Every 7 days, a heartbeat message confirms the monitor is still running
   even when nothing changed.

## Setup

1. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. Create a Telegram bot (via [@BotFather](https://t.me/BotFather)) and get
   your chat id, then set two environment variables:

   ```bash
   export BOT_TOKEN="123456:your-bot-token"
   export CHAT_ID="your-chat-id"
   ```

   If unset, the script prints notifications to stdout instead of sending
   them (useful for local testing).

3. Run once manually to seed the initial state:

   ```bash
   python monitor.py
   ```

## Running on GitHub Actions

The included workflow ([`.github/workflows/monitor.yml`](.github/workflows/monitor.yml))
runs the monitor twice a day (08:00 and 20:00 UTC, i.e. roughly 10:00 and
22:00 Italian time) and commits the updated `state.json`/`heartbeat.json`
back to the repository so the next run picks up where the last one left
off.

To enable it:

1. Push this repository to GitHub.
2. In **Settings > Secrets and variables > Actions**, add two repository
   secrets: `BOT_TOKEN` and `CHAT_ID`.
3. Make sure Actions are enabled for the repository, and that the default
   `GITHUB_TOKEN` has write permission (**Settings > Actions > General >
   Workflow permissions > Read and write permissions**), so the workflow
   can push the state files back.
4. Optionally trigger the workflow manually once from the **Actions** tab
   ("Run workflow") to confirm everything is wired up correctly.

## Editing sources

Add, remove or disable sources by editing `sources.json` - no code changes
needed for another "listing" page. Each entry supports:

| Field                | Meaning                                                              |
|----------------------|------------------------------------------------------------------------|
| `id`                 | Stable identifier, used as the key in `state.json`. Don't change it once the monitor is live, or it will be treated as a brand-new source. |
| `label`              | Human-readable name shown in Telegram messages.                      |
| `type`               | `listing`, `keyword_scan` or `asmelab_interpelli`.                    |
| `url`                | Page to fetch (`listing` only).                                      |
| `hub_urls`/`keywords`| Pages to scan and keyword list (`keyword_scan` only).                |
| `check_degree_class` | If `true`, newly found items are opened and scanned for `L-8`/`LM-32` (`listing` only). |
| `region_filter`      | Region name passed to AsmeLab's own REGIONE filter, e.g. `sicilia` (`asmelab_interpelli` only). |
| `highlight_profile`  | Optional substring matched (case-insensitive) against PROFILO; matching interpelli are marked "⭐ TUO PROFILO -" and sorted first (`asmelab_interpelli` only). |
| `enabled`            | Set to `false` to keep a source configured but paused.               |

Adding another region to the AsmeLab source (or a second region-specific
source alongside it) only needs a new entry with `"type":
"asmelab_interpelli"` and the desired `region_filter` - no code changes.
Provinces are only resolved for Sicilian comuni today
(`sicily_municipality_provinces.json`); for any other region the Telegram
message simply falls back to showing the comune without a province.

## Known limitations

- The extraction logic (`extract_listing_items` / `extract_keyword_links`
  in `monitor.py`) was built and unit-tested against synthetic HTML that
  mirrors the described structure of each site, plus live testing against
  `uke.it`. `politichegiovanili.gov.it` and `scelgoilserviziocivile.gov.it`
  could not be fetched from the environment this project was built in
  (their `robots.txt` was unreachable from there), so the extractor's
  behaviour against their *actual* markup has not been verified first-hand.
  It's intentionally generic (heading+link and keyword-based, not tied to
  specific CSS classes) precisely to be resilient to that uncertainty, but
  the first real run is the real test: run it once locally with `BOT_TOKEN`
  unset and skim the printed "sources initialized" summary plus
  `state.json` to make sure the extracted item titles look sane for those
  two domains before relying on it unattended.
- If a source that has already run at least once gets removed from
  `sources.json` (as happened with the dated call pages above), its old
  entry simply stays in `state.json` unused - harmless, but feel free to
  delete that key by hand if you want a tidy file.

## License

This project is licensed under the [PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0). You may use, modify, and distribute this software for noncommercial purposes.

For commercial use or an extended license, please contact me: [lufaletra@gmail.com](mailto:lufaletra@gmail.com)
