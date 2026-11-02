"""Marketing hub background jobs, run by .github/workflows/hub-jobs.yml.

MODE=index-library  Reads every Resource Library row that is shown on the hub, pulls the text out of
                    its file (Word, PDF, PowerPoint) and rewrites the Hub Knowledge table, which the
                    hub's question bar answers from. Rows in Hub Knowledge whose Source file does not
                    start with "lib:" (for example the voice guide) are left alone.
MODE=build-post     A self-serve LinkedIn post (topic + bullets from the hub). Claude writes the copy
                    and hashtags in the author's voice using the voice guide row in Hub Knowledge,
                    the image is drawn with postgen/evoila_post_gen.py (or the author's own photo is
                    cropped to 4:5), and the row goes to the author for approval (Marketing Approved,
                    Self-serve). Approving it on the review page posts it immediately.

MODE=meeting-overviews  Backfill: writes a short Meeting Overview on Call Transcripts rows from the last
                    60 days that have a transcript and no overview yet. New calls get theirs from the
                    Fireflies scenario in Make.

MODE=revise-copy    An author sent a post or article back with notes. Claude revises the text to reflect them
                   (keeps the old text in Copy Before Revision) and says what it changed in Revision Note.
MODE=voice-cards    Weekly (and on demand). Rebuilds the "Voice: <author>" rows in Hub Knowledge that the
                    hub's My voice page shows, from the newest "msg:" row (the voices, language and
                    governance parts of the Brand Messaging Platform, pushed by marketing whenever the
                    document changes) plus verbatim voice phrases from the last 30 days of call
                    transcripts. Older "msg:" rows are removed.

MODE=meeting-overviews also rebuilds the "TEAM CONVERSATIONS" rows in Hub Knowledge (source "calls:"): meeting
                    overviews plus Glen's and Mark's phrases, topics and stories and customer quotes from
                    the last 90 days, so the question bar can answer with what the team actually says.

MODE=meeting-overviews and index-library also refresh the "PUBLIC WEB" rows (source "web:"): the text of evoila's
                    public US page and its Broadcom partner listing, so answers match what is already public.

Public repository: never print document text, post copy or request data to the log.
"""
import base64, json, os, re, sys, textwrap, time, urllib.error, urllib.parse, urllib.request

import ai_steps

BASE = "appYVahdfC3gbtArV"
TOKEN = os.environ["AIRTABLE_TOKEN"]
MODE = os.environ.get("MODE", "")
REC = (os.environ.get("RECORD_ID") or "").strip()
WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")

T_RES, T_KB, T_POST = "tblYQvkJUWlEhtg6s", "tblgN6cSCyfuzy6Cg", "tblchftgrc7MkquoD"
R = {"name": "fld2D09t4tkerjQfw", "share": "fldSdB4ez9MhWdK9G", "file": "fld4tiUzYaV2LsOdX", "show": "fldk6x6akKDbSw3nf"}
K = {"name": "fldZUzOp8hcDOvKDq", "share": "fldwuiyH0Yr2BrmtR", "text": "fldP8vXueSmJfLVWl", "use": "fldFvBplItZr27L9p",
     "src": "fldNkir4wHX1fNdhd"}
P = {"title": "fld0JVy1STI2nhmP1", "copy": "fldNpsfupzhYrXorE", "author": "fldaKLAg5mK69CnLk", "status": "fldvo8DzHp1DUO23I",
     "date": "fldCwmyaVEgKJWQtD", "image": "fldaWaM6MEP836pLx", "bullets": "fldBmUUPNlni8BdL0", "upload": "fldwMMXx9NPzKLysB",
     "gen": "flde7F6SG3x75KRTR", "self": "fldKlGOWLtGe9x0ZG", "note": "fldkZDDMTtB0prwzJ"}
TITLES = {"Glen Tindal": "CEO and Managing Director, evoila US", "Mark Maisano": "VP Alliances & Strategy, evoila US",
          "Andrew Hald": "Lead Solution Architect, VCDX #4",
          "Tom MacKay": "VP of Technical Sales and Services, evoila US"}
T_CALL = "tbllSce3nYaZiZfRS"
C = {"title": "fldvTkxCvJBfBxuGp", "date": "fldKxPm29yO8wvNxA", "parts": "fldwpSTIKKRJw6CwT", "text": "fldZuwTbamTYvdQAa",
     "overview": "fldb82uxuawj7h5Ei"}
