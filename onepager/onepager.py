"""Builds one version of the evoila US one-pager. Run by .github/workflows/onepager.yml.

Fired by the Airtable automation "One-pager: build new version" when a One-pager Versions row is set to
Building (the hub creates the row). RECORD_ID is that row. What the row asks for decides the copy:

  Restore from = rec...   copy the words of that older version (one-click restore on the hub)
  Copy already filled     use it as is (seeding, or a hand-made fix in Airtable)
  otherwise               Claude applies the Request to the words of the Current version

Then every version goes through the same gate before it can go live:
  1. rules.json: banned terms (VCSP ...), no em dashes, evoila lowercase
  2. the page is rendered in Chrome and every fixed-size box is measured; text that no longer fits
     fails the build (Claude gets two tries to shorten it first)
If it passes: PDF and preview are attached, the row becomes vN and Current, the old Current becomes
Previous. If not: the row is set to Problems found with the reason, and the Current version is untouched.

Layout, colors and artwork never change here; they live in template.html. Only the words do.
Public repository: never print copy or request text to the log.
"""
import asyncio, base64, copy as cp, json, os, pathlib, re, sys, time, urllib.error, urllib.parse, urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import ai_steps  # noqa: E402

BASE = "appYVahdfC3gbtArV"
T = "tblayGlclXOopzo2o"
F = {"version": "fld12J8ryv2cRuncA", "number": "fldybNjNKEob6CsDl", "status": "fld819L8Q1yMYGy8L",
     "request": "fldI2rQGBXiUNtzbK", "by": "fld4VZ16l8LDCizUe", "restore": "fldxazqBXAIia5mpZ",
     "changes": "fldqrpNEfgI1dNxwr", "problems": "fldEJmUogYPJ8K9gS", "copy": "fldMeJWxYhk75FqoO",
     "pdf": "fld6mbJWsEXJrRuCI", "preview": "fldkUl4JU3ElviDva"}
RULES = json.loads((HERE / "rules.json").read_text())
WORK = HERE.parent / "work"

EDIT_PROMPT = """You edit the words on evoila US's one-page sales sheet (a US Letter PDF that sales hands to
Broadcom reps, partners and customers). The layout is fixed; you only change words. {who} asked for this change:

REQUEST:
{request}

Rules:
- Do exactly what the request asks, everywhere it applies on the page. Leave every other word as it is.
- Keep every claim at the scope the page already gives it. Never invent customers, results, numbers, awards,
  dates or quotes. If the request gives a fact or number, use it exactly.
- Never use these words or terms anywhere: {banned}. No em dashes (use a comma, colon or period).
  "evoila" is always lowercase. Wrap words in **double asterisks** only where the page already uses bold.
- Every box has a fixed size. Keep each changed text about as long as the text it replaces, or shorter.
- Keep the structure: the same keys, exactly 4 stats, 4 capabilities and 2 contacts. Lists may gain or lose
  an item only if the request asks for it.
- If part of the request breaks these rules or cannot be done with words alone (layout, colors, logos,
  badges, pictures), do the rest and list what you did not do under not_applied.

Call submit_onepager with the complete page (all keys, changed or not), a short list of what you changed,
and anything you did not apply.

CURRENT PAGE (JSON):
{copy}"""

SCHEMA = {"type": "object", "properties": {
    "page": {"type": "object", "description": "The complete page JSON, same keys as the current page."},
    "changes": {"type": "array", "items": {"type": "string"}, "description": "One short line per change."},
    "not_applied": {"type": "array", "items": {"type": "string"}}},
    "required": ["page", "changes"]}


# ------------------------------------------------------------------ Airtable
def api(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}", "Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"Airtable {e.code}: {e.read()[:300].decode('utf-8', 'ignore')}")


def url(rid=""):
    return f"https://api.airtable.com/v0/{BASE}/{T}" + (f"/{rid}" if rid else "")


def get(rid):
    return api("GET", url(rid) + "?returnFieldsByFieldId=true")


def all_rows():
    out, off = [], None
    while True:
        q = "?returnFieldsByFieldId=true&pageSize=100" + (f"&offset={off}" if off else "")
        r = api("GET", url() + q)
        out += r.get("records", [])
        off = r.get("offset")
        if not off:
            return out


def patch(rid, fields):
    api("PATCH", url(rid), {"fields": fields, "typecast": True})


def upload(rid, field, path, ctype):
    b64 = base64.b64encode(path.read_bytes()).decode()
    api("POST", f"https://content.airtable.com/v0/{BASE}/{rid}/{field}/uploadAttachment",
        {"contentType": ctype, "file": b64, "filename": path.name})


