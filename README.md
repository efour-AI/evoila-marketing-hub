# evoila deck generator

Builds an evoila walking deck (PPTX + PDF) for one row in the Airtable **Deck Requests** table
(evoila Content Portal base) and writes both files back to the row.

## The slide library (Word) is the master

All on-slide copy and speaker notes come from the Walking Deck slide library Word doc, kept with its
version history in the Airtable table **Slide Library Versions**. Every deck is built from the newest
row whose Status is **Live**.

To publish a new version: add a row, type the version (for example `v12`), attach the .docx, leave
Status empty. The automation "Slide library: check new version" fires this repo's `check-library` event.
The check builds the full library from the doc, writes a report (missing lines, placeholders still in
brackets, lines the generator does not use), attaches the library PowerPoint, sets the row to Live and
retires the previous Live row. If the doc cannot be read, the row is set to Problems found and decks keep
using the previous version. Kevin gets an email either way.

To roll back: set the bad version to Retired and an older version to Live.

Layout, icons and diagrams live in code (`build_library.py` in the engine zip on the Deck Assets table).
Each slide's table in the doc must keep its columns (Variant | On-slide copy | Design spec | Speaker notes)
and its line labels ("Headline:", "Card 1, Title:", "Audience line:" ...). Black notes become speaker
notes; red notes go under OWNER NOTES.

## Flow for a deck

1. Someone submits the request page (audience, framing, prospect, date, presenters, meeting notes).
2. Airtable automation "Deck request: plan and build" clears the old plan and research, sets
   Status = Building and fires `build-deck`. (No Airtable AI.)
3. `.github/workflows/build-deck.yml` runs `build.py`. When the row has no plan, Claude (`ai_steps.py`)
   picks the slides and writes talking points, and in parallel researches the two data slides with web
   search. Both are saved to the row. If a Claude step fails, the deck still builds (default slides, no
   data slides) and the reason goes in Market data check.
4. `build.py` then runs `run_request.py`: downloads the master template, the engine zip and the Live
   slide library doc, builds the deck, puts the talking points in the speaker notes, converts to PDF with
   LibreOffice and uploads both files to the row. Status = Ready (or Error with detail).
5. `review.py prepare` saves one picture per slide (Slide previews) and the on-slide text (Slide text),
   then ticks Review ready. Airtable automation "Deck ready: email requester" then emails the requester a
   link to review the deck on the hub (docs/deckreview) and sets Status = In review. Requesters without a
   hub link get the files attached, as before.
6. On the review page each slide gets Looks good or Needs work. A note is rewritten into new slide copy
   by Claude (Make scenario "evoila Deck Review API"), shown before and after, then kept or undone.
   Changes can also be typed by hand. "Wrong for everyone" sends the change to Kevin's Library Fixes table.
   Progress saves as they go (Review state). Submit sets Status = Finalizing.
7. Airtable automation "Deck review: build the final" fires `finalize-deck`; `review.py finalize` applies
   the kept changes to the PPTX text (layout, logo and colors untouched), keeps the old files in Earlier
   versions, writes Review summary and sets Status = Final. "Deck final: email requester" sends the final
   PowerPoint and PDF, sends Kevin his copy with the changes and library fixes, and sets Status = Sent.

Repo secrets needed: `ANTHROPIC_API_KEY` (Claude API key, with web search enabled in the Claude Console)
and `AIRTABLE_TOKEN` (Airtable personal access token with `data.records:read` and
`data.records:write` on the evoila Content Portal base).

The repo is public, so the build logs print no request data or slide copy.
To run by hand: Actions tab, "Build walking deck", Run workflow, paste the record ID and the mode
(`build-deck` or `check-library`).

## One-pager (docs/onepager, onepager/)

The evoila US one-pager is rebuilt from the May 2026 InDesign file as `onepager/template.html` (layout,
colors, badges, logo, fonts) plus the words, which live in the Airtable table **One-pager Versions** (Copy
field, JSON in the `onepager/copy.json` format). The Current row is what the hub shows and sends.

1. On the hub page (docs/onepager) anyone with a hub link describes a change, or picks an older version and
   makes it current again. Make scenario "evoila Hub One-pager" (6920302) adds a row with Status = Building.