OVERVIEW_PROMPT = """Below is the transcript of an evoila US meeting. Everyone at evoila can read your overview on the internal
marketing hub, including people who were not on the call, so write it for a colleague who missed the meeting.

- Start with two to four plain sentences: what the meeting was for and what was discussed.
- Then a line "Decided:" followed by up to four lines starting with "- " for decisions actually made (omit if none).
- Then a line "Next:" followed by up to four lines starting with "- " for agreed next steps with the owner's first name (omit if none).
- Only what the transcript supports. No quotes longer than ten words, no pricing figures, no personal or HR matters,
  no opinions about individuals.
- No markdown, no em dashes, write evoila in lowercase. Under 160 words.
- If the transcript is mostly silence or not a business conversation, reply with exactly: NONE
Reply with the overview text only.

MEETING TITLE: {title}
DATE: {date}
PARTICIPANTS: {parts}

TRANSCRIPT:
{text}"""
VOICE_PROMPT = """You write the "My voice" card that {author} ({title}) reads on evoila's internal marketing hub. It tells
him, in second person ("you"), exactly how his LinkedIn posts are written: the persona the drafting system uses.
Base it on the Brand Messaging Platform excerpt below (his section in Part Four first, plus the language
standards, AI position and governance rules that apply to his posts). Be detailed and specific: carry over his
named phrases, framings, positions, habits, hashtags, boundaries and gate questions rather than summarizing them
away. Where the document quotes him, keep the quote short (under 15 words) and in quotation marks.

{recent}

Rules:
- Leave out: health or personal details, internal pipeline figures, prices, customer, partner and competitor
  names, anything marked intelligence or [VERIFY], open-decision IDs (D-1 etc.), note numbers, other people's
  personas, and anything about how marketing operates internally.
- Plain text. Headings are lines that start with "## ". Bullets start with "- ". No bold, no tables, no em dashes.
  Write evoila in lowercase.
- Use these headings, in this order, skipping any that do not apply to him:
## Your role
## Who reads you
## What you talk about
## Positions you hold
## How you sound
## Heard recently
## Your post shape
## How you open and close
## Words and moves to avoid
## What stays out
## Your gate
## Hashtags
- "Heard recently" lists up to six of the recent phrases below that best show how he talks, each as
  - "phrase" (topic, Mon DD). Skip the heading if there are none.
- 900 to 1,400 words. Reply with the card text only.

BRAND MESSAGING PLATFORM EXCERPT:
{doc}"""
CHUNK = 90000          # Airtable long text holds 100,000 characters


def api(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"Airtable {e.code}: {e.read()[:300].decode('utf-8', 'ignore')}")


def rows(table, formula=None):
    out, offset = [], None
    while True:
        q = "?returnFieldsByFieldId=true&pageSize=100"
        if formula:
            q += "&filterByFormula=" + urllib.parse.quote(formula)
        if offset:
            q += "&offset=" + offset
        r = api("GET", f"https://api.airtable.com/v0/{BASE}/{table}{q}")
        out += r.get("records", [])
        offset = r.get("offset")
        if not offset:
            return out


def download(url, path):
    with urllib.request.urlopen(url, timeout=300) as r, open(path, "wb") as fh:
        fh.write(r.read())


# ------------------------------------------------------------------ index-library

def text_of(path):
    low = path.lower()
    if low.endswith(".docx"):
        import docx
        d = docx.Document(path)
        parts = []
        body = d.element.body
        for child in body.iterchildren():                       # paragraphs and tables in document order
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                t = "".join(n.text or "" for n in child.iter() if n.tag.endswith("}t")).strip()
                if t:
                    parts.append(t)
            elif tag == "tbl":
                for tr in child.iter():
                    if tr.tag.endswith("}tr"):
                        cells = []
                        for tc in tr:
                            if tc.tag.endswith("}tc"):
                                cells.append(" ".join("".join(n.text or "" for n in p.iter() if n.tag.endswith("}t"))
                                                      for p in tc.iter() if p.tag.endswith("}p")).strip())
                        if any(cells):
                            parts.append(" | ".join(cells))
        return "\n".join(parts)
    if low.endswith(".pdf"):
        from pypdf import PdfReader
        return "\n".join((pg.extract_text() or "") for pg in PdfReader(path).pages)
    if low.endswith(".pptx"):
        from pptx import Presentation
        out = []
        for i, s in enumerate(Presentation(path).slides, 1):
            tx = [sh.text_frame.text for sh in s.shapes if sh.has_text_frame and sh.text_frame.text.strip()]
            out.append(f"[Slide {i}] " + " | ".join(tx))
        return "\n".join(out)
    if low.endswith((".txt", ".md", ".csv")):
        return open(path, encoding="utf-8", errors="ignore").read()
    return ""