def sel(v):
    return (v or {}).get("name", "") if isinstance(v, dict) else (v or "")


# ------------------------------------------------------------------ checks
def strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for k, v in o.items():
            if not k.startswith("_"):
                yield from strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from strings(v)


def rule_problems(page):
    text = "\n".join(strings(page))
    bad = []
    for r in RULES["banned_terms"]:
        if re.search(re.escape(r["term"]), text, re.I):
            bad.append(f'"{r["term"]}" is on the page. {r["why"]}')
    for r in RULES["banned_characters"]:
        if r["char"] in text:
            bad.append(f'{r["why"]}')
    for r in RULES["case_rules"]:
        if re.search(r["pattern"], text):
            bad.append(r["why"])
    return bad


def shape_problems(page):
    need = {"headline": list, "intro": str, "callout": dict, "stats_label": str, "stats": list,
            "capabilities_title": str, "capabilities": list, "engagement_title": str, "engagement": list,
            "verticals_title": str, "verticals": list, "credentials": list, "facts": list, "contacts": list,
            "trust_title": str, "trust": list, "footer": str}
    bad = [f"{k} is missing or the wrong type" for k, t in need.items() if not isinstance(page.get(k), t)]
    if bad:
        return bad
    for k, n in (("stats", 4), ("capabilities", 4), ("contacts", 2)):
        if len(page[k]) != n:
            bad.append(f"{k} must have exactly {n} entries")
    if not all(isinstance(c, dict) and isinstance(c.get("items"), list) for c in page["capabilities"]):
        bad.append("every capability needs a title and an items list")
    return bad


# ------------------------------------------------------------------ render
async def _render(page, out_pdf, out_png):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        exe = os.environ.get("CHROME_PATH") or "/usr/bin/google-chrome"
        b = await (p.chromium.launch(executable_path=exe) if os.path.exists(exe) else p.chromium.launch())
        pg = await b.new_page(viewport={"width": 816, "height": 1056}, device_scale_factor=2)
        await pg.add_init_script("window.COPY=" + json.dumps(page, ensure_ascii=False))
        await pg.goto((HERE / "template.html").as_uri())
        await pg.evaluate("document.fonts.ready")
        await pg.wait_for_timeout(300)
        bad = await pg.evaluate("fitCheck()")
        if not bad:
            await pg.screenshot(path=str(out_png))
            await pg.pdf(path=str(out_pdf), prefer_css_page_size=True, print_background=True)
        await b.close()
        return bad


def render(page, out_pdf, out_png):
    return asyncio.run(_render(page, out_pdf, out_png))


# ------------------------------------------------------------------ Claude
def claude_edit(current, request, who, extra=None):
    banned = ", ".join(r["term"] for r in RULES["banned_terms"])
    prompt = EDIT_PROMPT.format(who=who or "Someone on the evoila team", request=request.strip(), banned=banned,
                                copy=json.dumps(current, ensure_ascii=False, indent=1))
    messages = [{"role": "user", "content": prompt}]
    if extra:
        messages += extra
    tools = [{"name": "submit_onepager", "description": "Submit the full revised page.", "input_schema": SCHEMA}]
    for _ in range(3):
        resp = ai_steps._call({"model": ai_steps.MODEL, "max_tokens": 12000, "tools": tools,
                               "tool_choice": {"type": "tool", "name": "submit_onepager"}, "messages": messages}, timeout=300)
        content = resp.get("content", [])
        spec = next((b.get("input") for b in content if b.get("type") == "tool_use"), None)
        if isinstance(spec, dict) and isinstance(spec.get("page"), dict):
            return spec, messages + [{"role": "assistant", "content": content}], \
                next(b["id"] for b in content if b.get("type") == "tool_use")
        print("retry, stop_reason", resp.get("stop_reason"))
    raise RuntimeError("Claude returned no page")


def tidy(page):
    """Fix the small things a rewrite can slip in, before the checks run."""
    def fix(s):
        s = s.replace(" — ", ", ").replace("—", ", ").replace("– ", ", ")
        return re.sub(r"\bEvoila\b", "evoila", s)
    def walk(o):
        if isinstance(o, str):
            return fix(o)
        if isinstance(o, list):
            return [walk(v) for v in o]
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        return o
    return walk(page)


