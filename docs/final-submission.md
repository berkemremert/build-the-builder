# Final submission package

The current organizer format requires **one** final workflow, **one** builder trace, and the URL of our own Cloudflare mock. The old two-brief practice instructions remain in [kit-readme-v3.md](kit-readme-v3.md); they do not describe the final package.

`tools/final_submission.py` works locally and makes no OpenAI, Freya, or Cloudflare calls. It checks that the workflow is a live Freya export, that its workflow and agent IDs match the successful published build in the trace, that referenced files are committed and unchanged, and that tracked files and artifacts contain no apparent credentials. It validates URL syntax but does not test that the mock is deployed or reachable.

## 1. Build and retain the final run

Run the actual final builder command once the organizer sends `{case_folder}` and the matching API material. The `run_command` in the manifest must be the **exact command that works with this repository's final CLI**, using `{case_folder}` and `{mock_url}` placeholders. Do not hand-edit the exported workflow. Keep the builder's original export and trace together under `out/` until packaging.

## 2. Stage one export and trace

From the repository root, substituting the files from the chosen successful run:

```bash
python3 tools/final_submission.py stage \
  --workflow out/CHOSEN_RUN/final.workflow.json \
  --trace out/CHOSEN_RUN/final.trace.jsonl
```

This validates the pair and copies it into `submission_artifacts/`, preserving filenames. The output prints the two repository-relative paths. Review the files, then commit and push them with the builder code:

```bash
git add submission_artifacts
git commit -m "Add final workflow export and trace"
git push origin main
```

## 3. Generate `submission.json` after that commit

Use the two paths printed by `stage`, the deployed **own Cloudflare mock** URL, and the builder command actually supported by the final CLI. For example, only if the final CLI accepts the organizer's two positional placeholders:

```bash
python3 tools/final_submission.py manifest \
  --workflow submission_artifacts/final.workflow.json \
  --trace submission_artifacts/final.trace.jsonl \
  --mock-url 'https://YOUR-MOCK.YOUR-SUBDOMAIN.workers.dev' \
  --run-command 'python3 builder.py {case_folder} {mock_url}'
python3 tools/final_submission.py check submission.json
```

`submission.json` contains `team: berking`, the sole member Berk Emre Mert (`bmert21@ku.edu.tr`), this repository URL, the current full commit SHA, the exact run command, both relative artifact paths, and the mock URL. It contains no organizer email. The file is ignored by Git because committing a manifest that names its own commit creates a self-reference; submit the generated file through the organizer's requested channel. Regenerate it if HEAD changes.

The checker rejects local-only workflow JSON, mismatched or failed traces, missing or modified committed artifacts, stale commit IDs, malformed manifest paths, placeholder mock URLs, and possible credentials. Do not run a final submission until the mock URL is reachable over HTTPS and the command works with the organizer's final case folder.