def index_library():
    os.makedirs(WORK, exist_ok=True)
    res = rows(T_RES, "{Show on hub}")
    new = []
    for r in res:
        f = r["fields"]
        name, share = f.get(R["name"], "Untitled"), f.get(R["share"], "") or "Internal only"
        for a in f.get(R["file"], []) or []:
            fn = a.get("filename", "")
            # PowerPoint copies of a Word source and the master template add nothing for answering questions
            if fn.lower().endswith(".pptx") and ("template" in fn.lower() or "library" in name.lower()):
                continue
            path = os.path.join(WORK, re.sub(r"[^A-Za-z0-9._-]", "_", fn) or "file")
            try:
                download(a["url"], path)
                t = text_of(path)
            except Exception as e:
                print(f"skipped one file: {type(e).__name__}")
                continue
            t = re.sub(r"[ \t]+", " ", t)
            t = re.sub(r"\n\s*\n+", "\n\n", t).strip()
            # source lists and endnotes (verbatim call quotes) add cost to every question, not answers
            m = re.search(r"\n(SOURCES|NOTES|Sources|Notes)\n", t)
            if m and m.start() > len(t) * 0.4:
                t = t[:m.start()].rstrip()
            if len(t) < 200:
                continue
            pieces = [t[i:i + CHUNK] for i in range(0, len(t), CHUNK)]
            for n, piece in enumerate(pieces, 1):
                label = name if len(pieces) == 1 else f"{name} (part {n} of {len(pieces)})"
                new.append({K["name"]: label, K["share"]: share, K["text"]: piece, K["use"]: True, K["src"]: f"lib:{r['id']}:{fn}"})
    old = [r["id"] for r in rows(T_KB) if str(r["fields"].get(K["src"], "")).startswith("lib:")]
    for i in range(0, len(old), 10):
        ids = "&".join("records[]=" + x for x in old[i:i + 10])
        api("DELETE", f"https://api.airtable.com/v0/{BASE}/{T_KB}?{ids}")
    for i in range(0, len(new), 10):
        api("POST", f"https://api.airtable.com/v0/{BASE}/{T_KB}", {"records": [{"fields": x} for x in new[i:i + 10]]})
    print(f"indexed {len(new)} pieces from {len(res)} resources")
    web_pages()
    if any("messaging platform" in x[K["name"]].lower() for x in new):
        voice_cards()


# ------------------------------------------------------------------ build-post

POST_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Hook line used as the working title, under 80 characters."},
        "copy": {"type": "string", "description": "The full post body, plain text with line breaks, without the hashtags."},
        "hashtags": {"type": "array", "items": {"type": "string"}, "description": "3 to 5 hashtags from the author's list."},
        "image": {"type": "object", "properties": {
            "headline": {"type": "string", "description": "6 to 12 words for the image, the post's idea in short. No hashtags."},
            "accent_words": {"type": "string", "description": "The last few words of the headline to colour as the accent."},
            "kicker": {"type": "string", "description": "Short label above the headline: a series hashtag such as #ExplainIT, or 1 to 3 words."},
            "subhead": {"type": "string", "description": "Optional, under 40 characters."},
            "dark": {"type": "boolean"},
            "variant": {"type": "string", "enum": ["ribbon_r", "arc_r", "corner_br", "block_r"]},
            "accent": {"type": "string", "enum": ["blue", "green", "indigo"]}},
            "required": ["headline", "kicker", "dark", "variant", "accent"]},
        "check": {"type": "string", "description": "One line: any governance rule you had to work around, or 'clean'."},
    },
    "required": ["title", "copy", "hashtags", "image", "check"],
}

POST_PROMPT = """You are writing a LinkedIn post that {author} ({title}) will publish on his own profile. He started it himself on the evoila marketing hub with a topic and a few bullets. Write it in his voice, following the voice guide exactly, then call submit_post.

VOICE GUIDE
{guide}

HIS INPUT
Topic: {topic}
Bullets / notes:
{bullets}
{photo}
RULES
- Use only what his bullets say plus general, well-known industry context. Never invent customers, results, numbers, dates, quotes or events. If a bullet has a number, keep it exactly.
- Every governance rule in the guide applies. If a bullet asks for something the guide forbids (for example naming a customer or the partner program), leave it out and say so in "check".
- No em dashes. "evoila" lowercase. No links. Plain text.
- The image headline is short, specific and readable at a glance; it does not repeat the hook word for word.
- Vary the image: pick dark or light, a variant and an accent that suit the topic.
"""


def clean_copy(t):
    t = str(t or "").replace("—", ", ").replace("–", "-")
    t = re.sub(r"[ \t]+,", ",", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"\bEvoila\b", "evoila", t)
    return t.strip()