# ------------------------------------------------------------------ main
def build(rid):
    row = get(rid)["fields"]
    request = (row.get(F["request"]) or "").strip()
    who = row.get(F["by"]) or ""
    rows = all_rows()
    current = next((r for r in rows if sel(r["fields"].get(F["status"])) == "Current" and r["id"] != rid), None)
    cur_page = json.loads(current["fields"][F["copy"]]) if current else json.loads((HERE / "copy.json").read_text())
    WORK.mkdir(exist_ok=True)
    pdf, png = WORK / "evoila_onepager_US.pdf", WORK / "preview.png"
    changes, skipped, problems = [], [], []

    if (row.get(F["restore"]) or "").strip():
        src = get(row[F["restore"]].strip())["fields"]
        page = json.loads(src[F["copy"]])
        changes = [f"Restored the words of {src.get(F['version']) or 'an earlier version'}."]
        problems = shape_problems(page) + rule_problems(page)
        if not problems:
            problems = [f'"{b}" no longer fits its box.' for b in render(page, pdf, png)]
    elif (row.get(F["copy"]) or "").strip():
        page = json.loads(row[F["copy"]])
        changes = [request or "Words set by hand in Airtable."]
        problems = shape_problems(page) + rule_problems(page)
        if not problems:
            problems = [f'"{b}" no longer fits its box.' for b in render(page, pdf, png)]
    else:
        if not request:
            raise RuntimeError("The row has no request, no copy and nothing to restore.")
        spec, msgs, tool_id = claude_edit(cur_page, request, who)
        for attempt in range(3):
            page = tidy(spec["page"])
            problems = shape_problems(page) + rule_problems(page)
            if not problems:
                over = render(page, pdf, png)
                problems = [f'"{b}" no longer fits its box.' for b in over]
            if not problems or attempt == 2:
                break
            print(f"attempt {attempt + 1}: {len(problems)} problems, asking Claude to fix")
            note = ("Your page has these problems. Fix only these, keep everything else as you had it, and call "
                    "submit_onepager again with the full page:\n- " + "\n- ".join(problems) +
                    "\nFor text that no longer fits, shorten those boxes: same meaning, fewer words.")
            spec, msgs, tool_id = claude_edit(cur_page, request, who, extra=msgs[1:] + [
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": note}]}])
        changes = [str(c).strip() for c in spec.get("changes") or [] if str(c).strip()]
        skipped = [str(c).strip() for c in spec.get("not_applied") or [] if str(c).strip()]

    if problems:
        patch(rid, {F["status"]: "Problems found", F["copy"]: json.dumps(page, ensure_ascii=False, indent=2),
                    F["problems"]: "This version was not published. The current version is still live.\n- " +
                    "\n- ".join(problems + ["Not applied: " + s for s in skipped])})
        print("problems found:", len(problems))
        return

    num = max([r["fields"].get(F["number"]) or 0 for r in rows if r["id"] != rid] + [0]) + 1
    version = f"v{num}"
    named = WORK / f"evoila_onepager_US_{version}.pdf"
    pdf.rename(named)
    patch(rid, {F["pdf"]: [], F["preview"]: []})
    upload(rid, F["pdf"], named, "application/pdf")
    upload(rid, F["preview"], png, "image/png")
    summary = "\n".join("- " + c for c in changes) or "- No text changes."
    if skipped:
        summary += "\nNot applied:\n" + "\n".join("- " + s for s in skipped)
    patch(rid, {F["copy"]: json.dumps(page, ensure_ascii=False, indent=2), F["changes"]: summary,
                F["number"]: num, F["version"]: version, F["status"]: "Current", F["problems"]: ""})
    for r in rows:
        if r["id"] != rid and sel(r["fields"].get(F["status"])) == "Current":
            patch(r["id"], {F["status"]: "Previous"})
    print("published", version)


def local(copy_path, out_dir):
    """python onepager/onepager.py --local copy.json outdir : render and check without Airtable."""
    page = json.loads(pathlib.Path(copy_path).read_text())
    out = pathlib.Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    probs = shape_problems(page) + rule_problems(page)
    probs += [f'"{b}" no longer fits its box.' for b in render(page, out / "onepager.pdf", out / "preview.png")]
    print("\n".join(probs) or "ok")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--local":
        local(sys.argv[2], sys.argv[3])
        sys.exit(0)
    rid = (os.environ.get("RECORD_ID") or "").strip()
    try:
        build(rid)
    except Exception as e:
        print("Job failed:", type(e).__name__)
        if rid:
            try:
                patch(rid, {F["status"]: "Problems found",
                            F["problems"]: "The build failed, so nothing changed. The current version is still live. (" +
                            str(e)[:300] + ")"})
            except Exception:
                pass
        sys.exit(1)
