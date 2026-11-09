"""Deck review on the hub: slide previews, slide text, and the final build.

prepare()  runs after every deck build (walking deck, custom outline, converted deck). It reads the
           PPTX and PDF just written to the Deck Requests row, saves one JPEG per slide to Slide previews
           and the on-slide text (by text box and paragraph) to Slide text, then ticks Review ready.
           The automation "Deck ready: email requester" waits for that tick and sends the review link.
           If anything here fails the box is still ticked, so the email always goes out.

finalize() runs on the finalize-deck event, fired by the automation "Deck review: build the final"
           when the requester submits on the hub (Status = Finalizing). It applies every kept change
           from Review state to the PPTX text (formatting, layout, logo and colors untouched), converts
           to PDF, keeps the previous files in Earlier versions, writes Review summary, adds a Library
           Fixes row for each change flagged "wrong for everyone", and sets Status = Final. The automation
           "Deck final: email requester" then sends the final files.

Review state (written by the hub page, docs/deckreview):
  {"slides": {"3": {"status": "good"|"work", "note": "...", "library": true, "kept": true,
                    "after": {"s2": ["new paragraph 1", "new paragraph 2"]}}}}
Slide text (written here):
  [{"n": 1, "title": "...", "boxes": [{"k": "s2", "paras": ["...", "..."]}]}]
Keys: "s2" = third shape on the slide, "s2.1" = second shape inside that group, "s4:r1c0" = a table cell.
"""
import copy, json, os, re, subprocess, sys

import run_request as rr

F_PREV = "fldOBqD7uE1aMkL55"      # Slide previews
F_TEXT = "fldBNsStatpXxpyJ2"      # Slide text
F_STATE = "fldLC1a4PfYoqjndK"     # Review state
F_READY = "fldwLW2DoIx9ZVi40"     # Review ready
F_VER = "fldpELDqobTnQA7CH"       # Deck version
F_EARLIER = "fld3gYMlUUZdHpSZP"   # Earlier versions
F_SUMMARY = "fldO5Dkw2qtOMjQre"   # Review summary
T_FIX = "tblavLyonu3BOfK8q"       # Library Fixes
X = {"fix": "fld6D2nt8zrKK73YE", "status": "flddWHxjuopuLCUyB", "deck": "fldGl69UJtEIIN9cU",
     "by": "fldmziTXwFzibaDUq", "slide": "fldlGTDfywTktoCvA", "title": "flddhPEg4raVYwPfl",
     "audience": "fld2Hcon4PxcxWAUv", "note": "fldVHz1QYX5GH43ie", "before": "fld3jHpcoP4b5NNIx",
     "after": "fldTe1lfAQ6L7Mv5T", "libver": "fldUXhOIiGldqmB30"}
PPTX_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
SKIP_PH = {"FOOTER", "SLIDE_NUMBER", "DATE"}


# ---------- reading and writing slide text ----------

def _boxes(shapes, prefix=""):
    """Yield (key, text_frame_or_cell) for every text holder, in slide order."""
    for i, sh in enumerate(shapes):
        k = f"{prefix}{i}"
        if sh.shape_type is not None and int(sh.shape_type) == 6:          # group
            yield from _boxes(sh.shapes, k + ".")
            continue
        if sh.is_placeholder:
            try:
                if sh.placeholder_format.type is not None and sh.placeholder_format.type.name in SKIP_PH:
                    continue
            except Exception:
                pass
        if getattr(sh, "has_table", False) and sh.has_table:
            for r, row in enumerate(sh.table.rows):
                for c, cell in enumerate(row.cells):
                    yield f"s{k}:r{r}c{c}", cell.text_frame
            continue
        if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
            yield f"s{k}", sh.text_frame


def _ptext(p):
    return p.text.replace("\v", " ").strip()


def _noise(paras):
    t = " ".join(paras).strip()
    return not t or t.isdigit() or t.lower() in ("confidential", "c2 confidential")