def draft(author, topic, bullets, has_photo, guide):
    prompt = POST_PROMPT.format(author=author, title=TITLES.get(author, ""), guide=guide, topic=topic or "(none given)",
                                bullets=bullets or "(none given)",
                                photo="He attached his own photo, which will be the image. Still fill in the image fields.\n" if has_photo else "")
    tools = [{"name": "submit_post", "description": "Submit the finished post.", "input_schema": POST_SCHEMA}]
    messages = [{"role": "user", "content": prompt}]
    for _ in range(3):
        resp = ai_steps._call({"model": ai_steps.MODEL, "max_tokens": 4000, "tools": tools, "messages": messages}, timeout=180)
        content = resp.get("content", [])
        spec = next((b.get("input") for b in content if b.get("type") == "tool_use" and b.get("name") == "submit_post"), None)
        if isinstance(spec, dict) and spec.get("copy") and spec.get("image"):
            return spec
        messages.append({"role": "assistant", "content": content})
        if resp.get("stop_reason") == "tool_use":
            messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": b["id"],
                                                          "content": "Missing fields. Call submit_post again with everything."}
                                                         for b in content if b.get("type") == "tool_use"]})
        else:
            messages.append({"role": "user", "content": "Call submit_post now."})
    raise RuntimeError("Claude returned no post")


def render(spec, author, out_png):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "postgen"))
    import evoila_post_gen as G
    im = spec.get("image") or {}
    head = clean_copy(im.get("headline") or spec.get("title") or "").replace("\n", " ")
    variant = im.get("variant") if im.get("variant") in G.VARIANTS else "ribbon_r"
    lines = textwrap.wrap(head, width=12 if variant == "block_r" else 17)
    if variant == "block_r" and len(lines) > 5:          # the colour block takes the right third
        variant, lines = "ribbon_r", textwrap.wrap(head, width=17)
    lines = lines[:5] or ["evoila"]
    acc_words = clean_copy(im.get("accent_words") or "").lower()
    accent_lines = {ln for ln in lines if acc_words and ln.lower() in acc_words} or {lines[-1]}
    col = {"blue": G.BLUE, "green": G.GREEN, "indigo": G.INDIGO}.get(im.get("accent"), G.BLUE)
    dark = bool(im.get("dark"))
    if dark and col == G.INDIGO:
        col = G.GREEN                                   # indigo disappears on violet
    sub = clean_copy(im.get("subhead") or "")[:44] or None
    if len(lines) >= 5:
        sub = None
    G.build(out_png, "4:5", dark=dark, variant=variant, kick=(clean_copy(im.get("kicker")) or "#InsightIT")[:28],
            head=lines, head_accent=accent_lines, accent=col, subhead=sub,
            footer=[author, TITLES.get(author, "evoila US")])


def to_jpeg(src_png_or_photo, out_jpg, crop=False):
    from PIL import Image, ImageOps
    im = Image.open(src_png_or_photo)
    im = ImageOps.exif_transpose(im).convert("RGB")
    if crop:
        im = ImageOps.fit(im, (1080, 1350), Image.LANCZOS, centering=(0.5, 0.45))
    im.save(out_jpg, "JPEG", quality=90, optimize=True, subsampling=1)   # no EXIF, no C2PA chunks


