"""Custom outline decks: the requester writes the outline, Claude writes the slide copy, we lay it out on brand.

Fired for Deck Requests rows with Request type = "Custom outline". The hub's deck outline page sends a JSON
outline (deck title plus one entry per slide: a layout, or "auto", and what the requester wants on it).

1. Claude turns the outline into slide copy that follows the evoila rules (make_spec).
2. render() draws each slide on the evoila master template with the same helpers as the walking deck
   (pptkit.py and the template plumbing in build_library.py, both from the engine zip on Deck Assets).
3. run() converts to PDF, uploads both files to the row and sets Status = Ready; the existing
   "Deck ready: email requester" automation sends them.

Research slides: an outline entry with layout "research" carries a topic and the requester's rules (how recent,
where it applies, which kinds of source, anything else). Claude researches it with web search, every figure is
checked on its source page with the same rules as the walking deck's data slides (trusted sources, the figure
must appear on the page, published and covering data inside the requester's window), and up to four confirmed
figures go on a Big numbers slide with their sources. With fewer than two confirmed figures the slide keeps
[figure] placeholders and the notes say why; the full check report goes to the row's Market data check field.

Nothing here prints request data: the repo is public and the build logs are visible.
"""
import datetime, json, math, os, re, subprocess, sys

import run_request as rr
import ai_steps

F_TYPE = "fldQahyDEnFyMIm87"
F_OUTLINE = "fldokfA560RE3GLh4"
F_ROOM = "fldh3Ppbw0ItJoTFw"
F_NEXT = "fldFr9808gsFms3qY"
F_BACKGROUND = "fldiUPLZFpHCY7ArN"

LAYOUTS = ["cover", "agenda", "statement", "bullets", "columns", "cards", "numbers", "steps", "comparison",
           "quote", "closing"]
ICON_NAMES = ["ArrowRight", "Award", "BadgeCheck", "Blocks", "Boxes", "Building2", "CalendarCheck", "ChartLine",
              "CircleCheck", "ClipboardCheck", "Cloud", "Coins", "Container", "Cpu", "Database", "Factory", "Gauge",
              "GitBranch", "Globe", "GraduationCap", "Handshake", "HardDrive", "HeartPulse", "Landmark", "Laptop",
              "Layers", "LifeBuoy", "Lock", "Mail", "MapPin", "MessagesSquare", "Monitor", "Network", "PackageCheck",
              "PiggyBank", "Recycle", "RefreshCw", "Rocket", "Route", "Scaling", "Search", "Server", "ServerCog",
              "Settings", "Shield", "ShieldCheck", "Sparkles", "Target", "Timer", "TrendingDown", "TrendingUp",
              "TriangleAlert", "UserCheck", "Users", "Workflow", "Zap"]

# ---------------------------------------------------------------- Claude: outline -> slide copy

ITEM = {"type": "object", "properties": {
    "heading": {"type": "string"}, "text": {"type": "string"},
    "points": {"type": "array", "items": {"type": "string"}},
    "figure": {"type": "string"}, "icon": {"type": "string", "enum": ICON_NAMES}}}
SPEC_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"}, "subtitle": {"type": "string"},
        "slides": {"type": "array", "items": {"type": "object", "properties": {
            "layout": {"type": "string", "enum": LAYOUTS},
            "title": {"type": "string"}, "kicker": {"type": "string"},
            "statement": {"type": "string"}, "support": {"type": "string"},
            "items": {"type": "array", "items": ITEM},
            "headers": {"type": "array", "items": {"type": "string"}},
            "rows": {"type": "array", "items": {"type": "object", "properties": {
                "label": {"type": "string"}, "cells": {"type": "array", "items": {"type": "string"}}}}},
            "source": {"type": "string"}, "notes": {"type": "string"}},
            "required": ["layout", "notes"]}},
    },
    "required": ["title", "slides"],
}

SPEC_PROMPT = """You write the slide copy for a presentation for evoila US, a VMware by Broadcom specialist services firm in North America (private cloud, private AI, security and hybrid operations on VMware Cloud Foundation; assessment, build, migration and lifecycle services). The person asking wrote an outline. Your job is to turn each outline entry into one finished slide, then call submit_deck.

WHO AND WHAT
Presenter(s): {presenters}
Audience: {audience}
Company they are presenting to (may be empty): {company}
What they want the audience to do next: {next_step}
Deck title they gave: {deck_title}
Notes about the meeting (may be empty): {notes}
Background material they pasted (may be empty): {background}

OUTLINE (one entry per slide, in order; "layout" is their pick or "auto")
{outline}

LAYOUTS (choose the one that fits each entry; honor the requester's pick unless the content cannot fit it)
- cover: first slide only. Uses the deck title and subtitle; no other fields.
- agenda: title + 3 to 6 items (heading only, under 40 characters each).
- statement: dark slide with one big message. kicker (under 30 characters, optional), statement (under 110 characters), support (one sentence under 140 characters, optional).
- bullets: title + 3 to 6 items; each item has an optional heading (2 to 4 words, bolded) and text (under 110 characters).
- columns: title + 2 or 3 items; each item has a heading (under 32 characters) and 2 to 4 points (under 75 characters each).
- cards: title + 3 or 4 items; each item has a heading (under 30 characters), text (under 95 characters) and an icon from the allowed list.
- numbers: title + 2 to 4 items; each item has figure (short, at most 8 characters, for example 40%, 3x, $1.5T, Oct 2027) and heading (the label, under 60 characters); source (who published the figures). The heading completes the figure in lower case where it reads on from it (for example "of enterprises run ..."). Only use figures the requester supplied. If they asked for numbers without giving them, use bracketed placeholders such as [figure] and say in the notes what to fill in.
- steps: title + 3 to 5 items; each item has a heading (under 28 characters) and text (under 90 characters).
- comparison: title + headers (2 or 3 column names, the first column holds row labels and has no header) + 2 to 5 rows; each row has a label (under 28 characters) and one short cell per header (under 60 characters).
- quote: dark slide. statement is the quote (under 160 characters, only words the requester supplied), support is the attribution.
- closing: dark slide, usually last. title (for example Next steps) + up to 3 items (heading only, under 80 characters). The presenters' contact cards are added automatically.
Every slide except cover has a title under 60 characters, written as the point of the slide, not a label.

RULES
- First slide is always cover. If the outline does not start with a cover, add one. If the requester asked for a closing or next-steps slide, use the closing layout.
- One slide per outline entry. Do not add or drop slides other than the cover.
- Write only what the requester asked for, in their direction. You may tighten and organize their words; keep their meaning.
- Never invent customers, case studies, results, figures, prices, savings, certifications, awards or dates. If the outline needs a fact you do not have, put a bracketed placeholder like [customer example] on the slide and say in the notes what is missing.
- evoila facts you may use if relevant: the credentials, written exactly this way: Broadcom Global Pinnacle Partner, VCSP Global Pinnacle Partner, President's Choice Award 2025, VCSP Partner of the Year 2025 (this one recognizes how evoila runs customer environments day to day, so lead with it when the slide is about operating or managed services); assessment-led approach; greenfield migration through a repeatable migration factory; work delivered in phases a customer can commission one at a time; a North American team backed by evoila's global VMware depth. When the requester names a credential or fact, keep it on the slide.
- Delivery under channel partners' badges is said only to Partner audiences. For any other audience never put it on a slide or in the notes.
- Everything on a slide is what evoila does. No negative statements about what evoila will not do, and no partner or competitor names that the requester did not give.
- Plain, direct, confident language. Short sentences. No hype, no buzzwords, no exclamation marks.
- No em dashes or en dashes anywhere. Use commas, colons or periods.
- notes: 2 to 4 sentences of speaker notes per slide, in the presenter's voice, saying how to land the slide. List anything in brackets that the presenter must fill in before the meeting.
- subtitle: one line under 70 characters for the cover that says what the deck is about. Do not name the company or event; the cover already says who it is prepared for.

Call submit_deck once with the whole deck."""


