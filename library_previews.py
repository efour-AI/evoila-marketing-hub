"""Resource Library previews for the hub's "look before you download" viewer.

For every Resource Library row shown on the hub that has a PowerPoint, PDF or Word file, this draws one
JPEG per slide or page into Slide previews, so the hub can flip through it before anyone downloads.

  - The source is the row's PDF when there is one (it matches what people download), otherwise the PPTX
    or DOCX converted with LibreOffice. At most MAX_PAGES pages are drawn.
  - Previews built from holds the attachment ID used. A row is redrawn only when its file changes
    (or FORCE=1), so running this often costs nothing.
  - Slide guide (JSON, one entry per page: n, title, about) and Details (who it is for, when to use it,
    how it runs) are written by marketing. When either is empty, or the guide no longer matches the
    page count, Claude drafts it from the file's text and speaker notes. Hand edits are never overwritten
    while the page count still matches.

Run by hub_jobs.py (MODE=library-previews, and at the end of every index-library run).
Public repository: never print document text to the log.
"""
import json, os, re, shutil, subprocess

import hub_jobs as hj

T_RES = hj.T_RES
F_NAME, F_FILE, F_DESC, F_CAT = "fld2D09t4tkerjQfw", "fld4tiUzYaV2LsOdX", "fldyGfovxWKaHU5pH", "fld2wc1SFDUQDKcjo"
F_DETAILS, F_GUIDE, F_PREV, F_FROM = "fldTM1FAL39s5tr9w", "fld5W1P2STVkJCBJn", "fldVN0I6t4hHs8O1F", "fldAtIDVLgDLSnVuM"
MAX_PAGES = 60
FORCE = os.environ.get("FORCE", "") == "1"
WORK = os.path.join(hj.WORK, "previews")


def _ext(a):
    return (a.get("filename", "").rsplit(".", 1)[-1] or "").lower()


def pick_source(files):
    by = {}
    for a in files or []:
        by.setdefault(_ext(a), a)
    for e in ("pdf", "pptx", "docx", "ppt", "doc"):
        if e in by:
            return by[e], by
    return None, by


def to_pdf(path):
    if path.lower().endswith(".pdf"):
        return path
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", os.path.dirname(path), path],
                   check=True, timeout=300, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out = os.path.splitext(path)[0] + ".pdf"
    if not os.path.exists(out):
        raise RuntimeError("PDF conversion produced nothing")
    return out


