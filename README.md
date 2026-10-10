# Build the Builder

This repository contains our builder for the Freya phone-agent challenge. Given bank briefs and the matching OpenAPI file, [builder.py](builder.py) extracts a policy, generates a phone workflow, checks it, and, in live mode, saves, publishes, and exports that workflow through Freya. The builder reads rules and API fields from its inputs; do not edit a generated phone agent by hand or add final-brief values to the builder.

The original version 3 kit README and challenge rules are preserved verbatim in [docs/kit-readme-v3.md](docs/kit-readme-v3.md); read its relative file paths from the repository root. [README_TEAM.md](README_TEAM.md) has the team runbook. The kit's [workflow format](docs/workflow-format.md), [tools and code](docs/tools-and-code.md), and [Freya API](docs/api.md) references remain available.

## Requirements and credentials

- Python **3.10 or newer**. The builder, Freya client, public runner, and offline tests use the Python standard library; there is no package installation step for this builder.
- `OPENAI_API_KEY` with access to `gpt-6-sol`. **Every builder run**, including `--no-live`, sends the supplied brief text and complete OpenAPI JSON directly to the OpenAI Responses API for policy extraction. The request is billed to this key. The builder requests medium reasoning, JSON output, and `store=false`; it fails if extraction is unavailable or invalid.
- `FREYA_API_KEY` for live save and export. Set a stable `FREYA_BANK_TEAM_KEY` of at least 16 characters for public practice simulations. Freya supplies the phone agent's model, configured in [freya.py](freya.py); OpenAI is used by the builder for policy extraction.

From the repository root, copy the example shell settings and fill in your own keys locally:

```bash
cp env.example env.sh
# Edit env.sh locally, then load it into this shell:
source env.sh
```

[env.example](env.example) also shows `FREYA_API_URL`, which defaults to the kit's staging URL. Optional `FREYA_WORKSPACE_ID` selects a workspace explicitly; otherwise the Freya client uses the first returned workspace. Keep `env.sh`, `.freya-agents.json`, and `.personas.json` local. Never put credentials in briefs, workflow files, traces, or commits.

## Build an agent

Pass each brief with `--brief`, in precedence order, and its matching tool API with `--openapi`. `--slot` is a stable name for one reusable Freya workflow: rerunning it updates and publishes that workflow. `--out` is the output directory. Use a distinct output directory for each run you want to keep because files with the same slot name are overwritten.

```bash
python3 builder.py --brief {brief_path} --openapi {tool_api_path} --slot {slot_name} --out {output_dir}
```

For the public briefs, Brief B amends Brief A, so supply both in order:

```bash
python3 builder.py --brief briefs/brief-a.md --openapi tools/openapi.json --slot public-a --out out/public-a
python3 builder.py --brief briefs/brief-a.md --brief briefs/brief-b.md --openapi tools/openapi.json --slot public-b --out out/public-b
```

Live mode validates with Freya, saves and publishes the agent, exports it, and automatically runs the matching public practice cases when `FREYA_BANK_TEAM_KEY` is set. The builder enforces a 30-minute limit per run. `--no-test` skips that automatic simulation while retaining live save and export. `--no-live` creates local artifacts without contacting Freya; it **still calls OpenAI**, and its workflow JSON is not a Freya export. Without `FREYA_API_KEY`, the builder likewise writes local output without a live export. Run `python3 builder.py --help` for the current options.

The builder prints a JSON summary with paths and, in live mode, `workflow_id` and `agent_id`. In `{output_dir}`, look for:

| File | Contents |
|---|---|
| `{slot}.trace.jsonl` | Time-stamped build events, input hashes, model-call purpose and usage, validation, Freya IDs, export, and any automatic practice test; also records failure reasons. |
| `{slot}.policy.json` | Policy extracted from the supplied files. |
| `{slot}.local.workflow.json` | Generated workflow before the Freya save; useful for inspection, but not the submission export. |
| `{slot}.workflow.json` | **Live:** workflow exported back from Freya with IDs. **Local:** generated workflow only. Check the trace and summary to distinguish them. |
| `practice-results.json`, `practice-test.log` | Automatic public-test result and console log, when that test runs. |

The `out/` directory is ignored by Git. Copy the chosen live exports and traces to a deliberate submission path before committing. A failed validation or test makes the builder exit nonzero; inspect its trace even if intermediate files exist.

## Verify locally and on the public cases

Offline tests make no OpenAI or Freya calls:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 tests/probe_changed_brief.py
```

With a published `agent_id`, `FREYA_API_KEY`, and `FREYA_BANK_TEAM_KEY`, run the official public cases separately. One Freya workspace simulation runs at a time.

```bash
python3 tools/run_cases.py <agent_id> --brief A --reps 3 --out results/public-a.json
python3 tools/run_cases.py <agent_id> --brief B --reps 3 --out results/public-b.json
```

Use the ID for the corresponding A or B agent. The runner prints each case's result and writes the selected JSON result. Optional `--style pieces`, `corrects`, `talkative`, `asks_back`, or `in_words` varies how public customers speak. Scores depend on bank records and conversation checks; repeat runs matter.

## Final submission

When the organizers provide final briefs and matching OpenAPI files, run the same builder command for each input pair. Use a separate slot and output directory for each, retain that run's trace and **live Freya export**, and verify the export's workflow and agent IDs against the trace. Fix general builder bugs only; do not insert final-brief rules or values into source. Commit the builder code and chosen exports and traces after checking for credentials. Submit the exact commit and artifact paths using the destination and format supplied by the organizers. The original [kit rules](docs/kit-readme-v3.md#the-final) govern the final.
