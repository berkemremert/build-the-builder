"""Run cases against an agent and score them from the bank's ledger and the judge's reading of the conversation.

    python tools/run_cases.py <agent_id> [--brief A|B] [--cases public-test-set.json] [--out results/run.json] [--reps 3] [--style talkative]

Needs FREYA_API_KEY (your workspace) and FREYA_BANK_TEAM_KEY (any secret of 16+ characters your team picks, the same one every time).
"""
import argparse, hashlib, json, os, pathlib, re, sys, time
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tools"))
import freya
from score import http, score_ledger

PERSONA = """OUTPUT RULE: Write only what you say on the phone. No stage directions, list numbers, planning notes, or instructions to yourself. A deliberately split answer may be a natural fragment; stop there and wait for the other side.

WHO YOU ARE: {who}

PRIVATE CALL PLAN (never read it aloud):
{script}

RULES:
- Use the next relevant move as your own natural reply to what the agent just said. Never recite the plan or describe a future move.
- Plan items such as "Ask...", "Say...", or "Clarify..." describe what to do. Turn them into first-person caller speech; never say those instructions verbatim.
- Answer what you are asked. Do not volunteer information nobody asked for.
- Usually give a complete number or date in one turn. If the plan or speaking style calls for a split answer, give only the planned fragment and resume after the agent replies.
- Do not invent dates, amounts or facts that are not written above. If asked something that is not covered, give a short neutral answer.
- If the caller reads something back and it matches what you agreed, say "Yes, I confirm," unless the call script says to revise it or not to confirm.
- When the caller says goodbye, say "Thank you, goodbye." and say nothing more."""

# How a customer talks. The facts of the case stay the same, only the way of saying them changes.
STYLES = {
    "pieces": "HOW YOU TALK: give long numbers and dates in two natural spoken parts. Say the first part, stop without narrating the pause, and give the rest in your next turn or when asked. Keep corrections natural too.",
    "corrects": "HOW YOU TALK: the first time you say a day or an amount, you say a slightly different one first and correct yourself in the same sentence, like 'the 21st, sorry, I mean the 20th'. The corrected value is the one written above. Do this once or twice in the call, not more. Never do it with a security answer: those you say right the first time unless the text above says otherwise.",
    "talkative": "HOW YOU TALK: you talk a lot. Before most answers you add one or two sentences about your day, the traffic or how busy work is. Once in the call you mention that your cousin's wedding is on the 30th. It has nothing to do with any payment, and you never offer it as a payment day.",
    "asks_back": "HOW YOU TALK: twice in the call, before you answer, you ask a short question back, like 'why do you need that?' or 'which account is this about?'. Whatever the caller replies, you then give your answer.",
    "in_words": "HOW YOU TALK: you say every amount and every date in words and never in digits, like 'four hundred and fifty-five' and 'the twenty-second of October'. Postcodes, years and other codes you say digit by digit.",
}

_NUMBERED_MOVE = re.compile(r"(?m)^(\d+)\.\s+(.+)$")


def private_plan(script):
    """Keep move order without putting recitable step numbers in the persona."""
    return _NUMBERED_MOVE.sub(lambda match: "- " + match.group(2), script)


def script_leakage(case, messages):
    """Return a recited step number when a caller quotes private plan wording."""
    steps = []
    planned = case.get("planned_caller_turns")
    moves = ([(str(index), move) for index, move in enumerate(planned, 1)]
             if isinstance(planned, list) and planned else _NUMBERED_MOVE.findall(case.get("script", "")))
    for number, move in moves:
        words = re.findall(r"[A-Za-z]+", move)[:4]
        if words:
            numbered = re.compile(
                r"\b" + re.escape(number) + r"\.\s+" + r"\W+".join(map(re.escape, words)), re.I)
            move_words = re.findall(r"[a-z0-9]+", move.lower())
            instruction = re.match(
                r"(?:after|ask|say|clarify|when|for|accept|at|on|offer|follow|give|change|repeat)\b",
                move, re.I)
            steps.append((number, numbered, " ".join(move_words)
                          if instruction and len(move_words) >= 7 else ""))
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content") or message.get("text") or ""
        if isinstance(content, list):
            content = " ".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
        utterance = " ".join(str(content).split())
        normalized = " " + " ".join(re.findall(r"[a-z0-9]+", utterance.lower())) + " "
        for number, numbered, verbatim in steps:
            if numbered.search(utterance) or (verbatim and " " + verbatim + " " in normalized):
                return number
    return None


