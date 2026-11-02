"""Entry point for build-deck and finalize-deck runs.

build-deck: Claude plans and researches, then run_request.py (or custom_deck / convert_deck) builds the
deck, then review.prepare() saves slide previews and slide text and ticks Review ready, which sends the
requester the review link. finalize-deck: review.finalize() applies the changes kept on the hub review
page and sets Status = Final, which sends the final files.

The Airtable automation "Deck request: plan and build" no longer uses Airtable AI. It clears the old
plan and research, sets Status = Building and fires this build. When the row has no plan, Claude
(ai_steps.py) picks the slides and writes talking points, and researches the two data slides with web
search in the background while the build runs. Both are saved to the row, where run_request.py reads
them exactly as it read the Airtable AI output before.

If a Claude step fails the deck still goes out (default slides without talking points, or no data
slides) and the reason is added to the Market data check field.
"""
import datetime, json, os, sys, threading, traceback

import run_request as rr
import ai_steps
import review

NOTES = []
# run_request.F plus the request fields only the Claude prompts read
F = dict(rr.F, room="fldh3Ppbw0ItJoTFw", next_step="fldFr9808gsFms3qY", background="fldiUPLZFpHCY7ArN")


def presenter_names(f):
    names = []
    for pid in f.get(rr.F["presenters"], [])[:2]:
        try:
            names.append(rr.get_record(rr.T_PRES, pid).get(rr.P["name"], ""))
        except Exception:
            pass
    return names


def research(f):
    try:
        r = ai_steps.make_research(f, F, datetime.date.today().isoformat())
    except Exception as e:
        NOTES.append(f"Claude research failed, no data slides: {str(e)[:300]}")
        r = {"relevant": False, "stats": []}
    try:
        rr.update({rr.F["research"]: json.dumps(r, indent=2)})
    except Exception:
        NOTES.append("Could not save the research to the row.")


def claude_steps():
    f = rr.get_record(rr.T_REQ, rr.REC)
    if (f.get(rr.F["plan"]) or "").strip():
        return                                   # plan already on the row (old flow or a manual rerun)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        NOTES.append("ANTHROPIC_API_KEY repo secret is missing: built without a plan or data slides.")
        rr.update({rr.F["research"]: json.dumps({"relevant": False, "stats": []})})   # don't wait six minutes
        return
    threading.Thread(target=research, args=(f,), daemon=True).start()
    try:
        plan = ai_steps.make_plan(f, F, presenter_names(f))
        rr.update({rr.F["plan"]: json.dumps(plan, indent=2)})
        print("claude plan: ok")
    except Exception as e:
        NOTES.append(f"Claude slide plan failed, built the default slides without talking points: {str(e)[:300]}")
        print("claude plan: failed")


def add_notes():
    if not NOTES:
        return
    try:
        cur = rr.get_record(rr.T_REQ, rr.REC).get(rr.F["rcheck"]) or ""
        rr.update({rr.F["rcheck"]: "\n".join(NOTES + ([cur] if cur else []))})
    except Exception:
        pass


if __name__ == "__main__":
    if rr.MODE == "check-library":
        os.execv(sys.executable, [sys.executable, os.path.join(rr.HERE, "run_request.py")] + sys.argv[1:])
    try:
        if rr.MODE == "finalize-deck":
            review.finalize()                    # requester submitted the review on the hub
            sys.exit(0)
        review.reset()                           # a (re)build starts a fresh review
        rtype = rr.get_record(rr.T_REQ, rr.REC).get("fldQahyDEnFyMIm87") or ""
        if rtype == "Custom outline":
            import custom_deck                   # requester wrote the outline on the hub
            custom_deck.run()
        elif rtype == "Convert deck":
            import convert_deck                  # requester uploaded their own deck on the hub
            convert_deck.run()
        else:
            claude_steps()
            rr.main()
            add_notes()
        review.prepare()                         # previews + slide text, then Review ready -> review email
    except SystemExit:
        raise
    except Exception:
        err = traceback.format_exc()
        print("Run failed. Details are on the Airtable row.")   # public repo: no request data in logs
        try:
            rr.update({rr.F["status"]: "Error", rr.F["error"]: ("\n".join(NOTES) + "\n" + err)[-3000:],
                       rr.F["log"]: rr.RUN_URL or None})
        except Exception:
            print("Could not write the error to Airtable.")
        sys.exit(1)