def build_post():
    os.makedirs(WORK, exist_ok=True)
    url = f"https://api.airtable.com/v0/{BASE}/{T_POST}/{REC}"
    f = api("GET", url + "?returnFieldsByFieldId=true")["fields"]
    author = f.get(P["author"]) or ""
    author = author.get("name") if isinstance(author, dict) else author
    if author not in TITLES:
        raise RuntimeError("Self-serve posts are for Glen, Mark, Andrew or Tom")
    api("PATCH", url, {"fields": {P["note"]: "Writing your post…"}})
    guide_rows = [r for r in rows(T_KB) if str(r["fields"].get(K["name"], "")).upper().startswith("VOICE GUIDE")]
    if not guide_rows:
        raise RuntimeError("The voice guide row is missing from Hub Knowledge")
    guide = guide_rows[0]["fields"].get(K["text"], "")
    photo = (f.get(P["upload"]) or [None])[0]
    spec = draft(author, f.get(P["title"], ""), f.get(P["bullets"], ""), bool(photo), guide)
    api("PATCH", url, {"fields": {P["note"]: "Drawing the image…"}})
    jpg = os.path.join(WORK, f"post_{REC}.jpg")
    if photo:
        raw = os.path.join(WORK, "photo_in")
        download(photo["url"], raw)
        to_jpeg(raw, jpg, crop=True)
    else:
        png = os.path.join(WORK, "post.png")
        render(spec, author, png)
        to_jpeg(png, jpg)
    tags = " ".join(t if t.startswith("#") else "#" + t for t in (spec.get("hashtags") or [])[:5])
    body = clean_copy(spec["copy"]) + ("\n\n" + tags if tags else "")
    with open(jpg, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    api("PATCH", url, {"fields": {P["image"]: []}})
    api("POST", f"https://content.airtable.com/v0/{BASE}/{REC}/{P['image']}/uploadAttachment",
        {"contentType": "image/jpeg", "file": b64, "filename": f"{re.sub('[^A-Za-z]', '', author)}_selfserve.jpg"})
    check = clean_copy(spec.get("check") or "")
    fields = {P["copy"]: body, P["title"]: clean_copy(spec.get("title"))[:120] or f.get(P["title"], ""),
              P["status"]: "Draft" if "[TEST]" in (f.get(P["bullets"]) or "") else "Marketing Approved", P["gen"]: False,
              P["note"]: ("Ready. " + ("" if not check or check.lower().startswith("clean") else "Note: " + check))[:250]}
    if not f.get(P["date"]):
        fields[P["date"]] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    api("PATCH", url, {"fields": fields, "typecast": True})
    print("post drafted")


# ------------------------------------------------------------------ revise-copy

T_ART = "tblcTJ5gIwdJvJmGH"
REV = {  # field ids per table for the author-notes revision
    "post": {"table": T_POST, "copy": "fldNpsfupzhYrXorE", "notes": "fldq8CJhq5B8tkycF", "author": "fldaKLAg5mK69CnLk",
             "status": "fldvo8DzHp1DUO23I", "before": "fld47MEzZqI5uyvbg", "rnote": "fldb5lfEUqzoA6AU0", "title": "fld0JVy1STI2nhmP1"},
    "article": {"table": T_ART, "copy": "fldeoYpiF5nAyYhhV", "notes": "fldzT7k7GHL9FHQtb", "author": "fldtBsPvJFEnnaKjX",
                "status": "fldXZJewKg8qBXNql", "before": "fldjIdq83fowU9VIP", "rnote": "fldIC5ZuiRpbIpQ1Q", "title": "fldtLy3z7SDmS8LHf"},
}

REVISE_SCHEMA = {
    "type": "object",
    "properties": {
        "copy": {"type": "string", "description": "The full revised text. Same format as the current text (hashtags kept at the end of a post; Markdown headings kept in an article)."},
        "changes": {"type": "array", "items": {"type": "string"},
                    "description": "One short line per change you made, in plain words, so marketing can check it. Under 20 words each."},
        "not_applied": {"type": "array", "items": {"type": "string"},
                        "description": "Anything the author asked for that you did not apply, each with the reason (a content rule, a fact you cannot verify, unclear). Empty if everything was applied."},
    },
    "required": ["copy", "changes", "not_applied"],
}

REVISE_PROMPT = """{author} ({title}) read the {kind} below on evoila's marketing hub and sent it back to marketing with notes: changes he wants, things that are off, or where he stands on the topic. Revise the text so it reflects his notes, then call submit_revision. Marketing (Kevin) reviews your revision before it goes back to {first} for final approval.

HOW TO REVISE
- His latest note is what matters most. Earlier notes in the thread are context; marketing may already have handled them.
- Apply every concrete change he asks for.
- Where he pushes back or states his position, rewrite the affected parts so the {kind} says what he actually thinks, in his voice. Do not argue with him and do not water his position down.
- Where he says something is off but not how to fix it, fix it the way his voice card and position suggest.
- His explicit instructions win over the style habits in his voice card. If he says to cut a line, a closing call to action included, cut it and do not replace it with another. Only the content rules (what evoila may not say) can override him.
- Keep his claims at the scope he gave them. "Most of ours" stays about evoila's customers; never widen it to all customers or the whole market.
- Change only what his notes call for. Keep everything else as it is, word for word.
- Never invent customers, results, numbers, dates, quotes or events. If he gives a fact or number, use it exactly as he wrote it.
- The content rules in the voice guide still apply. If he asks for something they forbid (for example naming a customer or the partner program), leave it out and list it in not_applied with the reason.
- No em dashes. "evoila" is lowercase. {fmt}

HIS VOICE CARD
{card}

GENERAL VOICE GUIDE AND CONTENT RULES
{guide}

NOTES THREAD (oldest first; his latest note is last)
{notes}

CURRENT TEXT
{copy}
"""


def revise_copy():
    kind = (os.environ.get("TABLE") or "post").strip().lower()
    if kind not in REV:
        raise RuntimeError("TABLE must be post or article")
    F = REV[kind]
    url = f"https://api.airtable.com/v0/{BASE}/{F['table']}/{REC}"
    f = api("GET", url + "?returnFieldsByFieldId=true")["fields"]
    author = f.get(F["author"]) or ""
    author = author.get("name") if isinstance(author, dict) else author
    copy, notes = f.get(F["copy"]) or "", (f.get(F["notes"]) or "").strip()
    if not copy.strip() or not notes:
        api("PATCH", url, {"fields": {F["rnote"]: "Nothing to revise: no text or no notes."}})
        print("nothing to revise")
        return
    first = author.split(" ")[0] if author else "the author"
    api("PATCH", url, {"fields": {F["rnote"]: f"Revising the text from {first}'s notes…"}})
    kb = rows(T_KB)
    card = next((r["fields"].get(K["text"], "") for r in kb if r["fields"].get(K["name"], "") == f"Voice: {author}"), "")
    guide = next((r["fields"].get(K["text"], "") for r in kb
                  if str(r["fields"].get(K["name"], "")).upper().startswith("VOICE GUIDE")), "")
    prompt = REVISE_PROMPT.format(
        author=author or "The author", title=TITLES.get(author, ""), first=first,
        kind="LinkedIn post" if kind == "post" else "blog article",
        fmt="Plain text with line breaks; keep the hashtag line at the end unless he asks to change it." if kind == "post"
        else "Keep the Markdown structure (headings, lists).",
        card=card or "(no voice card on file)", guide=guide or "(none)", notes=notes[-12000:], copy=copy)
    tools = [{"name": "submit_revision", "description": "Submit the revised text.", "input_schema": REVISE_SCHEMA}]
    messages = [{"role": "user", "content": prompt}]
    spec = None
    for _ in range(3):
        resp = ai_steps._call({"model": ai_steps.MODEL, "max_tokens": 16000, "tools": tools, "messages": messages}, timeout=300)
        content = resp.get("content", [])
        spec = next((b.get("input") for b in content if b.get("type") == "tool_use" and b.get("name") == "submit_revision"), None)
        if isinstance(spec, dict) and str(spec.get("copy") or "").strip():
            break
        print("retry, stop_reason", resp.get("stop_reason"))
        spec = None
        messages.append({"role": "assistant", "content": content})
        if resp.get("stop_reason") == "tool_use":
            messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": b["id"],
                                                          "content": "Missing fields. Call submit_revision again with everything."}
                                                         for b in content if b.get("type") == "tool_use"]})
        else:
            messages.append({"role": "user", "content": "Call submit_revision now."})
    if not spec:
        raise RuntimeError("Claude returned no revision")
    new = clean_copy(spec["copy"])
    changes = [clean_copy(x) for x in (spec.get("changes") or []) if str(x).strip()]
    skipped = [clean_copy(x) for x in (spec.get("not_applied") or []) if str(x).strip()]
    lines = [f"Revised from {first}'s notes on {time.strftime('%b %-d')}."]
    lines += ["Changed:"] + ["- " + c for c in changes] if changes else ["No text changes were needed."]
    if skipped:
        lines += ["Not applied:"] + ["- " + s for s in skipped]
    summary = "\n".join(lines)
    today = time.strftime("%Y-%m-%d")
    log = notes + f"\n\n[{today}] Revision: " + ("; ".join(c.rstrip(". ") for c in changes) if changes else "no text changes needed") + \
        (". Not applied: " + "; ".join(s.rstrip(". ") for s in skipped) if skipped else "")
    api("PATCH", url, {"fields": {F["before"]: copy, F["copy"]: new, F["rnote"]: summary, F["notes"]: log}})
    print("revised:", len(changes), "changes,", len(skipped), "not applied")