def extract(prs):
    out = []
    for n, slide in enumerate(prs.slides, start=1):
        boxes = []
        for k, tf in _boxes(slide.shapes):
            paras = [_ptext(p) for p in tf.paragraphs]
            while paras and not paras[-1]:
                paras.pop()
            if not _noise(paras):
                boxes.append({"k": k, "paras": paras})
        title = ""
        try:
            if slide.shapes.title is not None:
                title = slide.shapes.title.text_frame.text.replace("\v", " ").strip()
        except Exception:
            pass
        if not title and boxes:
            title = next((p for b in boxes for p in b["paras"] if p), "")
        out.append({"n": n, "title": title[:120], "boxes": boxes})
    return out


def _set_para(p, text):
    if _ptext(p) == text.strip():
        return                                           # unchanged: keep line breaks and runs as built
    runs = p.runs
    if not runs:
        p.text = text
        return
    lead = runs[0].text
    if len(runs) > 1 and lead.strip() and text.startswith(lead):
        keep = [runs[0], runs[1]]                       # bold lead-in kept as is, the rest in the body style
        runs[1].text = text[len(lead):]
    else:
        body = max(runs, key=lambda r: len(r.text))     # the body style, not a short bold or colored word
        body.text = text
        keep = [body]
    keep_els = {r._r for r in keep}
    parent = runs[0]._r.getparent()
    for el in list(parent):                             # drop every other run, line break and field
        tag = el.tag.rsplit("}", 1)[-1]
        if tag in ("r", "br", "fld") and el not in keep_els:
            parent.remove(el)


def _set_frame(tf, paras):
    paras = [str(x) for x in paras] or [""]
    ps = list(tf.paragraphs)
    while len(ps) < len(paras):                          # new lines copy the last line's formatting
        new = copy.deepcopy(ps[-1]._p)
        ps[-1]._p.addnext(new)
        ps = list(tf.paragraphs)
    for p, t in zip(ps, paras):
        _set_para(p, t)
    for p in ps[len(paras):]:                            # fewer lines than before
        p._p.getparent().remove(p._p)


def apply_edits(prs, edits):
    """edits: {slide number: {box key: [paragraphs]}}. Returns how many boxes changed."""
    changed = 0
    slides = list(prs.slides)
    for n, boxes in edits.items():
        n = int(n)
        if not 1 <= n <= len(slides):
            continue
        frames = dict(_boxes(slides[n - 1].shapes))
        for k, paras in (boxes or {}).items():
            tf = frames.get(k)
            if tf is None or not isinstance(paras, list):
                continue
            _set_frame(tf, paras)
            changed += 1
    return changed


# ---------- previews ----------

