# Build the Builder

This repository contains our builder for the Freya phone-agent challenge. Given bank briefs and the matching OpenAPI file, [builder.py](builder.py) extracts a policy, generates a phone workflow, checks it, and, in live mode, saves, publishes, and exports that workflow through Freya. The builder reads rules and API fields from its inputs; do not edit a generated phone agent by hand or add final-brief values to the builder.

The original version 3 kit README and challenge rules are preserved verbatim in [docs/kit-readme-v3.md](docs/kit-readme-v3.md); read its relative file paths from the repository root. [README_TEAM.md](README_TEAM.md) has the team runbook. The kit's [workflow format](docs/workflow-format.md), [tools and code](docs/tools-and-code.md), and [Freya API](docs/api.md) references remain available.

## Requirements and credentials

- Python **3.10 or newer**. The builder, Freya client, public runner, and offline tests use the Python standard library; there is no package installation step for this builder.
- `OPENROUTER_API_KEY` for policy extraction. **Every builder run**, including `--no-live`, sends the supplied brief text and complete OpenAPI JSON to OpenRouter's chat completions API. The default model is `openai/gpt-6-sol`; set `OPENROUTER_MODEL` to override it. The builder requests medium reasoning and JSON output. It does not use `OPENAI_API_KEY` or call the direct OpenAI API.
- `FREYA_API_KEY` for live save and export. Set a stable `FREYA_BANK_TEAM_KEY` of at least 16 characters for public practice simulations. Freya supplies the phone agent's model, configured in [freya.py](freya.py).

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

Live mode validates with Freya, saves and publishes the agent, exports it, and automatically runs the matching public practice cases when `FREYA_BANK_TEAM_KEY` is set. The builder enforces a 30-minute limit per run. `--no-test` skips that automatic simulation while retaining live save and export. `--no-live` creates local artifacts without contacting Freya; it **still calls OpenRouter**, and its workflow JSON is not a Freya export. Without `FREYA_API_KEY`, the builder likewise writes local output without a live export. Run `python3 builder.py --help` for the current options.

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

Offline tests make no OpenRouter or Freya calls:

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

The final builder takes the case folder and your deployed mock's HTTPS origin as two positional arguments. The mock must run on Cloudflare Workers, implement the service contract in that folder, and remain reachable during the organizer's simulations. Use `OPENROUTER_API_KEY` for the `openai/gpt-6-sol` builder model and `FREYA_API_KEY` for Freya publication. No `OPENAI_API_KEY` is needed. This command reads the case folder at run time; it does not require copying its bank rules into repository code.

```bash
source env.sh
python3 builder.py "{case_folder}" "{mock_url}" --slot final-case --out out/final-case
```

The successful live run writes `out/final-case/final-case.workflow.json` (the Freya export) and `out/final-case/final-case.trace.jsonl` (the matching builder trace). Confirm the trace ends with `status: ok` and records the published Freya version. Keep the two files together; a local workflow or a trace from a different run is not a valid pair. The `out/` directory is ignored by Git.

Stage the chosen export and trace, commit those artifact files, then generate the manifest against the resulting commit. Replace `out/final-case` below if the chosen run used another output directory. The `{case_folder}` and `{mock_url}` strings in `--run-command` are organizer placeholders and must remain literal.

```bash
python3 tools/final_submission.py stage \
  --workflow out/final-case/final-case.workflow.json \
  --trace out/final-case/final-case.trace.jsonl
git add submission_artifacts/final-case.workflow.json submission_artifacts/final-case.trace.jsonl
git commit -m "Add final workflow export and trace"
git push origin main
python3 tools/final_submission.py manifest \
  --workflow submission_artifacts/final-case.workflow.json \
  --trace submission_artifacts/final-case.trace.jsonl \
  --mock-url "{mock_url}" \
  --run-command 'python3 builder.py "{case_folder}" "{mock_url}" --slot final-case --out out/final-case'
python3 tools/final_submission.py check submission.json
```

The checker confirms the current commit, source-file fingerprints, trace/export identity, and credential scan. If you commit again, regenerate `submission.json` so its `commit` field matches HEAD. The three files to send are `submission.json`, the exported workflow, and its trace. The organizer's deadline is **Sunday 11 October 2026, 20:00 UTC (23:00 Istanbul)**; the submission address is `yavuz@freyavoice.ai`. See the [final submission package guide](docs/final-submission.md) for the full checklist. The original [kit rules](docs/kit-readme-v3.md#the-final) remain available for background; the current organizer format controls the package.