def _clean(t):
    t = str(t or "")
    t = t.replace("—", ", ").replace("–", "-").replace(" ,", ",")
    return re.sub(r"\s{2,}", " ", t).strip()


def _clean_obj(o):
    if isinstance(o, str):
        return _clean(o)
    if isinstance(o, list):
        return [_clean_obj(x) for x in o]
    if isinstance(o, dict):
        return {k: (_clean_obj(v) if k not in ("layout", "icon") else v) for k, v in o.items()}
    return o


def make_spec(outline, meta):
    lines = []
    for i, s in enumerate(outline.get("slides") or [], 1):
        lines.append(f"{i}. layout: {s.get('layout') or 'auto'}\n   wants: {str(s.get('content') or '').strip()}")
    prompt = SPEC_PROMPT.format(
        presenters=meta.get("presenters") or "", audience=meta.get("audience") or "",
        company=meta.get("company") or "", next_step=meta.get("next_step") or "",
        deck_title=outline.get("title") or "", notes=meta.get("notes") or "",
        background=(meta.get("background") or "")[:30000], outline="\n".join(lines) or "(empty)")
    tools = [{"name": "submit_deck", "description": "Submit the finished deck.", "input_schema": SPEC_SCHEMA}]
    messages = [{"role": "user", "content": prompt}]
    for _ in range(3):
        resp = ai_steps._call({"model": ai_steps.MODEL, "max_tokens": 12000, "tools": tools, "messages": messages},
                              timeout=240)
        content = resp.get("content", [])
        spec = None
        for b in content:
            if b.get("type") == "tool_use" and b.get("name") == "submit_deck":
                spec = b.get("input") or {}
        if spec is None:
            text = " ".join(b.get("text", "") for b in content if b.get("type") == "text")
            m = re.search(r"\{.*\}", text, re.S)
            try:
                spec = json.loads(m.group(0)) if m else None
            except Exception:
                spec = None
        if isinstance(spec, dict) and spec.get("slides"):
            return _clean_obj(spec)
        messages.append({"role": "assistant", "content": content})
        if resp.get("stop_reason") == "tool_use":
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b["id"], "content": "Missing slides. Call submit_deck again with every slide."}
                for b in content if b.get("type") == "tool_use"]})
        else:
            messages.append({"role": "user", "content": "Call submit_deck now with the whole deck."})
    raise RuntimeError("Claude returned no deck")


def fallback_spec(outline):
    """No Claude (missing key or API down): lay the requester's own words out as bullet slides."""
    slides = [{"layout": "cover", "notes": "Built without the copywriting step; check every slide before the meeting."}]
    for s in outline.get("slides") or []:
        txt = str(s.get("content") or "").strip()
        parts = [p.strip(" -*•") for p in re.split(r"[\n;]+", txt) if p.strip(" -*•")]
        head = parts[0][:60] if parts else "Slide"
        rest = parts[1:] or parts
        lay = s.get("layout") if s.get("layout") in ("closing", "statement") else "bullets"
        if lay == "statement":
            slides.append({"layout": "statement", "statement": txt[:110], "notes": ""})
        elif lay == "closing":
            slides.append({"layout": "closing", "title": "Next steps", "items": [{"heading": p[:80]} for p in parts[:3]], "notes": ""})
        else:
            slides.append({"layout": "bullets", "title": head, "items": [{"text": p[:110]} for p in rest[:6]], "notes": ""})
    return _clean_obj({"title": outline.get("title") or "evoila", "subtitle": "", "slides": slides})


# ---------------------------------------------------------------- rendering

MONTHS = {m: m[:3] for m in ("January February March April May June July August September October November December").split()}


def _money(v):
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= div:
            x = v / div
            return "$" + (f"{x:.2f}" if x < 10 else f"{x:.1f}" if x < 100 else f"{x:.0f}").rstrip("0").rstrip(".") + suf
    return f"${v:,.0f}"


def normalize_figure(fig, label):
    """Make a big-number figure short enough to read at a glance.
    '1,484,397' + 'spending, millions of US dollars' -> '$1.48T' + 'spending'. 'October 2027' -> 'Oct 2027'."""
    f, l = str(fig or "").strip(), str(label or "").strip()
    m = re.fullmatch(r"\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?", f)
    unit = re.search(r",?\s*\(?\s*(?:in\s+)?(millions|billions|thousands)\s+of\s+(?:US\s+|U\.S\.\s+)?dollars\s*\)?", l, re.I)
    if m and unit:
        v = float((m.group(1) + (m.group(2) or "")).replace(",", "")) * {"thousands": 1e3, "millions": 1e6, "billions": 1e9}[unit.group(1).lower()]
        f = _money(v)
        l = (l[:unit.start()] + l[unit.end():]).strip(" ,")
    elif m and len(m.group(1).replace(",", "")) >= 6:
        v = float(m.group(1).replace(",", ""))
        f = (f"{v / 1e9:.1f}B" if v >= 1e9 else f"{v / 1e6:.1f}M").replace(".0", "")
    for long, short in MONTHS.items():
        f = re.sub(r"\b" + long + r"\b", short, f)
    return f, l


def display_figure(f):
    return str(f or "").strip()


def display_label(l):
    return str(l or "").strip()


def _fit(t, n, base, low):
    l = len(t or "")
    return base if l <= n else max(low, round(base * math.sqrt(n / l), 1))


