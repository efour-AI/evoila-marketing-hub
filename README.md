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