2. Airtable automation "One-pager: build new version" fires `onepager-build`; `.github/workflows/onepager.yml`
   runs `onepager/onepager.py`: Claude applies the change to the current words, then every version must pass
   `onepager/rules.json` (no VCSP, no em dashes, evoila lowercase) and the fit check (each box measured in
   Chrome; text that no longer fits fails). Claude gets two tries to fix a problem. It passes: PDF and preview
   are attached, the row becomes vN and Current, the old one Previous. It fails: Problems found, the reason is
   on the row, the requester gets an email ("One-pager: version not published") and the current version stays.
3. Sending: the sender picks one or two people for the contact box at the bottom (from **One-pager
   Contacts**, editable on the page). The hub adds a **One-pager Sends** row (Status = Preparing);
   "One-pager: prepare send" fires `onepager-send`, which renders the Current words with those contacts,
   checks they fit, attaches the PDF and sets Queued. "One-pager: send" then emails it with the sender in CC
   and as reply-to, and sets Status = Sent. If the contacts don't fit: Problems found, and "One-pager: send
   failed, tell the sender" emails the reason.
4. Download it: same form, "Download it" mode (Make route opcopy). The Sends row is Kind = Download; the job
   renders the copy (contacts plus any "changes just for this copy", applied by Claude with the same checks,
   never saved as a version) and sets Status = Ready; the page shows the download link. Contact boxes shrink
   their text to fit (down to 6pt) before the fit check fails, and 10-digit phones are formatted.

To ban another word on the one-pager, add it to `onepager/rules.json`. Layout changes are made in
`template.html` by hand (check with `python onepager/onepager.py --local onepager/copy.json out/`).

## Library item viewer (docs/index.html, library_previews.py)

Clicking a Resource Library item (or its Preview button) on the hub opens a viewer: Details on the right, the
file flipped through slide by slide or page by page (arrows, arrow keys, swipe, thumbnails, a clickable
contents list), a line under each slide saying what it covers, and the download buttons at the bottom.
Recent decks built on the hub open the same viewer from their Slide previews.

- Data lives on the Resource Library row: Details, Slide guide (JSON: n, title, about), Slide previews,
  Previews built from. Edit Details and Slide guide by hand in Airtable whenever you like.
- `library_previews.py` (hub-jobs, mode `library-previews`, also run after every `index-library`) draws the
  previews for any row whose file changed, and drafts Slide guide and Details with Claude only when they are
  empty or the page count changed.
- The viewer loads through Make scenario "evoila Hub Library Viewer" (6998407): `resget` returns one library
  row (only if Show on hub), `deckpeek` returns a deck's previews and slide titles. Same hub links as the rest.

## Stock picks (hub section "Stock picks", stock_picks.py)

Weekly contest: at the end of the marketing call each person names one stock. Airtable table **Stock Picks**
(tblBbD5cXBJtKoyC5), one row per person per week.

1. `.github/workflows/stock-picks.yml` runs Wednesday afternoons/evenings and once a day. `stock_picks.py` reads the newest
   "Marketing and Sales Weekly Sync" in Call Transcripts that has no picks yet, Claude pulls out who picked what, each
   ticker is checked on Yahoo Finance, and a row is added per person (Status = Waiting for start price, or Check this if
   the ticker isn't found).
2. Start price = closing price (3:00 PM Central) on the call day (Status = Live). End price = price at 1:00 PM Central seven
   days later (Status = Final).
3. The hub reads the rows and live prices through Make scenario "evoila Hub Stocks" (7000206): `a=picks` lists the
   table, `a=quotes&s=AMD,VRT` returns current prices. The page ranks this week live, and the leaderboard rolls a
   $100 stake through every week's pick (compounded).

To fix a misheard pick: edit Ticker on the row (clear Start price too if it was already locked), then run the workflow
by hand from the Actions tab. To add a pick by hand: add a row with Week of, Person and Ticker.

Trash talk: Airtable table **Stock Talk** (tblamxVjGmwuqwDnW). The hub posts through the same Make scenario
(`a=talkpost`); From comes from the sender's personal link, so nobody can post as someone else. Delete a row to take a
comment down. The $100 is a one-time buy-in that rides every week's pick (compounded).