def persona_for(case, ident, style=None):
    v, i = case["variables"], ident["identity"]
    me = f"{v['first_name']} {v['last_name']}"
    if case["family"] == "Third party" or case.get("third_party"):
        who = f"You are NOT {me}. You answered this phone. You do not know {me}'s security answers and you would not give them. The text below says who you are."
    else:
        say = {"date_of_birth": "Your date of birth is {} (say it in words, like '14 March 1988').", "phone_last4": "The last four digits of your phone number on file are {}.",
               "postcode": "The postcode of your registered address is {}.", "birth_year": "Your year of birth is {}.",
               "company_number": "Your company number is {}.", "founded_year": "The company was founded in {}."}
        who = f"You are {me}. " + " ".join(say.get(k, "Your " + k.replace("_", " ") + " is {}.").format(x) for k, x in i.items()) + " Give these only when asked, and give the wrong ones where the text below says so."
    style = style or case.get("style")
    text = PERSONA.format(who=who, script=private_plan(case["script"])) + ("\n\n" + STYLES[style] if style else "")
    return {"name": case["id"] + " " + case["title"] + (f" [{style}]" if style else ""), "prompt": text, "successCriteria": "All of these are true:\n- " + "\n- ".join(case["checks"]), "tags": ["hackathon", "brief-" + case["brief"]]}

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("agent_id"); ap.add_argument("--brief"); ap.add_argument("--cases", default=str(ROOT / "public-test-set.json")); ap.add_argument("--out"); ap.add_argument("--only"); ap.add_argument("--reps", type=int, default=1); ap.add_argument("--style", choices=sorted(STYLES)); ap.add_argument("--rerun-if-slow", type=int, default=0)
    a = ap.parse_args()
    cases = [c for c in json.load(open(a.cases)) if (not a.brief or c["brief"] == a.brief) and (not a.only or c["id"] in a.only.split(","))]
    ident = {c["id"]: {"identity": c["identity"]} for c in cases}
    store = ROOT / ".personas.json"; ids = json.load(open(store)) if store.exists() else {}
    # a persona is recreated whenever its text changes, so an edited case never runs with a stale customer
    spec = {c["id"]: persona_for(c, ident[c["id"]], a.style) for c in cases}
    key = {i: i + ":" + hashlib.sha1(json.dumps(p, sort_keys=True).encode()).hexdigest()[:10] for i, p in spec.items()}
    new = [c for c in cases if key[c["id"]] not in ids]
    if new:
        made = freya.create_personas([spec[c["id"]] for c in new])["personas"]
        ids.update({key[c["id"]]: p["id"] for c, p in zip(new, made)}); json.dump(ids, open(store, "w"), indent=1)
    ids = {c["id"]: ids[key[c["id"]]] for c in cases}
    (ROOT / "results").mkdir(exist_ok=True)
    reps, total_s = [], 0
    for rep in range(a.reps):
        for attempt in range(1 + a.rerun_if_slow):                   # organisers: a run made while the model was slow is thrown away and repeated
            rows, sim_id, it_id, status, took, med = run_once(a.agent_id, cases, ids)
            if med <= 10 or attempt == a.rerun_if_slow: break
            print(f"-- run discarded: median slowest turn {med:.0f} s. Waiting 5 minutes, then repeating it.\n", flush=True); time.sleep(300)
        reps.append(rows); total_s += took
        if a.reps > 1: print(f"-- run {rep + 1} of {a.reps}: {sum(r['pass'] is True for r in rows)} / {len(rows)} passed, {took:.0f} s\n")
    need = a.reps                                                # a customer counts only when every run passes
    final = []
    for k, c in enumerate(cases):
        passes = sum(r[k]["pass"] is True for r in reps)
        unscored = sum(r[k]["pass"] is None for r in reps)
        final.append({"id": c["id"], "family": c["family"], "passes": passes, "runs": a.reps,
                      "pass": None if unscored else passes >= need, "unscored_runs": unscored})
    n = sum(f["pass"] is True for f in final)
    unscored = sum(f["pass"] is None for f in final)
    if a.reps > 1:
        for f in final: print("UNSCORED" if f["pass"] is None else ("PASS" if f["pass"] else "FAIL"), f["id"], f"{f['passes']}/{f['runs']}")
    print(f"\n{n} / {len(final)} passed (bank records and conversation checks){f', {unscored} unscored (ledger unavailable)' if unscored else ''}{'' if a.reps == 1 else f', a customer counts with {need} of {a.reps} runs'}. Last simulation {sim_id}, status {status}, {total_s:.0f} s.")
    with open(ROOT / "results" / "log.jsonl", "a") as f:      # one line per run, the record of how the score moved over time
        f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "agent_id": a.agent_id, "brief": a.brief, "runs": a.reps, "passed": n, "total": len(final), "seconds": round(total_s), "failed": [f["id"] for f in final if f["pass"] is False], "unscored": [f["id"] for f in final if f["pass"] is None]}) + "\n")
    if a.out:
        pathlib.Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        json.dump({"agent_id": a.agent_id, "simulation_id": sim_id, "iteration_id": it_id, "seconds": round(total_s), "passed": n, "total": len(final), "unscored": unscored, "runs": a.reps, "final": final, "rows": reps[-1], "all_rows": reps}, open(a.out, "w"), indent=1)