CALL_FIELDS = [("Glen, in his words", "fldtjdShVGV6RWhs3"), ("Glen's focus", "fldJkAFlNFQSjZ0Tw"),
               ("Glen's stories", "fldSbZoo0MWv9pvCQ"), ("Mark, in his words", "fldZpZVI2zKOH0RgM"),
               ("Mark's focus", "fld4Dl5BWQoS3xJC1"), ("Mark's stories", "fldJICiTOzwN9M6RP"),
               ("Customer and partner quotes", "fldhN3U4ZYUZIieCO"), ("Market signals", "fldQ6TsoMnS9x2S4D")]


def team_conversations():
    calls = rows(T_CALL, "AND({Full Transcript}!='', IS_AFTER({Meeting Date}, DATEADD(TODAY(), -90, 'days')))")
    calls.sort(key=lambda r: r["fields"].get(C["date"], ""), reverse=True)
    parts = []
    for c in calls:
        f = c["fields"]
        block = [f"### {f.get(C['title'], 'Meeting')} ({f.get(C['date'], '')})"]
        if f.get(C["overview"]):
            block.append("Overview:\n" + f[C["overview"]].strip())
        for label, fid in CALL_FIELDS:
            v = str(f.get(fid, "") or "").strip()
            if v:
                block.append(label + ":\n" + v)
        if len(block) > 1:
            parts.append("\n".join(block))
    text = ("What the evoila US team has said in recorded meetings over the last 90 days, newest first. These are the "
            "team's own talking points and stories, not approved copy. Lines marked [NOT FOR PUBLICATION] are internal only.\n\n"
            + "\n\n".join(parts))
    pieces = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] if parts else []
    old = [r["id"] for r in rows(T_KB, "LEFT({Source file}, 6)='calls:'")]
    for i in range(0, len(old), 10):
        api("DELETE", f"https://api.airtable.com/v0/{BASE}/{T_KB}?" + "&".join("records[]=" + x for x in old[i:i + 10]))
    recs = [{"fields": {K["name"]: "TEAM CONVERSATIONS: what the team has said in meetings" + ("" if len(pieces) == 1 else f" (part {n} of {len(pieces)})"),
                        K["share"]: "Internal only", K["text"]: p, K["use"]: True, K["src"]: "calls: " + time.strftime("%Y-%m-%d")}}
            for n, p in enumerate(pieces, 1)]
    for i in range(0, len(recs), 10):
        api("POST", f"https://api.airtable.com/v0/{BASE}/{T_KB}", {"records": recs[i:i + 10]})
    print(f"team conversations: {len(parts)} meetings, {len(text)} characters, {len(pieces)} rows")


