#!/usr/bin/env python3
"""Run a 5-call hard test suite for Bank A and then Bank B sequentially.

Ensures that only one simulation runs at a time in the Freya workspace.

Usage:
    python3 tools/run_hard_5.py --bank A
    python3 tools/run_hard_5.py --bank B
    python3 tools/run_hard_5.py --bank both
    python3 tools/run_hard_5.py --dry-run
"""
import argparse
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import freya
import run_cases

# 5 representative hard cases per bank:
DEFAULT_CASES_A = ["HARD-01", "HARD-02", "HARD-04", "HARD-05", "HARD-06"]
DEFAULT_CASES_B = ["HARD-03", "HARD-07", "HARD-08", "HARD-09", "HARD-10"]


def load_agents_state():
    state_file = ROOT / ".freya-agents.json"
    if not state_file.exists():
        raise FileNotFoundError(f"Missing {state_file}")
    return json.loads(state_file.read_text())


def prepare_bank(bank_label, case_ids, agent_id, dry_run=False):
    all_cases = json.load(open(ROOT / "hard-test-set.json"))
    cases = [c for c in all_cases if c["id"] in case_ids]
    if len(cases) != len(case_ids):
        found = {c["id"] for c in cases}
        missing = set(case_ids) - found
        raise ValueError(f"Cases not found in hard-test-set.json: {missing}")

    ident = {c["id"]: {"identity": c["identity"]} for c in cases}
    store = ROOT / ".personas.json"
    ids = json.load(open(store)) if store.exists() else {}

    spec = {c["id"]: run_cases.persona_for(c, ident[c["id"]], None) for c in cases}
    key = {i: i + ":" + run_cases.hashlib.sha1(json.dumps(p, sort_keys=True).encode()).hexdigest()[:10]
           for i, p in spec.items()}

    new = [c for c in cases if key[c["id"]] not in ids]
    if new and not dry_run:
        made = freya.create_personas([spec[c["id"]] for c in new])["personas"]
        ids.update({key[c["id"]]: p["id"] for c, p in zip(new, made)})
        json.dump(ids, open(store, "w"), indent=1)

    matched_ids = {c["id"]: ids.get(key[c["id"]], "PENDING_CREATION") for c in cases}

    print(f"\n==================================================")
    print(f"Bank {bank_label} Test Suite (Agent {agent_id})")
    print(f"==================================================")
    for c in cases:
        print(f"  - {c['id']:<8} | {c['title'][:45]:<45} | persona: {matched_ids[c['id']]}")

    return cases, matched_ids

def execute_bank(bank_label, cases, matched_ids, agent_id, out_path):
    print(f"\n>>> Launching Simulation for Bank {bank_label} (5 concurrent calls)...", flush=True)
    out_dir = pathlib.Path(out_path).parent
    out_dir.mkdir(parents=True, exist_ok=True)

    rows, sim_id, it_id, status, took, med = run_cases.run_once(agent_id, cases, matched_ids)
    passed_count = sum(r["pass"] is True for r in rows)
    unscored_count = sum(r["pass"] is None for r in rows)

    summary = {
        "bank": bank_label,
        "agent_id": agent_id,
        "simulation_id": sim_id,
        "iteration_id": it_id,
        "status": status,
        "seconds": round(took),
        "passed": passed_count,
        "unscored": unscored_count,
        "total": len(rows),
        "rows": rows
    }
    with open(out_path, "w") as f:
        json.dump(summary, f, indent=1)

    print(f"\nBank {bank_label} Complete: {passed_count}/{len(rows)} passed ({took:.0f}s). Saved to {out_path}\n", flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description="Run 5 hard test cases for Bank A and/or Bank B.")
    parser.add_argument("--bank", choices=["A", "B", "both"], default="both", help="Which bank to test")
    parser.add_argument("--cases-a", default=",".join(DEFAULT_CASES_A), help="Comma-separated case IDs for Bank A")
    parser.add_argument("--cases-b", default=",".join(DEFAULT_CASES_B), help="Comma-separated case IDs for Bank B")
    parser.add_argument("--dry-run", action="store_true", help="Inspect and validate without launching simulations")
    parser.add_argument("--out-dir", default=str(ROOT / "out" / "gemini-handoff"), help="Output directory")
    args = parser.parse_args()

    state = load_agents_state()
    agent_a = state["brief-a"]["agent_id"]
    agent_b = state["brief-b"]["agent_id"]

    cases_a_ids = [c.strip() for c in args.cases_a.split(",") if c.strip()]
    cases_b_ids = [c.strip() for c in args.cases_b.split(",") if c.strip()]

    print(f"Loaded Configuration:")
    print(f"  Bank A: Agent ID {agent_a} (Version {state['brief-a']['version']})")
    print(f"  Bank B: Agent ID {agent_b} (Version {state['brief-b']['version']})")

    cases_a, personas_a = prepare_bank("A", cases_a_ids, agent_a, dry_run=args.dry_run)
    cases_b, personas_b = prepare_bank("B", cases_b_ids, agent_b, dry_run=args.dry_run)

    if args.dry_run:
        print("\nDry-run complete. All cases and personas verified.")
        return

    out_dir = pathlib.Path(args.out_dir)

    if args.bank in ("A", "both"):
        out_a = out_dir / "v15-hard-a-5.json"
        execute_bank("A", cases_a, personas_a, agent_a, str(out_a))

    if args.bank == "both":
        print("Pausing 5 seconds before starting Bank B...")
        time.sleep(5)

    if args.bank in ("B", "both"):
        out_b = out_dir / "v10-hard-b-5.json"
        execute_bank("B", cases_b, personas_b, agent_b, str(out_b))


if __name__ == "__main__":
    main()