def run_once(agent_id, cases, ids):
    """One simulation: every case once. Returns the scored rows."""
    # A custom persona can reuse a published bank account while keeping its
    # own case ID and expectations for conversation and ledger scoring.
    sessions = {c["id"]: http("/v1/practice/sessions", {"case": c.get("source_case", c["id"])})["variables"] for c in cases}
    t0 = time.time()
    sim_id, it_id, status = freya.run_simulation(agent_id, f"cases {time.strftime('%H:%M:%S')}", {ids[c["id"]]: sessions[c["id"]] for c in cases})
    took = time.time() - t0
    judge = {r["personaId"]: r for r in freya.get_iteration(sim_id, it_id)["runs"]}
    rows = []
    for c in cases:
        j = judge.get(ids[c["id"]], {})                      # the conversation checks, read from the transcript by the judge model
        led, ledger_error = None, None
        try:
            led = http("/v1/practice/ledger/" + sessions[c["id"]]["customer_id"])
            if not isinstance(led, dict) or led.get("ok") is False or not all(k in led for k in ("calls", "promises", "notes", "callbacks", "transfers", "outcome")):
                raise ValueError("incomplete bank ledger response")
            r = score_ledger(c, led)
        except (OSError, ValueError, KeyError, TypeError) as error:
            led = None
            ledger_error = f"{type(error).__name__}: {error}"
            r = {"pass": None, "failed": []}
        if j.get("outcome") != "pass": r["failed"].append("conversation: " + " ".join(str(j.get("reason") or "no verdict").split())[:400])
        if led is not None: r["pass"] = not r["failed"]
        tools = [x["tool"] for x in led["calls"]] if led is not None else None
        rows.append({"id": c["id"], "source_case": c.get("source_case", c["id"]),
                     "level": c["level"], "family": c["family"], "pass": r["pass"], "failed": r["failed"],
                     "judge_outcome": j.get("outcome"), "judge_reason": j.get("reason"), "call_id": j.get("callId"),
                     "tools": tools, "ledger_available": led is not None, "ledger_error": ledger_error,
                     "customer_id": sessions[c["id"]]["customer_id"]})
    # How long the phone agent kept the customer waiting. A call whose customer hung up after long silences says
    # something about the speed of the model at that moment, not only about the agent.
    waits = []
    cases_by_id = {case["id"]: case for case in cases}
    for r in rows:
        if not r["call_id"]: continue
        try: call = freya.get_call(r["call_id"])
        except Exception: continue
        leaked_step = script_leakage(cases_by_id[r["id"]], call.get("messages", []))
        if leaked_step is not None:
            r["persona_leakage"] = leaked_step
            r["pass"] = None
            r["failed"].append(f"persona recited private script step {leaked_step}; run is not a valid workflow score")
        last_user, worst = None, 0.0
        for m in call.get("messages", []):
            t = m.get("secondsFromStart")
            if t is None: continue
            if m.get("role") == "user": last_user = t
            elif last_user is not None: worst = max(worst, t - last_user); last_user = None if m.get("role") == "assistant" else last_user
        r["slowest_turn_s"] = round(worst, 1); r["ended"] = call.get("endedReason"); waits.append(worst)
    for r in rows:
        tools = r["tools"]
        print("UNSCORED" if r["pass"] is None else ("PASS" if r["pass"] else "FAIL"), r["id"], f"{r['level']:<6}", f"{r['family']:<14}", "|", " > ".join(x.replace("verify_identity", "verify").replace("create_promise", "promise") for x in tools) if tools is not None else "(ledger unavailable)")
        if r["ledger_error"]: print("       - bank ledger unavailable:", r["ledger_error"][:260])
        for failure in r["failed"]: print("       -", failure[:260])
    if waits:
        waits.sort(); med = waits[len(waits) // 2]; hung = sum(r.get("ended") == "customer-ended-call" and r["pass"] is False for r in rows)
        print(f"\nslowest agent turn per call: median {med:.0f} s, worst {waits[-1]:.0f} s. Failed calls where the customer hung up: {hung}.")
        if med > 10: print("WARNING: the phone agent was slow in this run. If the same agent is fast in another run, the model was busy and this score is too low.")
    return rows, sim_id, it_id, status, took, (med if waits else 0)

if __name__ == "__main__": main()