WEB_PAGES = [("PUBLIC WEB: evoila.com/us (evoila US website)", "https://evoila.com/us/"),
             ("PUBLIC WEB: Broadcom Expert Advantage partner listing for evoila",
              "https://expert.broadcom.com/partners/consulting-services/evoila")]


def web_pages():
    import html as _html
    rows_ = []
    for name, url in WEB_PAGES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (evoila marketing hub)"})
            with urllib.request.urlopen(req, timeout=60) as r:
                t = r.read().decode("utf-8", "ignore")
        except Exception as e:
            print("web page skipped:", type(e).__name__)
            continue
        t = re.sub(r"(?is)<(script|style|noscript|svg|nav|header|footer).*?</\1>", " ", t)
        t = re.sub(r"<[^>]+>", " ", t)
        t = _html.unescape(re.sub(r"\s+", " ", t)).strip()
        if len(t) < 300:
            continue
        rows_.append({"fields": {K["name"]: name, K["share"]: "Public", K["text"]: (url + "\n\n" + t)[:CHUNK], K["use"]: True,
                                 K["src"]: "web: " + url}})
    if not rows_:
        return
    old = [r["id"] for r in rows(T_KB, "LEFT({Source file}, 4)='web:'")]
    for i in range(0, len(old), 10):
        api("DELETE", f"https://api.airtable.com/v0/{BASE}/{T_KB}?" + "&".join("records[]=" + x for x in old[i:i + 10]))
    api("POST", f"https://api.airtable.com/v0/{BASE}/{T_KB}", {"records": rows_})
    print(f"public web pages: {len(rows_)}")


def meeting_overviews():
    recs = rows(T_CALL, "AND({Meeting Overview}='', {Full Transcript}!='', IS_AFTER({Meeting Date}, DATEADD(TODAY(), -60, 'days')))")
    print(f"{len(recs)} meetings to summarize")
    done = 0
    for r in recs:
        f = r["fields"]
        prompt = OVERVIEW_PROMPT.format(title=f.get(C["title"], ""), date=f.get(C["date"], ""),
                                        parts=str(f.get(C["parts"], ""))[:1500], text=f.get(C["text"], "")[:95000])
        try:
            resp = ai_steps._call({"model": ai_steps.MODEL, "max_tokens": 1500,
                                   "messages": [{"role": "user", "content": prompt}]}, timeout=180)
            out = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text").strip()
        except Exception as e:
            print("one meeting failed:", type(e).__name__)
            continue
        if not out or out == "NONE":
            continue
        out = out.replace("\u2014", ", ").replace("Evoila", "evoila")
        api("PATCH", f"https://api.airtable.com/v0/{BASE}/{T_CALL}/{r['id']}", {"fields": {C["overview"]: out[:5000]}})
        done += 1
    print(f"wrote {done} overviews")
    team_conversations()
    web_pages()