def render_previews(pdf, outdir):
    os.makedirs(outdir, exist_ok=True)
    for f in os.listdir(outdir):
        os.remove(os.path.join(outdir, f))
    subprocess.run(["pdftoppm", "-jpeg", "-jpegopt", "quality=82", "-scale-to", "1280", pdf,
                    os.path.join(outdir, "slide")], check=True, timeout=240,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    files = sorted(os.listdir(outdir), key=lambda x: int("".join(c for c in x if c.isdigit()) or 0))
    out = []
    for i, name in enumerate(files, start=1):        # stable names: slide-01.jpg, slide-02.jpg ...
        dst = os.path.join(outdir, f"slide-{i:02d}.jpg")
        os.rename(os.path.join(outdir, name), dst)
        out.append(dst)
    return out


def save_review_assets(pptx, pdf):
    from pptx import Presentation
    text = extract(Presentation(pptx))
    rr.update({F_PREV: [], F_TEXT: json.dumps(text, ensure_ascii=False)[:99000]})
    for img in render_previews(pdf, os.path.join(rr.WORK, "previews")):
        rr.upload(F_PREV, img, "image/jpeg")
    return text


def _fetch_files(f, tag, need_pdf=True):
    os.makedirs(rr.WORK, exist_ok=True)
    pp, pd = (f.get(rr.F["pptx"]) or [None])[0], (f.get(rr.F["pdf"]) or [None])[0]
    if not pp:
        raise RuntimeError("No PPTX on the row.")
    pptx = os.path.join(rr.WORK, f"{tag}.pptx")
    rr.download(pp["url"], pptx)
    if not need_pdf:
        return pp, pd, pptx, None
    if pd:
        pdf = os.path.join(rr.WORK, f"{tag}.pdf")
        rr.download(pd["url"], pdf)
    else:
        subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", rr.WORK, pptx],
                       check=True, timeout=240, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pdf = pptx[:-5] + ".pdf"
    return pp, pd, pptx, pdf


def reset():
    """Start of every build: a rebuild starts a fresh review."""
    rr.update({F_READY: False, F_STATE: "", F_TEXT: "", F_PREV: [], F_SUMMARY: "", F_VER: 1, F_EARLIER: [],
               "fld1umKyM2DUfQLBj": None})   # Review reminders sent: a rebuilt deck gets its own 2 reminders


def prepare():
    """After a build has set Status = Ready. Always ends with Review ready ticked."""
    note = None
    try:
        f = rr.get_record(rr.T_REQ, rr.REC)
        _, _, pptx, pdf = _fetch_files(f, "review_src")
        text = save_review_assets(pptx, pdf)
        print("review assets:", len(text), "slides")
    except Exception as e:
        note = f"Slide previews for the review page failed: {str(e)[:300]}"
        print("review assets: failed")
    fields = {F_READY: True}
    if note:
        try:
            cur = rr.get_record(rr.T_REQ, rr.REC).get(rr.F["rcheck"]) or ""
            fields[rr.F["rcheck"]] = (note + ("\n" + cur if cur else ""))[:95000]
        except Exception:
            pass
    rr.update(fields)


# ---------- final build ----------

def _join(paras):
    return "\n".join(p for p in (paras or []) if p)


def rework(f, state):
    """Redesign and research requests from the review page (custom outline and converted decks).
    Rebuilds the deck from its slide plan with the requested slides replaced, keeps every text change
    already kept on the other slides, refreshes previews and text, and puts the deck back In review."""
    import custom_deck as CD
    from pptx import Presentation
    spec = json.loads(f.get(rr.F["plan"]) or "{}")
    if not isinstance(spec, dict) or not spec.get("slides"):
        raise RuntimeError("This deck has no slide plan, so slides cannot be redesigned here.")
    try:
        outline = json.loads(f.get(CD.F_OUTLINE) or "{}")
    except Exception:
        outline = {}
    os.makedirs(rr.WORK, exist_ok=True)
    slides = state.get("slides") or {}
    meta = CD.build_meta(f)
    meta["deck_title"] = spec.get("title") or ""
    done, report, edits = [], [], {}
    for n in sorted(slides, key=lambda x: int(x) if str(x).isdigit() else 999):
        st = slides[n] or {}
        i = int(n) - 1 if str(n).isdigit() else -1
        if not (0 < i < len(spec["slides"])):
            continue
        cur = spec["slides"][i]
        if st.get("status") == "research" and isinstance(st.get("research"), dict):
            rq = st["research"]
            entries = outline.get("slides") or []
            base = dict(entries[i - 1]) if 0 <= i - 1 < len(entries) and isinstance(entries[i - 1], dict) else {}
            rules = dict(base.get("research") or {})
            if rq.get("months"):
                rules["months"] = rq["months"]
            if rq.get("rules") is not None:
                rules["rules"] = rq["rules"]
            entry = {"layout": "research", "content": (rq.get("topic") or base.get("content") or cur.get("title") or "").strip(),
                     "research": rules}
            new = CD.research_slide(entry, meta, report)
            spec["slides"][i] = new
            done.append((n, "new research", new.get("title") or ""))
        elif st.get("status") == "redesign":
            rq = st.get("redesign") or {}
            try:
                new = CD.redesign_slide(cur, rq.get("layout"), rq.get("note"), meta, spec.get("title") or "")
                spec["slides"][i] = new
                done.append((n, "redesigned as " + new.get("layout", ""), rq.get("note") or ""))
            except Exception as e:
                report.append(f"Slide {n}: redesign failed ({str(e)[:150]}), slide left as it was.")
                done.append((n, "redesign failed, unchanged", ""))
        elif st.get("kept") and isinstance(st.get("after"), dict) and st["after"]:
            edits[n] = st["after"]
    tpl = rr.fetch_assets()
    pp = (f.get(rr.F["pptx"]) or [None])[0]
    pd = (f.get(rr.F["pdf"]) or [None])[0]
    version = int(f.get(F_VER) or 1) + 1
    stem = re.sub(r"_v\d+$", "", (pp or {}).get("filename", "evoila_deck.pptx").rsplit(".", 1)[0])
    out = os.path.join(rr.WORK, re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_")[:90] + f"_v{version}.pptx")
    os.makedirs(rr.WORK, exist_ok=True)
    CD.render(spec, tpl, out, meta, rr.ENGINE)
    if edits:
        prs = Presentation(out)
        apply_edits(prs, edits)
        prs.save(out)
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", rr.WORK, out],
                   check=True, timeout=240, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pdf = out[:-5] + ".pdf"
    keep = [{"id": a["id"]} for a in (f.get(F_EARLIER) or [])]
    keep += [{"url": a["url"], "filename": a.get("filename")} for a in (pp, pd) if a]
    rr.update({F_EARLIER: keep, rr.F["pptx"]: [], rr.F["pdf"]: [], rr.F["plan"]: json.dumps(spec, indent=1)[:95000]})
    rr.upload(rr.F["pptx"], out, PPTX_TYPE)
    rr.upload(rr.F["pdf"], pdf, "application/pdf")
    save_review_assets(out, pdf)
    for n, what, _ in done:                     # reworked slides come back unreviewed, flagged for a look
        slides[n] = {"reworked": what}
    state.pop("mode", None)
    state.pop("reworkError", None)
    state["slides"] = slides
    cur_check = f.get(rr.F["rcheck"]) or ""
    summary = "; ".join(f"slide {n} {what}" for n, what, _ in done)
    rr.update({F_STATE: json.dumps(state), F_VER: version, rr.F["status"]: "In review", rr.F["error"]: "",
               rr.F["log"]: rr.RUN_URL or None,
               rr.F["rcheck"]: ("\n".join([f"Review round v{version}: {summary}"] + report) + ("\n" + cur_check if cur_check else ""))[:95000]})
    print("rework:", len(done), "slides")


def finalize():
    f = rr.get_record(rr.T_REQ, rr.REC)
    if f.get(rr.F["status"]) != "Finalizing":
        print("finalize: row is not Finalizing, nothing to do")
        return
    try:
        st0 = json.loads(f.get(F_STATE) or "{}")
    except Exception:
        st0 = {}
    if any((v or {}).get("status") in ("redesign", "research") for v in (st0.get("slides") or {}).values()):
        try:
            return rework(f, st0)
        except Exception as e:
            st0["reworkError"] = "The redesign didn't go through. Your requests are saved; try again, or tell Kevin."
            rr.update({rr.F["status"]: "In review", F_STATE: json.dumps(st0),
                       rr.F["rcheck"]: f"Redesign round failed: {str(e)[:400]}\n" + (f.get(rr.F["rcheck"]) or "")})
            print("rework failed")
            return
    try:
        state = json.loads(f.get(F_STATE) or "{}")
    except Exception:
        state = {}
    try:
        text = json.loads(f.get(F_TEXT) or "[]")
    except Exception:
        text = []
    by_n = {str(s.get("n")): s for s in text if isinstance(s, dict)}
    slides = state.get("slides") or {}
    version = int(f.get(F_VER) or 1)

    edits, lines, fixes = {}, [], []
    for n in sorted(slides, key=lambda x: int(x) if str(x).isdigit() else 999):
        s = slides[n] or {}
        info = by_n.get(str(n), {})
        title = info.get("title") or f"Slide {n}"
        if s.get("status") == "work" and s.get("kept") and isinstance(s.get("after"), dict) and s["after"]:
            edits[n] = s["after"]
            lines.append(f"Slide {n} ({title}): changed. Note: {s.get('note') or '(none)'}"
                         + ("  [also sent to the slide library]" if s.get("library") else ""))
            if s.get("library"):
                before = {b["k"]: b["paras"] for b in info.get("boxes", [])}
                fixes.append({
                    X["fix"]: f"Slide {n}: {title}"[:120],
                    X["status"]: "New",
                    X["deck"]: [rr.REC],
                    X["by"]: f.get(rr.F["name"]) or "",
                    X["slide"]: int(n) if str(n).isdigit() else None,
                    X["title"]: title,
                    X["audience"]: f.get(rr.F["audience"]) or "",
                    X["note"]: s.get("note") or "",
                    X["before"]: "\n\n".join(_join(before.get(k)) for k in s["after"])[:90000],
                    X["after"]: "\n\n".join(_join(v) for v in s["after"].values())[:90000],
                    X["libver"]: f.get(rr.F["libver"]) or "",
                })

    total = len(text) or len(slides)
    good = sum(1 for s in slides.values() if (s or {}).get("status") == "good")
    if edits:
        from pptx import Presentation
        pp, pd, pptx, _ = _fetch_files(f, "before_final", need_pdf=False)
        prs = Presentation(pptx)
        changed = apply_edits(prs, edits)
        stem = (pp.get("filename") or "evoila_deck.pptx").rsplit(".", 1)[0]
        for suffix in ("_v2", "_v3", "_v4", "_v5", "_v6", "_v7", "_v8", "_v9"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
        version += 1
        out = os.path.join(rr.WORK, re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_")[:90] + f"_v{version}.pptx")
        prs.save(out)
        subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", rr.WORK, out],
                       check=True, timeout=240, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pdf = out[:-5] + ".pdf"
        keep = [{"id": a["id"]} for a in (f.get(F_EARLIER) or [])]          # already stored: keep as is
        keep += [{"url": a["url"], "filename": a.get("filename")} for a in (pp, pd) if a]
        rr.update({F_EARLIER: keep, rr.F["pptx"]: [], rr.F["pdf"]: []})
        rr.upload(rr.F["pptx"], out, PPTX_TYPE)
        rr.upload(rr.F["pdf"], pdf, "application/pdf")
        try:
            save_review_assets(out, pdf)
        except Exception:
            pass                                          # previews are a nicety; the files are what matter
        head = (f"v{version} final: {len(edits)} slide{'s' if len(edits) != 1 else ''} changed, "
                f"{good} marked Looks good, {total} slides in all.")
        print("finalize:", changed, "text boxes changed")
    else:
        head = f"v{version} final: no changes. All {total} slides marked Looks good."
    if fixes:
        for i in range(0, len(fixes), 10):
            rr.api("POST", f"https://api.airtable.com/v0/{rr.BASE}/{T_FIX}",
                   {"records": [{"fields": {k: v for k, v in x.items() if v is not None}} for x in fixes[i:i + 10]],
                    "typecast": True})
        head += f" {len(fixes)} fix{'es' if len(fixes) != 1 else ''} sent to the slide library queue."
    rr.update({F_SUMMARY: "\n".join([head] + lines)[:95000], F_VER: version,
               rr.F["status"]: "Final", rr.F["error"]: "", rr.F["log"]: rr.RUN_URL or None})


if __name__ == "__main__":
    {"prepare": prepare, "finalize": finalize}[sys.argv[1]]()
