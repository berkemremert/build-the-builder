# Bank B long-call stress set

[`long-call-stress.json`](../long-call-stress.json) contains three targeted cases (`LONG-01`–`LONG-03`) with 14 planned caller turns each and two soak cases (`LONG-04`–`LONG-05`) with 25 each. Every case reuses a published Bank B fixture through `source_case`, so the seeded identity and products match the public practice bank. The persona's `pieces` style allows split answers. The numbered caller moves test state across identity, product order, account-fact clarification, per-product terms, and corrected combined confirmation. `LONG-04` includes one non-answer within the first late-band persuasion attempt; it accepts that offer without requiring another attempt.

## Run in two tiers

Wait until the bank practice-session endpoint and Freya simulation service are healthy. Use a **published Bank B agent** and one simulation at a time. Run the targeted tier first; review results and transcripts before starting the soak tier. The runner executes the three repetitions sequentially, and a case counts as passed only when all three pass.

```bash
AGENT_ID='your-published-bank-b-agent-id'
python3 tools/run_cases.py "$AGENT_ID" --cases long-call-stress.json --brief B --only LONG-01,LONG-02,LONG-03 --reps 3 --out results/long-targeted.json
python3 tools/run_cases.py "$AGENT_ID" --cases long-call-stress.json --brief B --only LONG-04,LONG-05 --reps 3 --out results/long-soak.json
```

Each JSON result has `all_rows`, with a `call_id` for every repetition. The simulator may combine or skip scripted moves, so **planned turns are not measured turns**. Fetch each transcript and count customer messages before treating the call as a long-call result:

```bash
python3 - <<'PY'
import json
import freya
for result_path in ('results/long-targeted.json', 'results/long-soak.json'):
    result = json.load(open(result_path))
    for repeat, rows in enumerate(result['all_rows'], 1):
        for row in rows:
            call = freya.get_call(row['call_id'])
            turns = sum(message.get('role') == 'user' for message in call.get('messages', []))
            print(result_path, repeat, row['id'], turns, row['pass'], row['call_id'])
PY
```

If a call compresses far below its target, treat that result as a shorter stress check; inspect the transcript instead of inferring that every planned boundary was exercised. Preserve the result JSON and any transcripts used for review.

The two tiers request **15 simulated calls total** (three cases × three runs, then two cases × three runs). The Freya client sets a 420-second maximum per call and up to five concurrent calls within one simulation by default. Queueing, service health, and conversation length determine actual elapsed time. No Freya per-call price is provided in this kit, so there is no grounded monetary estimate. Running these cases through `tools/run_cases.py` does not invoke the builder's OpenAI policy extraction.

Before a live run, validate the case file offline:

```bash
python3 -m unittest discover -s tests -p 'test_long_call_stress.py' -v
```