def render(pdf, outdir):
    shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir)
    subprocess.run(["pdftoppm", "-jpeg", "-jpegopt", "quality=80", "-scale-to", "1280", "-l", str(MAX_PAGES),
                    pdf, os.path.join(outdir, "p")], check=True, timeout=300,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    files = sorted(os.listdir(outdir), key=lambda x: int("".join(c for c in x if c.isdigit()) or 0))
    out = []
    for i, name in enumerate(files, 1):
        dst = os.path.join(outdir, f"page-{i:02d}.jpg")
        os.rename(os.path.join(outdir, name), dst)
        out.append(dst)
    return out


def page_text(pptx, pdf, n):
    """[(title, text, notes)] per page, from the PPTX when there is one, else from the PDF."""
    out = []
    if pptx:
        from pptx import Presentation
        for s in list(Presentation(pptx).slides)[:n]:
            title = (s.shapes.title.text if s.shapes.title is not None else "").strip()
            tx = [sh.text_frame.text for sh in s.shapes if sh.has_text_frame and sh.text_frame.text.strip()]
            if not title:
                title = next((t.split("\n")[0] for t in tx if len(t) > 12 and not t.isdigit()
                              and t.strip() not in ("Confidential",)), "")
            notes = s.notes_slide.notes_text_frame.text if s.has_notes_slide else ""
            out.append((title.strip(), " | ".join(tx), notes))
    else:
        from pypdf import PdfReader
        for pg in PdfReader(pdf).pages[:n]:
            t = (pg.extract_text() or "").strip()
            first = next((ln.strip() for ln in t.split("\n") if len(ln.strip()) > 3), "")
            out.append((first[:90], t, ""))
    while len(out) < n:
        out.append(("", "", ""))
    return out


def draft(name, desc, cat, pages, need_details, deck):
    """Claude writes the per-page guide (and Details when empty). Falls back to titles only."""
    fallback = [{"n": i, "title": t or f"Page {i}", "about": ""} for i, (t, _, _) in enumerate(pages, 1)]
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return fallback, None
    import ai_steps
    kind = "slide" if deck else "page"
    body = "\n\n".join(f"[{kind.upper()} {i}] TITLE: {t}\nON THE {kind.upper()}: {x[:1200]}\nSPEAKER NOTES: {nt[:800]}"
                       for i, (t, x, nt) in enumerate(pages, 1))
    prompt = f"""You are writing the guide that appears on the evoila US marketing hub when a salesperson clicks a
library item before downloading it. They flip through the {kind}s one at a time and read your line under each.

ITEM: {name}
CATEGORY: {cat}
ONE-LINE DESCRIPTION ALREADY ON THE HUB: {desc}

Write JSON only, no code fences, in exactly this shape:
{{"details": "<3 or 4 short paragraphs separated by blank lines, each opening with a plain label: 'Who it is for:', 'What it does:', 'How to use it:', and if useful 'Before you use it:'>",
  "pages": [{{"n": 1, "title": "<short title of the {kind}>", "about": "<one or two sentences, under 30 words: what this {kind} covers and when or how to use it>"}}]}}

Rules:
- One entry in "pages" for every {kind}, {len(pages)} in total, in order.
- Use only what is in the file. Never invent customers, numbers, dates or claims.
- Speaker notes marked OWNER NOTES are internal: you may say a {kind} still has items to confirm, never quote them.
- If a {kind} is not meant to be presented (a guide or divider), say so.
- The text was pulled out page by page, so sentences and tables may break across pages. That is how the text
  was extracted, not a fault in the file: never say a page is cut off, incomplete or breaks mid-sentence.
- Plain, direct sentences. No em dashes. Write evoila in lowercase. No marketing hype.

THE FILE, {kind} by {kind}:
{body[:150000]}"""
    try:
        resp = ai_steps._call({"model": ai_steps.MODEL, "max_tokens": 12000,
                               "messages": [{"role": "user", "content": prompt}]}, timeout=300)
        out = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text").strip()
        out = re.sub(r"^```(json)?|```$", "", out).strip()
        j = json.loads(out)
        got = {int(p.get("n", 0)): p for p in j.get("pages", []) if isinstance(p, dict)}
        guide = []
        for i, fb in enumerate(fallback, 1):
            p = got.get(i, {})
            guide.append({"n": i, "title": _clean(p.get("title")) or fb["title"], "about": _clean(p.get("about"))})
        details = _clean(j.get("details")) if need_details else None
        return guide, details
    except Exception as e:
        print("guide draft failed:", type(e).__name__)
        return fallback, None


def _clean(s):
    s = str(s or "").strip()
    return s.replace("—", ", ").replace(" , ", ", ").replace("Evoila", "evoila")


def one(r):
    f, rid = r["fields"], r["id"]
    src, by = pick_source(f.get(F_FILE))
    if not src:
        return "no file"
    if not FORCE and f.get(F_FROM) == src["id"] and f.get(F_PREV):
        return "up to date"
    d = os.path.join(WORK, rid)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    path = os.path.join(d, "src." + _ext(src))
    hj.download(src["url"], path)
    pptx = None
    if "pptx" in by:
        pptx = path if _ext(src) == "pptx" else os.path.join(d, "deck.pptx")
        if pptx != path:
            hj.download(by["pptx"]["url"], pptx)
    pdf = to_pdf(path)
    imgs = render(pdf, os.path.join(d, "img"))
    if not imgs:
        return "no pages"

    hj.api("PATCH", f"https://api.airtable.com/v0/{hj.BASE}/{T_RES}/{rid}", {"fields": {F_PREV: []}})
    for img in imgs:
        hj.upload_attachment(rid, F_PREV, img, "image/jpeg")

    fields = {F_FROM: src["id"]}
    try:
        cur = json.loads(f.get(F_GUIDE) or "[]")
    except Exception:
        cur = []
    need_guide = not isinstance(cur, list) or len(cur) != len(imgs)
    need_details = not (f.get(F_DETAILS) or "").strip()
    if need_guide or need_details:
        pages = page_text(pptx, pdf, len(imgs))
        guide, details = draft(f.get(F_NAME, ""), f.get(F_DESC, ""), (f.get(F_CAT) or ""), pages, need_details, bool(pptx))
        if need_guide:
            fields[F_GUIDE] = json.dumps(guide, ensure_ascii=False)[:99000]
        if details:
            fields[F_DETAILS] = details[:20000]
    hj.api("PATCH", f"https://api.airtable.com/v0/{hj.BASE}/{T_RES}/{rid}", {"fields": fields})
    return f"{len(imgs)} pages" + (" + guide" if F_GUIDE in fields else "") + (" + details" if F_DETAILS in fields else "")


def run():
    os.makedirs(WORK, exist_ok=True)
    rows = hj.rows(T_RES, "{Show on hub}")
    for r in rows:
        try:
            print("previews:", r["id"], one(r))
        except Exception as e:
            print("previews:", r["id"], "failed", type(e).__name__, str(e)[:120])


if __name__ == "__main__":
    run()