def render(spec, tpl_path, out, meta, engine_dir):
    sys.path.insert(0, engine_dir)
    import build_library as BL
    import pptkit as K
    from pptx import Presentation
    from pptx.util import Pt
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
    from pptx.enum.dml import MSO_LINE_DASH_STYLE as DASH
    VIOLET, VIOLET2, GREEN, BLUE, LBLUE, INDIGO, CREAM, WHITE = (K.VIOLET, K.VIOLET2, K.GREEN, K.BLUE, K.LBLUE,
                                                                 K.INDIGO, K.CREAM, K.WHITE)
    SOFT, MUTED = BL.SOFT, BL.MUTED
    box, text, text_in, icon, icon_disc, line, pill = K.box, K.text, K.text_in, K.icon, K.icon_disc, K.line, K.pill

    BL.TPL = Presentation(tpl_path)
    prs = Presentation(tpl_path)
    sl = prs.slides._sldIdLst
    for sid in list(sl):
        prs.part.drop_rel(sid.rId); sl.remove(sid)
    new = BL.new_slide
    X0, W = 0.68, 11.97
    ACC = [GREEN, LBLUE, INDIGO, BLUE]

    def items(s, lo=0, hi=9):
        return [i for i in (s.get("items") or []) if isinstance(i, dict)][:hi]

    def ic(it, default="CircleCheck"):
        n = it.get("icon")
        return n if n in ICON_NAMES else BL.pick_icon((it.get("heading") or "") + " " + (it.get("text") or ""), default)

    from pptx.util import Inches as _Inch

    def cover(s):
        sld = new(prs, "cover")
        for ph in sld.placeholders:
            if ph.placeholder_format.idx == 0:
                ph.text = spec.get("title") or "evoila"
            elif ph.placeholder_format.idx == 1:
                ph.text = "EVOILA  |  NORTH AMERICA"
        sub = spec.get("subtitle") or ""
        CW = 4.25                                 # clear of the cover artwork; stacked from the bottom up
        people = meta.get("people") or []
        lines_p = [[(f"{p['name']}, {p['title']}", {})] for p in people]
        ph_ = 0.27 * max(1, len(lines_p))
        py_ = 7.02 - ph_
        if lines_p:
            text(sld, 1.0, py_, CW, ph_, lines_p, size=12, color=SOFT)
        ml = [[(x, {"size": 14 if i == 0 else 12, "bold": i == 0, "color": GREEN if i == 0 else SOFT})]
              for i, x in enumerate([x for x in (meta.get("meeting_lines") or []) if x])]
        y = py_ - 0.3
        if ml:
            mh = sum(BL.para_h(m, CW, 12) for m in ml) + 0.05 * (len(ml) - 1) + 0.08
            y -= mh
            text(sld, 1.0, y, CW, mh, ml, size=13, color=GREEN, spacing=3)
            y -= 0.22
        if sub:
            sz = 15 if len(sub) <= 44 else 14
            sh_ = BL.para_h(sub, CW + 0.4, sz) + 0.05
            y -= sh_
            text(sld, 1.0, y, CW + 0.4, sh_, sub, size=sz, color=SOFT)
        ttl = spec.get("title") or "evoila"
        ts = 40
        while True:
            th = BL.n_lines(ttl, 5.35, ts) * ts * 0.95 / 72 + 0.08
            if y - 0.2 - th - 0.5 >= 1.75 or ts <= 26:
                break
            ts -= 2
        ty = y - 0.2 - th
        for ph in sld.placeholders:
            if ph.placeholder_format.idx == 0:
                ph.left, ph.top, ph.width, ph.height = _Inch(1.0), _Inch(ty), _Inch(5.35), _Inch(th)
                for r in ph.text_frame.paragraphs[0].runs:
                    r.font.size = Pt(ts)
            elif ph.placeholder_format.idx == 1:
                ph.left, ph.top, ph.width, ph.height = _Inch(1.0), _Inch(ty - 0.42), _Inch(4.4), _Inch(0.24)
        return sld

    # ---- measuring helpers: keep every line inside its box
    def tw(t, size, bold=True):
        """Width in inches of t on one line at this size (Inter metrics)."""
        f = BL._font(size, bold)
        return (f.getlength(str(t)) if f else len(str(t)) * size * 4 * 0.58) / (4 * 72)

    def one_line(t, w_in, hi, lo, bold=True):
        sz = hi
        while sz > lo and tw(t, sz, bold) > w_in * 0.96:
            sz -= 1
        return sz

    def fit(t, w_in, h_in, hi, lo, bold=False):
        return BL.fit_size(t, w_in, h_in, list(range(int(hi), int(lo) - 1, -1)), bold)

    def th(t, w_in, size, bold=False, lead=1.22):
        return BL.text_lines(t, w_in, size, bold) * size * lead / 72

    state = {"dark": False}

    def mark(sld, dark):
        state["dark"] = dark
        return sld

    def gbar(sld, x, y, w, h, vertical=False):
        return box(sld, x, y, w, h, grad=(GREEN, BLUE, 90 if vertical else 0), radius=0)

    def check(sld, x, y, d=0.3, dark=False):
        icon_disc(sld, "CircleCheck", x, y, d, disc=GREEN if not dark else GREEN, color="v")

    # ---- statement: split layout, big message on white, support line on a violet panel
    def statement(s):
        st = s.get("statement") or s.get("title") or ""
        sup = s.get("support") or ""
        ttl = (s.get("title") or "").strip()
        if ttl and not s.get("kicker") and ttl.strip(" .").lower() not in st.lower() and len(ttl) <= 40:
            s = dict(s, kicker=ttl)               # a separate short title reads as the kicker
        sld = new(prs, "light", "")
        for shp in list(sld.placeholders):             # no title bar on a statement slide
            if shp.placeholder_format.idx == 0:
                shp._element.getparent().remove(shp._element)
        PX = 8.55
        box(sld, PX, 0, 13.333 - PX, 6.62, fill=VIOLET, radius=0)
        box(sld, PX, 0, 0.09, 6.62, grad=(GREEN, BLUE, 90), radius=0)
        for k, (d, a) in enumerate([(3.6, 10), (2.5, 14), (1.4, 22)]):   # rings, partly off the edge
            box(sld, 13.333 - d * 0.62, 0.35 - d * 0.18, d, d, line="0BD7A1@%d" % a, lw=1.5, shape=MSO_SHAPE.OVAL)
        icon(sld, "Sparkles", PX + 0.7, 1.15, 0.62, "g")
        if sup:
            ssz = fit(sup, 3.6, 2.6, 24, 16, True)
            text(sld, PX + 0.7, 2.05, 3.65, 3.0, sup, size=ssz, bold=True, color=WHITE, line_spacing=1.08)
        else:
            text(sld, PX + 0.7, 2.05, 3.65, 1.0, "evoila  |  North America", size=16, color=SOFT)
        y = 1.55
        gbar(sld, X0, y + 0.05, 0.12, 4.1, vertical=True)
        if s.get("kicker"):
            text(sld, X0 + 0.45, y, 7.0, 0.4, [[(s["kicker"].upper(), {"caps_spacing": 300})]], size=14, bold=True, color=BLUE)
            y += 0.6
        ssz = fit(st, 6.9, 3.6, 46, 28, True)
        text(sld, X0 + 0.45, y, 6.9, 3.8, st, size=ssz, bold=True, color=VIOLET, line_spacing=1.0)
        return mark(sld, False)

    # ---- bullets: list for up to 3, a numbered 2-column grid for 4 to 6
    def bullets(s):
        sld = new(prs, "light", s.get("title") or "")
        y0 = 1.5
        if s.get("support"):
            text(sld, X0, y0, W, 0.5, s["support"], size=15, color=MUTED)
            y0 += 0.6
        its = items(s, hi=6) or [{"text": ""}]
        n = len(its)
        if n >= 4:
            rows = (n + 1) // 2; g = 0.22; cw = (W - g) / 2
            ch = min(1.45, (6.55 - y0 - g * (rows - 1)) / rows)
            longest = max(((it.get("text") or "") for it in its), key=len)
            tsz = fit(longest, cw - 1.75, ch - 0.55, 15, 11)
            for i, it in enumerate(its):
                x = X0 + (i % 2) * (cw + g); y = y0 + (i // 2) * (ch + g)
                box(sld, x, y, cw, ch, fill=CREAM, radius=0.1)
                box(sld, x, y + 0.18, 0.08, ch - 0.36, fill=ACC[i % 2], radius=0)
                text(sld, x + 0.3, y, 1.0, ch, f"{i + 1:02d}", size=30, bold=True, color=ACC[i % 2] if i % 2 else BLUE,
                     anchor=MSO_ANCHOR.MIDDLE)
                hd = (it.get("heading") or "").rstrip(":")
                body = it.get("text") or ""
                paras = ([[(hd, {"bold": True, "size": 16, "color": VIOLET})]] if hd else []) + [[(body, {"color": MUTED})]]
                text(sld, x + 1.35, y, cw - 1.6, ch, paras, size=tsz, anchor=MSO_ANCHOR.MIDDLE, spacing=3)
        else:
            gap = min(1.35, (6.55 - y0) / n)
            for i, it in enumerate(its):
                y = y0 + i * gap
                box(sld, X0, y, W, gap - 0.16, fill=CREAM, radius=0.14)
                icon_disc(sld, ic(it), X0 + 0.25, y + (gap - 0.16) / 2 - 0.32, 0.64, disc=ACC[i % 2], color="v")
                runs = []
                if it.get("heading"):
                    runs.append((it["heading"].rstrip(":") + "   ", {"bold": True}))
                runs.append((it.get("text") or "", {}))
                text(sld, X0 + 1.2, y, W - 1.5, gap - 0.16, [runs], size=fit(it.get("text") or "", W - 3, gap - 0.3, 20, 14),
                     color=VIOLET, anchor=MSO_ANCHOR.MIDDLE)
        return mark(sld, False)

    # ---- columns: violet header block with a number and icon, checked points underneath
    def columns(s):
        sld = new(prs, "light", s.get("title") or "")
        its = items(s, hi=3) or [{}]
        n = len(its); g = 0.3; w = (W - g * (n - 1)) / n
        top, bot = 1.55, 6.5
        hh = 1.55
        allpts = [p for it in its for p in (it.get("points") or ([it["text"]] if it.get("text") else []))]
        psz = fit(max(allpts, key=len) if allpts else "", w - 1.0, 0.9, 18, 12)
        hsz_all = min(fit(it.get("heading") or "", w - 0.6, 0.66, 20, 14, True) for it in its)
        for i, it in enumerate(its):
            x = X0 + i * (w + g)
            box(sld, x, top, w, bot - top, fill=CREAM, radius=0.06)
            hb = box(sld, x, top, w, hh, fill=VIOLET, radius=0.06)
            box(sld, x, top + hh - 0.2, w, 0.2, fill=VIOLET, radius=0)          # square off the header's bottom
            box(sld, x, top + hh, w, 0.07, grad=(ACC[i % len(ACC)], BLUE, 0), radius=0)
            text(sld, x + 0.3, top + 0.2, 1.2, 0.6, f"{i + 1:02d}", size=26, bold=True, color=GREEN)
            icon(sld, ic(it), x + w - 0.85, top + 0.25, 0.52, "g")
            hd = it.get("heading") or ""
            text(sld, x + 0.3, top + 0.72, w - 0.6, 0.75, hd, size=hsz_all, bold=True, color=WHITE)
            pts = it.get("points") or ([it["text"]] if it.get("text") else [])
            y = top + hh + 0.4
            step = min(1.15, (bot - y - 0.1) / max(1, len(pts[:4])))
            for p in pts[:4]:
                check(sld, x + 0.3, y + 0.02, 0.32)
                text(sld, x + 0.78, y - 0.02, w - 1.0, step, p, size=psz, color=VIOLET)
                y += step
        return mark(sld, False)

    # ---- cards: white cards with a gradient top edge and a big icon
    def cards(s):
        sld = new(prs, "light", s.get("title") or "")
        its = items(s, hi=4) or [{}]
        n = len(its); g = 0.28; w = (W - g * (n - 1)) / n
        top, h = 1.6, 4.85
        box(sld, 0, top + 1.25, 13.333, h - 1.25 + 0.35, fill=CREAM, radius=0)        # band behind the cards
        hsz = min(fit(it.get("heading") or "", w - 0.7, 0.85, 19, 14, True) for it in its)
        tsz = min(fit(it.get("text") or "", w - 0.7, 1.9, 15, 11) for it in its)
        for i, it in enumerate(its):
            x = X0 + i * (w + g)
            box(sld, x, top, w, h, fill=WHITE, line="1F1534@14", lw=0.75, radius=0.06)
            box(sld, x, top, w, 0.1, grad=(GREEN, BLUE, 0), radius=0)
            icon_disc(sld, ic(it), x + 0.35, top + 0.45, 1.0, grad=(GREEN, BLUE, 45), color="w")
            text(sld, x + w - 0.95, top + 0.5, 0.65, 0.5, f"{i + 1:02d}", size=14, bold=True, color="1F1534@30",
                 align=PP_ALIGN.RIGHT)
            text(sld, x + 0.35, top + 1.7, w - 0.7, 0.9, it.get("heading") or "", size=hsz, bold=True, color=VIOLET)
            line(sld, x + 0.35, top + 2.65, x + 1.15, top + 2.65, color=GREEN, width=2)
            text(sld, x + 0.35, top + 2.85, w - 0.7, 1.9, it.get("text") or "", size=tsz, color=MUTED)
        return mark(sld, False)

    # ---- numbers: one-line figures sized to fit, a meter for shares, alternating dark and light
    def share(fig):
        f = str(fig).replace(" ", "").lower()
        m = re.match(r"^~?(\d+(?:\.\d+)?)%\+?$", f)
        if m:
            return min(1.0, float(m.group(1)) / 100)
        m = re.match(r"^(\d+)(?:of|in|outof)(\d+)$", f)
        if m and float(m.group(2)) > 0:
            return min(1.0, float(m.group(1)) / float(m.group(2)))
        return None

    def numbers(s):
        dark = not state["dark"]
        sld = new(prs, "dark" if dark else "light", s.get("title") or "")
        its = items(s, hi=4) or [{}]
        n = len(its); g = 0.3; w = (W - g * (n - 1)) / n
        top, h = 1.65, 4.25
        figs = [display_figure(it.get("figure") or "[figure]") for it in its]
        fsz = min(one_line(f, w - 0.6, 72 if n <= 3 else 54, 28) for f in figs)   # same size across the row
        fh = fsz * 1.05 / 72
        labels = [display_label(it.get("heading") or it.get("text") or "") for it in its]
        lsz = min(fit(l, w - 0.6, 1.6, 20 if n <= 3 else 17, 12) for l in labels)
        lh = max(th(l, w - 0.6, lsz) for l in labels)
        h = min(4.4, 0.45 + fh + 0.3 + 0.42 + lh + 0.5)
        top = 1.55 + max(0, (4.4 - h) / 2) * 0.6
        card_fill = VIOLET2 if dark else CREAM
        fig_col = GREEN if dark else BLUE
        lab_col = WHITE if dark else VIOLET
        for i, (it, fig, lab) in enumerate(zip(its, figs, labels)):
            x = X0 + i * (w + g)
            box(sld, x, top, w, h, fill=card_fill, line="FFFFFF@16" if dark else None, lw=0.75, radius=0.07)
            box(sld, x, top, w, 0.08, grad=(GREEN, BLUE, 0), radius=0)
            y = top + 0.45
            text(sld, x + 0.3, y, w - 0.6, fh + 0.1, fig, size=fsz, bold=True, color=fig_col)
            y += fh + 0.3
            sh = share(fig)
            if sh is not None:
                box(sld, x + 0.3, y, w - 0.6, 0.14, fill="FFFFFF@18" if dark else "1F1534@12", radius=0.5)
                if sh > 0.01:
                    box(sld, x + 0.3, y, max(0.16, (w - 0.6) * sh), 0.14, grad=(GREEN, BLUE, 0), radius=0.5)
            else:
                icon(sld, BL.pick_icon(lab, "TrendingUp"), x + 0.3, y - 0.12, 0.42, "g" if dark else "v")
            y += 0.42
            text(sld, x + 0.3, y, w - 0.6, top + h - y - 0.2, lab, size=lsz, color=lab_col)
        if s.get("source"):
            text(sld, X0, top + h + 0.25, W, 0.4, [[("SOURCE   ", {"bold": True, "color": GREEN if dark else BLUE, "size": 10}),
                                                  (s["source"], {})]], size=11, color=SOFT if dark else MUTED)
        return mark(sld, dark)

    # ---- steps: a chevron band in brand colours, detail cards underneath
    def steps(s):
        sld = new(prs, "light", s.get("title") or "")
        its = items(s, hi=5) or [{}]
        n = len(its); ov = 0.22
        cw = (W + ov * (n - 1)) / n
        cols = [GREEN, "06B8BE", LBLUE, BLUE, INDIGO][:n] if n == 5 else [GREEN, LBLUE, BLUE, INDIGO][:n]
        hsz = min(one_line(it.get("heading") or "", cw - 0.9, 18, 12) for it in its)
        tsz = min(fit(it.get("text") or "", cw - ov - 0.56, 1.9, 17, 11) for it in its)
        bh = min(3.4, max(th(it.get("text") or "", cw - ov - 0.56, tsz) for it in its) + 0.75)
        for i, it in enumerate(its):
            x = X0 + i * (cw - ov)
            shp = box(sld, x, 1.7, cw, 1.05, fill=cols[i], shape=MSO_SHAPE.PENTAGON if i == 0 else MSO_SHAPE.CHEVRON, radius=0)
            try:
                shp.adjustments[0] = 0.32
            except Exception:
                pass
            dark_txt = cols[i] in (GREEN,)
            text_in(shp, [[(f"{i + 1:02d}  ", {"size": hsz - 2, "color": VIOLET if dark_txt else "FFFFFF@70"}),
                           (it.get("heading") or "", {"size": hsz, "bold": True})]],
                    color=VIOLET if dark_txt else WHITE, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE, margin=0.05)
            bx = X0 + i * (cw - ov) + (0.12 if i else 0)
            bw = cw - ov - 0.12
            box(sld, bx, 3.05, bw, bh, fill=CREAM, radius=0.06)
            box(sld, bx, 3.05, bw, 0.07, fill=cols[i], radius=0)
            text(sld, bx + 0.22, 3.35, bw - 0.44, bh - 0.4, it.get("text") or "", size=tsz, color=VIOLET)
        return mark(sld, False)

    # ---- comparison: "today" and "after" panels with an arrow between (2 columns); a grid for 3
    def comparison(s):
        heads = (s.get("headers") or [])[:3]
        rows = [r for r in (s.get("rows") or []) if isinstance(r, dict)][:5]
        if len(heads) != 2:
            return table(dict(s, headers=[""] + heads))
        sld = new(prs, "light", s.get("title") or "")
        gap = 0.9; pw = (W - gap) / 2
        top, bot = 1.5, 6.55
        hh = 0.75
        nr = max(1, len(rows))
        rh = (bot - top - hh - 0.15) / nr
        cells = [c for r in rows for c in (r.get("cells") or [])[:2]]
        csz = min([fit(c, pw - 1.2, rh - 0.42, 16, 11) for c in cells] or [14])
        lx = X0; rx = X0 + pw + gap
        box(sld, lx, top, pw, bot - top, fill=CREAM, radius=0.05)
        box(sld, rx, top, pw, bot - top, fill=VIOLET, radius=0.05)
        box(sld, rx, top, pw, 0.09, grad=(GREEN, BLUE, 0), radius=0)
        text(sld, lx + 0.35, top + 0.15, pw - 0.7, hh - 0.15, heads[0].upper(), size=15, bold=True, color="1F1534@55",
             anchor=MSO_ANCHOR.MIDDLE)
        text(sld, rx + 0.35, top + 0.15, pw - 0.7, hh - 0.15, heads[1].upper(), size=15, bold=True, color=GREEN,
             anchor=MSO_ANCHOR.MIDDLE)
        d = box(sld, X0 + pw + gap / 2 - 0.38, (top + bot) / 2 - 0.38, 0.76, 0.76, grad=(GREEN, BLUE, 45), shape=MSO_SHAPE.OVAL)
        icon(sld, "ArrowRight", X0 + pw + gap / 2 - 0.22, (top + bot) / 2 - 0.22, 0.44, "w")
        for i, r in enumerate(rows):
            y = top + hh + 0.1 + i * rh
            cl = (r.get("cells") or ["", ""]) + ["", ""]
            lab = (r.get("label") or "").upper()
            if i:
                line(sld, lx + 0.35, y, lx + pw - 0.35, y, color="1F1534@14", width=0.75)
                line(sld, rx + 0.35, y, rx + pw - 0.35, y, color="FFFFFF@16", width=0.75)
            text(sld, lx + 0.35, y + 0.1, pw - 0.7, 0.3, lab, size=10, bold=True, color="1F1534@50")
            text(sld, rx + 0.35, y + 0.1, pw - 0.7, 0.3, lab, size=10, bold=True, color=GREEN)
            box(sld, lx + 0.38, y + 0.5, 0.22, 0.22, fill="1F1534@22", shape=MSO_SHAPE.OVAL)
            text(sld, lx + 0.85, y + 0.38, pw - 1.2, rh - 0.42, cl[0], size=csz, color=MUTED)
            check(sld, rx + 0.35, y + 0.44, 0.3)
            text(sld, rx + 0.85, y + 0.38, pw - 1.2, rh - 0.42, cl[1], size=csz, bold=True, color=WHITE)
        return mark(sld, False)

    # ---- quote: big mark, white panel feel without the cover artwork
    def quote(s):
        sld = new(prs, "dark", "")
        for shp in list(sld.placeholders):
            if shp.placeholder_format.idx == 0:
                shp._element.getparent().remove(shp._element)
        gbar(sld, X0, 1.5, 0.12, 4.4, vertical=True)
        icon(sld, "Quote", X0 + 0.5, 1.45, 1.0, "g")
        q = s.get("statement") or ""
        text(sld, X0 + 0.5, 2.7, 10.5, 2.6, q, size=fit(q, 10.5, 2.6, 34, 22, True), bold=True, color=WHITE)
        if s.get("support"):
            text(sld, X0 + 0.5, 5.45, 10, 0.5, s["support"], size=16, color=GREEN)
        return mark(sld, True)

    def agenda(s):
        sld = new(prs, "light", s.get("title") or "Agenda")
        its = items(s, hi=7)
        h = min(0.78, 4.9 / max(1, len(its)))
        for i, it in enumerate(its):
            y = 1.55 + i * (h + 0.1)
            box(sld, X0, y, W, h, fill=CREAM, radius=0.18)
            d = box(sld, X0 + 0.18, y + h / 2 - 0.27, 0.54, 0.54, grad=(GREEN, BLUE, 45), shape=MSO_SHAPE.OVAL)
            text_in(d, str(i + 1), size=15, bold=True, color=WHITE, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE, margin=0)
            text(sld, X0 + 1.0, y, W - 1.3, h, it.get("heading") or it.get("text") or "", size=18, bold=True,
                 color=VIOLET, anchor=MSO_ANCHOR.MIDDLE)
        return mark(sld, False)

    def closing(s):
        sld = new(prs, "dark", s.get("title") or "Next steps")
        its = items(s, hi=3)
        gap = 4.5 / max(3, len(its) or 1)
        for i, it in enumerate(its):
            y = 1.55 + i * gap
            d = box(sld, X0, y, 0.9, 0.9, grad=(GREEN, BLUE, 45), shape=MSO_SHAPE.OVAL)
            text_in(d, str(i + 1), size=26, bold=True, color=WHITE, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE, margin=0)
            if i < len(its) - 1:
                line(sld, X0 + 0.45, y + 0.95, X0 + 0.45, y + gap - 0.05, color="FFFFFF@40", width=1.5, dash=DASH.DASH)
            text(sld, X0 + 1.17, y, 4.8, 0.9, it.get("heading") or it.get("text") or "", size=17, bold=True, color=WHITE,
                 anchor=MSO_ANCHOR.MIDDLE)
        for i, c in enumerate((meta.get("people") or [])[:2]):
            y = 1.55 + i * 2.05
            box(sld, 6.95, y, 5.7, 1.8, fill=VIOLET2, alpha=92, line="FFFFFF@20", lw=0.75, radius=0.08)
            box(sld, 7.2, y + 0.3, 1.2, 1.2, fill="FFFFFF@12", shape=MSO_SHAPE.OVAL)
            icon(sld, "UserCheck", 7.5, y + 0.6, 0.6, "g")
            text(sld, 8.65, y + 0.3, 3.8, 0.45, c["name"], size=16, bold=True, color=WHITE)
            text(sld, 8.65, y + 0.78, 3.8, 0.35, c["title"], size=13, color=SOFT)
            text(sld, 8.65, y + 1.15, 3.8, 0.35, c.get("email") or "", size=13, color=GREEN)
        pill(sld, 6.95, 5.85, 2.6, 0.5, "evoila.com/us", fill=GREEN, color=VIOLET, size=14)
        return sld

    # ---- layouts the deck converter uses (convert_deck.py); the outline builder can use them too
    from pptx.util import Inches as _In

    def table(s):
        sld = new(prs, "light", s.get("title") or "")
        heads = [str(h) for h in (s.get("headers") or [])][:6]
        rows = [r for r in (s.get("rows") or []) if isinstance(r, dict)][:8]
        nc = max([len(heads)] + [1 + len(r.get("cells") or []) for r in rows])
        nc = max(2, min(6, nc))
        heads = (heads + [""] * nc)[:nc]
        labelled = any((r.get("label") or "").strip() for r in rows)
        g = 0.08
        lw = min(3.0, W / nc * 1.15) if labelled else (W - g * (nc - 1)) / nc
        cw = (W - lw - g * (nc - 1)) / max(1, nc - 1)
        widths = [lw] + [cw] * (nc - 1)
        xs = [X0]
        for w_ in widths[:-1]:
            xs.append(xs[-1] + w_ + g)
        top = 1.55
        hh = 0.62 if any(heads) else 0
        rh = min(0.82, (6.55 - top - hh - g) / max(1, len(rows)) - g)
        longest = max([len(str(c)) for r in rows for c in ([r.get("label") or ""] + list(r.get("cells") or []))] + [8])
        fs = _fit("x" * longest, 28, 15, 10.5)
        if hh:
            for j, h in enumerate(heads):
                if not h and j == 0 and labelled:
                    continue
                b = box(sld, xs[j], top, widths[j], hh, fill=INDIGO, radius=0.1)
                text_in(b, h, size=_fit(h, 22, 14, 11), bold=True, color=WHITE, align=PP_ALIGN.CENTER,
                        anchor=MSO_ANCHOR.MIDDLE, margin=0.08)
        for i, r in enumerate(rows):
            y = top + hh + g + i * (rh + g)
            cells = [r.get("label") or ""] + list(r.get("cells") or []) if labelled else list(r.get("cells") or [])
            cells = (cells + [""] * nc)[:nc]
            for j, c in enumerate(cells):
                first = labelled and j == 0
                b = box(sld, xs[j], y, widths[j], rh, fill=VIOLET if first else CREAM, radius=0.06)
                text_in(b, str(c), size=fs, bold=first, color=WHITE if first else VIOLET, anchor=MSO_ANCHOR.MIDDLE,
                        align=PP_ALIGN.LEFT if first else PP_ALIGN.CENTER, margin=0.1)
        if s.get("source"):
            text(sld, X0, 6.62, W, 0.3, "Source: " + s["source"], size=10.5, color=MUTED)
        return sld

    def chart(s):
        from pptx.chart.data import CategoryChartData
        from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
        ch = s.get("chart") or {}
        cats = [str(c) for c in ch.get("categories") or []]
        series = [x for x in ch.get("series") or [] if x.get("values")][:6]
        if not cats or not series:
            return bullets(dict(s, items=[{"text": "[chart data]"}]))
        kind = s.get("chart_kind") or ""
        orig = (ch.get("type") or "").upper()
        if not kind:
            kind = "line" if "LINE" in orig else "pie" if ("PIE" in orig or "DOUGHNUT" in orig) else "bar" if orig.startswith("BAR") else "column"
        dark = False
        sld = new(prs, "light", s.get("title") or "")
        cd = CategoryChartData()
        cd.categories = cats
        for x in (series[:1] if kind == "pie" else series):
            cd.add_series(x.get("name") or "", [float(v or 0) for v in x["values"][:len(cats)]])
        xt = {"column": XL_CHART_TYPE.COLUMN_CLUSTERED, "bar": XL_CHART_TYPE.BAR_CLUSTERED,
              "line": XL_CHART_TYPE.LINE_MARKERS, "pie": XL_CHART_TYPE.DOUGHNUT}[kind]
        side = bool(s.get("support"))
        cw_ = 8.0 if side else W
        gf = sld.shapes.add_chart(xt, _In(X0), _In(1.5), _In(cw_), _In(4.95), cd)
        c = gf.chart
        c.font.name = "Inter"
        c.font.size = Pt(12)
        c.font.color.rgb = K.rgb(VIOLET)
        pal = [BLUE, GREEN, INDIGO, LBLUE, "8C84A0", "1F1534"]
        multi = len(series) > 1 and kind != "pie"
        c.has_legend = multi or kind == "pie"
        if c.has_legend:
            c.legend.position = XL_LEGEND_POSITION.BOTTOM if kind != "pie" else XL_LEGEND_POSITION.RIGHT
            c.legend.include_in_layout = False
            c.legend.font.size = Pt(12)
        plot = c.plots[0]
        if kind == "pie":
            for k, pt in enumerate(plot.series[0].points):
                pt.format.fill.solid(); pt.format.fill.fore_color.rgb = K.rgb(pal[k % len(pal)])
            plot.has_data_labels = True
            plot.data_labels.font.size = Pt(12); plot.data_labels.font.color.rgb = K.rgb(WHITE)
        else:
            for k, se in enumerate(plot.series):
                col = K.rgb(pal[k % len(pal)])
                if kind == "line":
                    se.format.line.color.rgb = col; se.format.line.width = Pt(3)
                    se.smooth = False
                    try:
                        se.marker.format.fill.solid(); se.marker.format.fill.fore_color.rgb = col; se.marker.format.line.color.rgb = col
                    except Exception:
                        pass
                else:
                    se.format.fill.solid(); se.format.fill.fore_color.rgb = col
            if kind in ("column", "bar"):
                try:
                    plot.gap_width = 60; plot.overlap = -10 if multi else 0
                except Exception:
                    pass
            if len(cats) * len(series) <= 12:
                plot.has_data_labels = True
                dl = plot.data_labels
                dl.font.size = Pt(11); dl.font.color.rgb = K.rgb(VIOLET)
                try:
                    dl.number_format = '#,##0.##'; dl.number_format_is_linked = False
                except Exception:
                    pass
            va = c.value_axis
            va.has_major_gridlines = True
            va.major_gridlines.format.line.color.rgb = K.rgb("E4DDD6")
            va.format.line.fill.background()
            va.tick_labels.font.size = Pt(11); va.tick_labels.font.color.rgb = K.rgb(MUTED)
            ca = c.category_axis
            ca.format.line.color.rgb = K.rgb("CFC6BD")
            ca.tick_labels.font.size = Pt(12)
        if side:
            b = box(sld, X0 + cw_ + 0.35, 1.5, W - cw_ - 0.35, 4.95, fill=VIOLET, radius=0.08)
            text(sld, X0 + cw_ + 0.65, 1.85, W - cw_ - 0.95, 0.4, "WHAT IT SHOWS", size=12, bold=True, color=GREEN)
            text(sld, X0 + cw_ + 0.65, 2.35, W - cw_ - 0.95, 3.8, s["support"], size=_fit(s["support"], 80, 21, 16), bold=True, color=WHITE)
        if s.get("source"):
            text(sld, X0, 6.55, W, 0.3, "Source: " + s["source"], size=10.5, color=MUTED)
        return sld

    def image(s):
        from PIL import Image as _Im
        sld = new(prs, "light", s.get("title") or "")
        pth = s.get("image_path")
        its = items(s, hi=4)
        cap = s.get("caption") or ""
        if its:
            fx, fy, fw, fh = X0, 1.5, 7.55, 5.0
        else:
            fx, fy, fw, fh = X0, 1.5, W, 4.75 if cap else 5.05
        frame = box(sld, fx, fy, fw, fh, fill=CREAM, radius=0.06)
        if pth and os.path.exists(pth):
            iw, ih = _Im.open(pth).size
            pad = 0.22
            aw, ah = fw - 2 * pad, fh - 2 * pad
            sc_ = min(aw / iw, ah / ih)
            w_, h_ = iw * sc_, ih * sc_
            pic = sld.shapes.add_picture(pth, _In(fx + (fw - w_) / 2), _In(fy + (fh - h_) / 2), _In(w_), _In(h_))
            pic.line.color.rgb = K.rgb("E4DDD6"); pic.line.width = Pt(0.75)
        if cap and not its:
            text(sld, X0, fy + fh + 0.12, W, 0.4, cap, size=13, color=MUTED)
        if its:
            tx = fx + fw + 0.35; tw = X0 + W - tx
            if cap:
                text(sld, tx, 1.5, tw, 0.9, cap, size=_fit(cap, 70, 17, 14), bold=True, color=VIOLET)
            y = 1.5 + (1.05 if cap else 0)
            gap = min(1.15, (6.5 - y) / max(1, len(its)))
            for i, it in enumerate(its):
                box(sld, tx, y + i * gap + 0.12, 0.14, 0.14, fill=ACC[i % 2], shape=MSO_SHAPE.OVAL)
                runs = []
                if it.get("heading"):
                    runs.append((it["heading"].rstrip(":") + "  ", {"bold": True}))
                runs.append((it.get("text") or "", {}))
                text(sld, tx + 0.35, y + i * gap, tw - 0.35, gap - 0.08, [runs], size=_fit(it.get("text") or "", 70, 15, 12), color=VIOLET)
        return sld

    def hub(s):
        sld = new(prs, "light", s.get("title") or "")
        its = items(s, hi=6) or [{}]
        cx, cy, rx, ry = 6.67, 4.05, 4.25, 1.85
        n = len(its)
        import math as _m
        pos = []
        for i in range(n):
            a = -_m.pi / 2 + 2 * _m.pi * i / n
            pos.append((cx + rx * _m.cos(a), cy + ry * _m.sin(a)))
        for (px, py) in pos:
            line(sld, cx, cy, px, py, color="0075E4@55", width=1.25, dash=DASH.DASH)
        d = box(sld, cx - 1.05, cy - 1.05, 2.1, 2.1, grad=(GREEN, BLUE, 45), shape=MSO_SHAPE.OVAL)
        hc = s.get("hub_center") or ""
        text_in(d, hc, size=_fit(hc, 16, 18, 13), bold=True, color=WHITE, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE, margin=0.15)
        bw, bh = 2.75, 0.95
        for i, ((px, py), it) in enumerate(zip(pos, its)):
            bx, by = min(max(X0, px - bw / 2), X0 + W - bw), min(max(1.45, py - bh / 2), 6.55 - bh)
            box(sld, bx, by, bw, bh, fill=WHITE, line=VIOLET, lw=1.0, radius=0.14)
            col = ACC[i % len(ACC)]
            icon_disc(sld, ic(it), bx + 0.14, by + 0.17, 0.6, disc=col, color="v" if col in (GREEN, LBLUE) else "w")
            hd = it.get("heading") or it.get("text") or ""
            text(sld, bx + 0.86, by, bw - 0.95, bh, hd, size=_fit(hd, 20, 14, 11), bold=True, color=VIOLET, anchor=MSO_ANCHOR.MIDDLE)
        return sld

    draw = dict(cover=cover, agenda=agenda, statement=statement, bullets=bullets, columns=columns, cards=cards,
                numbers=numbers, steps=steps, comparison=comparison, quote=quote, closing=closing,
                table=table, chart=chart, image=image, hub=hub)
    slides = [s for s in spec.get("slides") or [] if isinstance(s, dict)]
    if not slides or slides[0].get("layout") != "cover":
        slides = [{"layout": "cover", "notes": ""}] + slides
    for s in slides:
        lay = s.get("layout")
        if lay == "numbers":
            s = dict(s, items=[dict(it, **dict(zip(("figure", "heading"), normalize_figure(it.get("figure") or "", it.get("heading") or it.get("text") or ""))))
                               for it in (s.get("items") or []) if isinstance(it, dict)])
        sld = draw.get(lay, bullets)(s)
        if lay in ("cover", "closing"):
            state["dark"] = True
        elif lay in ("table", "chart", "image", "hub"):
            state["dark"] = False
        if s.get("notes"):
            K.notes(sld, s["notes"])
    BL.set_sections(prs, [("Deck", [e.get("id") for e in prs.slides._sldIdLst])])
    prs.save(out)
    return len(slides)


# ---------------------------------------------------------------- build flow

SOURCE_KINDS = {
    "Analyst firms": ("gartner.com idc.com forrester.com mckinsey.com bcg.com bain.com deloitte.com pwc.com kpmg.com "
                      "ey.com accenture.com").split(),
    "News and trade press": ("hipaajournal.com healthcareitnews.com fiercehealthcare.com beckershospitalreview.com "
                             "modernhealthcare.com statnews.com healthcaredive.com cybersecuritydive.com ciodive.com "
                             "bankingdive.com utilitydive.com reuters.com apnews.com bloomberg.com wsj.com ft.com nytimes.com "
                             "washingtonpost.com cnbc.com axios.com forbes.com fortune.com darkreading.com securityweek.com "
                             "theregister.com bleepingcomputer.com scmagazine.com infosecurity-magazine.com techtarget.com "
                             "computerweekly.com computerworld.com networkworld.com cio.com csoonline.com infoworld.com zdnet.com "
                             "virtualizationreview.com redmondmag.com rcpmag.com storagereview.com itpro.com techzine.eu "
                             "datacenterknowledge.com datacenterdynamics.com sdxcentral.com siliconangle.com techrepublic.com "
                             "informationweek.com venturebeat.com blocksandfiles.com networkcomputing.com channelfutures.com "
                             "crn.com govtech.com statescoop.com nextgov.com fedscoop.com route-fifty.com govexec.com "
                             "americanbanker.com").split(),
}


def _kind(url):
    import urllib.parse as up
    h = up.urlparse(url).netloc.lower().split(":")[0]
    h = h[4:] if h.startswith("www.") else h
    if re.search(r"\.(gov|mil|int)(\.[a-z]{2})?$", h):
        return "Government and regulators"
    for k, doms in SOURCE_KINDS.items():
        if any(h == d or h.endswith("." + d) for d in doms):
            return k
    return "Industry studies and associations"


def research_slide(entry, meta, report):
    """Research one outline entry and return a numbers slide (verified figures or placeholders)."""
    rules = entry.get("research") or {}
    try:
        months = max(3, min(36, int(rules.get("months") or 12)))
    except Exception:
        months = 12
    rules["months"] = months
    topic = str(entry.get("content") or "").strip()
    today = datetime.date.today()
    allowed = set(rules.get("sources") or [])
    used, seen, dropped = [], set(), []
    r = {"stats": []}
    for attempt in range(2):
        rules_now = dict(rules)
        if attempt:
            rules_now["rules"] = (str(rules.get("rules") or "") + " SECOND PASS: the first search found only figures the build cannot use. "
                                  "Find figures on pages from analyst firms, government bodies, or established news and trade press "
                                  "(for example Reuters, CIO Dive, CRN, The Register, TechTarget, Computerworld, Network World, "
                                  "Virtualization Review, StorageReview, ITPro, Data Center Dynamics, SiliconANGLE). A survey a vendor "
                                  "commissioned is fine when one of those outlets reports it; link that article, not the vendor's "
                                  "own press release. These were rejected: " + "; ".join(dropped[:8])).strip()
        try:
            r = ai_steps.make_topic_research(topic, rules_now, meta, today.isoformat())
        except Exception as e:
            r = {"stats": []}
            report.append(f"Research slide '{topic[:60]}': research failed ({str(e)[:150]}).")
        for st in (r.get("stats") or [])[:10]:
            label = f"{st.get('figure','')} {str(st.get('claim',''))[:70]} ({st.get('source','')})"
            if allowed and _kind(st.get("url", "")) not in allowed:
                report.append(f"DROPPED (source kind not allowed by the requester): {label} {st.get('url','')}")
                dropped.append(f"{st.get('url','')} (source kind not allowed)")
                continue
            key = (rr.compact_figure(st.get("figure", "")), str(st.get("source", "")).lower())
            if key in seen or len(used) >= 4:
                continue
            ok, why = rr.check_stat(st, today, max_days=int(months * 30.5))
            if not ok:
                report.append(f"DROPPED ({why}): {label} {st.get('url','')}")
                dropped.append(f"{st.get('url','')} ({why})")
                continue
            seen.add(key)
            used.append(st)
            report.append(f"USED (found on the page, {st.get('year','')}): {label} {st.get('url','')}")
        if len(used) >= 2:
            break
        if attempt == 0:
            report.append(f"Research slide '{topic[:60]}': fewer than two figures passed, searching again.")
    title = rr.nodash(r.get("title") or topic[:60] or "What the data shows")
    if len(used) >= 2:
        srcs = []
        for st in used:
            s_ = f"{st.get('source','')}, {st.get('year','')}".strip(", ")
            if s_ not in srcs:
                srcs.append(s_)
        notes = " ".join(x for x in [rr.nodash(r.get("talking_point") or ""), rr.nodash(r.get("takeaway") or "")] if x)
        notes += " Sources: " + "; ".join(f"{st.get('source','')} ({st.get('url','')})" for st in used)
        report.append(f"Research slide '{title}': included with {len(used)} confirmed figures.")
        return {"layout": "numbers", "title": title, "source": "; ".join(srcs)[:220],
                "items": [{"figure": st.get("figure", ""), "heading": st.get("claim", "")} for st in used], "notes": notes}
    report.append(f"Research slide '{title}': fewer than two figures passed the checks, placeholders left on the slide.")
    return {"layout": "numbers", "title": title, "source": "[source]",
            "items": [{"figure": "[figure]", "heading": "[what the figure shows]"} for _ in range(3)],
            "notes": "We could not confirm enough recent figures that met your rules for this slide, so it has placeholders. "
                     "The build report on the request lists what was found and why each figure was dropped."}


def run():
    os.makedirs(rr.WORK, exist_ok=True)
    f = rr.get_record(rr.T_REQ, rr.REC)
    try:
        outline = json.loads(f.get(F_OUTLINE) or "{}")
    except Exception:
        raise RuntimeError("The custom outline on this row is not valid JSON.")
    people = []
    for pid in f.get(rr.F["presenters"], [])[:2]:
        p = rr.get_record(rr.T_PRES, pid)
        people.append({"name": p.get(rr.P["name"], ""), "title": p.get(rr.P["title"], ""), "email": p.get(rr.P["email"], "")})
    prospect = (f.get(rr.F["prospect"]) or "").strip()
    when = ""
    if f.get(rr.F["date"]):
        d = datetime.date.fromisoformat(f[rr.F["date"]])
        when = f"{d.strftime('%B')} {d.day}, {d.year}"
    meta = {"people": people, "presenters": ", ".join(f"{p['name']} ({p['title']})" for p in people),
            "audience": f.get(rr.F["audience"]) or "", "company": prospect,
            "next_step": f.get(F_NEXT) or "", "notes": f.get(rr.F["notes"]) or "",
            "background": f.get(F_BACKGROUND) or "",
            "meeting_lines": [f"Prepared for {prospect}" if prospect else "", when]}
    note = ""
    meta["deck_title"] = outline.get("title") or ""
    entries = outline.get("slides") or []
    research_idx = [i for i, e in enumerate(entries) if e.get("layout") == "research"]
    report, researched = [], {}
    if research_idx:
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(max_workers=3) as ex:
            futs = {i: ex.submit(research_slide, entries[i], meta, report) for i in research_idx}
            for i, fu in futs.items():
                researched[i] = fu.result()
        # the copywriter sees a plain numbers slide it must leave alone; the researched slide replaces it afterwards
        outline = dict(outline, slides=[dict(e, layout="numbers", content="[Research slide, filled in by the build. Write a one-line title only.]")
                                        if i in researched else e for i, e in enumerate(entries)])
    try:
        spec = make_spec(outline, meta)
    except Exception as e:
        spec = fallback_spec(outline)
        note = f"Claude copywriting failed, slides use the outline text as written: {str(e)[:300]}"
    if researched:
        offset = len(spec.get("slides", [])) - len(entries)
        for i, sl in researched.items():
            j = i + max(offset, 0)
            if 0 <= j < len(spec.get("slides", [])):
                spec["slides"][j] = sl
        note = "\n".join(([note] if note else []) + report)
    rr.update({rr.F["plan"]: json.dumps(spec, indent=1)[:95000]})
    tpl = rr.fetch_assets()
    stamp = f.get(rr.F["date"]) or datetime.date.today().isoformat()
    base = f"evoila_{rr.safe(outline.get('title') or prospect or 'Deck')}_{stamp}"
    pptx = os.path.join(rr.WORK, base + ".pptx")
    n = render(spec, tpl, pptx, meta, rr.ENGINE)
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", rr.WORK, pptx],
                   check=True, timeout=240, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pdf = pptx[:-5] + ".pdf"
    rr.update({rr.F["pptx"]: [], rr.F["pdf"]: []})
    rr.upload(rr.F["pptx"], pptx, "application/vnd.openxmlformats-officedocument.presentationml.presentation")
    rr.upload(rr.F["pdf"], pdf, "application/pdf")
    rr.update({rr.F["status"]: "Ready", rr.F["log"]: rr.RUN_URL or None, rr.F["error"]: "",
               rr.F["libver"]: "custom outline", rr.F["rcheck"]: note or None})
    print("custom deck done:", n, "slides")
