# GeM Keyword Tender Pipeline (MVP scaffold)

Goal: verify whether we can automatically pull GeM tenders relevant to
our ~290 keywords (welding electrodes/consumables), filtered against
our 19 negative keywords -- the same core loop Palladium sells us for
₹2L/year.

## What's confirmed working right now

- `keywords_loader.py` has been tested against the real
  `Keywords__2_.xlsx` file: **290 positive keywords, 19 negative
  keywords loaded correctly.** This part is done and works.

## What's NOT yet verified

- `gem_client.py`'s HTML parsing selectors are a **first-draft guess**,
  not tested against a real response. The sandbox this was built in
  cannot reach `gem.gov.in` (network is restricted to a small dev-tool
  allowlist), so the scraping logic has not actually been run
  end-to-end yet.

This is the honest state: the plumbing (keyword loading, dedupe logic,
negative-keyword filtering, CSV output) is built and correct. The part
that touches the live GeM site needs to be run and debugged from a
machine with real internet access -- that's the next step, and it's a
fast one.

## How to get this fully working (next 30-60 minutes of work)

1. Install dependencies on a machine with normal internet access:
   ```
   pip install -r requirements.txt
   ```

2. Run the debug fetch for one keyword to see the real HTML:
   ```
   python gem_client.py "welding electrode"
   ```
   This saves `debug_response.html`. Open it in a browser or a text
   editor.

3. Find the actual bid listing markup in that HTML (look for repeating
   card/row elements containing bid number, title, ministry). Update
   the CSS selectors in `search_bids()` in `gem_client.py` to match
   what you actually see -- they're currently a guess (`.card`,
   `.bid_no_title a`).

4. Once one keyword search returns real results, run the full pipeline:
   ```
   python pipeline.py Keywords__2_.xlsx relevant_tenders.csv
   ```
   This will search all 290 keywords (with a 2.5s delay between each,
   so expect ~12-13 minutes total), dedupe by bid number, drop anything
   matching a negative keyword, and save the result to CSV.

5. Compare `relevant_tenders.csv` against what you're currently seeing
   in Palladium for the same period -- that comparison is your actual
   validation.

## Things worth knowing before you run this for real

- **robots.txt**: `bidplus.gem.gov.in` disallows automated access.
  This is a grey area every tender aggregator in this space operates
  in (see our earlier conversation) -- not unique to this script, but
  worth being deliberate about, especially once this moves beyond
  internal testing.
- **No official API**: this endpoint isn't documented by GeM. It can
  change without notice, which would silently break the selectors.
  Treat scraper breakage as a "when," not "if," and plan for
  monitoring/alerting once this is more than a prototype.
- **Rate limiting**: `REQUEST_DELAY_SECONDS = 2.5` in `gem_client.py`
  is a deliberately conservative default. Don't lower it without
  thinking about it -- the goal here is proving feasibility, not
  stress-testing a government portal.
- **This covers GeM only.** Your Palladium subscription almost
  certainly also pulls from CPPP and state portals. This scaffold is
  scoped to GeM specifically so we can validate the core loop
  (keyword match + negative filter -> relevant tenders) before
  multiplying the effort across more sources.

## Files

- `keywords_loader.py` -- reads the Keywords Excel file (tested, works)
- `gem_client.py` -- GeM search + debug helper (needs selector fixes)
- `pipeline.py` -- orchestrates the full run, writes CSV
- `requirements.txt` -- pip dependencies