def voice_cards():
    # Prefer the Brand Messaging Platform from the resource library (kept current from Drive); fall back to a pushed msg: row
    lib = [r for r in rows(T_KB, "LEFT({Source file}, 4)='lib:'") if "messaging platform" in str(r["fields"].get(K["name"], "")).lower()]
    lib.sort(key=lambda r: str(r["fields"].get(K["name"], "")))
    doc, origin = "", ""
    if lib:
        full = "".join(r["fields"].get(K["text"], "") for r in lib)
        a, b = full.find("PART THREE"), full.find("DOCUMENT CONTROL")
        doc = full[a:b] if a >= 0 and b > a else full
        origin = str(lib[0]["fields"].get(K["name"], "library")).split(" (part")[0]
    msgs = rows(T_KB, "LEFT({Source file}, 4)='msg:'")
    msgs.sort(key=lambda r: r.get("createdTime", ""), reverse=True)
    if not doc and msgs:
        doc = msgs[0]["fields"].get(K["text"], "")
        origin = msgs[0]["fields"].get(K["src"], "msg:")[4:].strip()
    if not doc:
        raise RuntimeError("no messaging platform text found")
    calls = rows(T_CALL, "IS_AFTER({Meeting Date}, DATEADD(TODAY(), -30, 'days'))")
    phrase_field = {"Glen Tindal": "fldtjdShVGV6RWhs3", "Mark Maisano": "fldZpZVI2zKOH0RgM"}
    cards = {r["fields"].get(K["name"], ""): r["id"] for r in rows(T_KB, "LEFT({Name}, 6)='Voice:'")}
    for author in ["Glen Tindal", "Mark Maisano", "Andrew Hald", "Tom MacKay"]:
        recent = ""
        fid = phrase_field.get(author)
        if fid:
            bits = [f"[{c['fields'].get(C['date'], '')}] {c['fields'][fid]}" for c in calls if c["fields"].get(fid)]
            if bits:
                recent = "RECENT VOICE PHRASES FROM CALL TRANSCRIPTS (verbatim, last 30 days):\n" + "\n".join(bits)[:12000]
        prompt = VOICE_PROMPT.format(author=author, title=TITLES.get(author, ""), doc=doc[:95000],
                                     recent=recent or "There are no recent transcript phrases for him; skip Heard recently.")
        out = ""
        for attempt in range(3):
            resp = ai_steps._call({"model": ai_steps.MODEL, "max_tokens": 16000,
                                   "messages": [{"role": "user", "content": prompt}]}, timeout=300)
            out = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text").strip()
            print(f"attempt {attempt + 1}: stop={resp.get('stop_reason')} chars={len(out)}")
            if len(out) >= 400:
                break
        out = out.replace("\u2014", ", ").replace("Evoila", "evoila")
        if len(out) < 400:
            print("short card skipped for one author")
            continue
        fields = {K["name"]: "Voice: " + author, K["text"]: out[:90000], K["share"]: "Internal only", K["use"]: False,
                  K["src"]: "voice: built " + time.strftime("%Y-%m-%d") + " from " + origin}
        rid = cards.get("Voice: " + author)
        if rid:
            api("PATCH", f"https://api.airtable.com/v0/{BASE}/{T_KB}/{rid}", {"fields": fields})
        else:
            api("POST", f"https://api.airtable.com/v0/{BASE}/{T_KB}", {"records": [{"fields": fields}]})
        print("card written")
    for old in (msgs if lib else msgs[1:]):
        api("DELETE", f"https://api.airtable.com/v0/{BASE}/{T_KB}/{old['id']}")


if __name__ == "__main__":
    try:
        if MODE == "index-library":
            index_library()
        elif MODE == "build-post":
            build_post()
        elif MODE == "meeting-overviews":
            meeting_overviews()
        elif MODE == "voice-cards":
            voice_cards()
        elif MODE == "revise-copy":
            revise_copy()
        else:
            raise SystemExit(f"unknown MODE {MODE!r}")
    except Exception as e:
        print("Job failed. Details are on the Airtable row.")
        print(type(e).__name__)
        if MODE == "build-post" and REC:
            try:
                api("PATCH", f"https://api.airtable.com/v0/{BASE}/{T_POST}/{REC}",
                    {"fields": {P["note"]: ("Something went wrong drafting this post. Try again, or tell Kevin. " + str(e))[:250]}})
            except Exception:
                pass
        if MODE == "revise-copy" and REC:
            try:
                F = REV.get((os.environ.get("TABLE") or "post").strip().lower(), REV["post"])
                api("PATCH", f"https://api.airtable.com/v0/{BASE}/{F['table']}/{REC}",
                    {"fields": {F["rnote"]: "The automatic revision failed, so the text is unchanged. Apply the author's notes by hand. (" + str(e)[:200] + ")"}})
            except Exception:
                pass
        sys.exit(1)
