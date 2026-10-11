#!/usr/bin/env python3
"""Reproducible bank-brief-to-Freya builder.

    python3 builder.py --brief briefs/brief-a.md --openapi tools/openapi.json \
      --slot brief-a --out out
    python3 builder.py --brief briefs/brief-a.md --brief briefs/brief-b.md \
      --openapi tools/openapi.json --slot brief-b --out out --no-live
    python3 builder.py CASE_FOLDER MOCK_URL --slot final-case --out out/final-case

The policy and workflow are always generated from the supplied files. Live mode
saves, publishes, exports, and (for recognizable public practice briefs) tests
the agent when the corresponding credentials are present.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
import zipfile

from policy_compiler import compile_policy


MAX_BUILD_SECONDS = 30 * 60
MODEL_TIMEOUT_SECONDS = 900
OPENAI_URL = "https://api.openai.com/v1/responses"
OPENAI_MODEL = "gpt-6-sol"
OPENAI_MAX_OUTPUT_TOKENS = 25_000
KIT = Path(__file__).resolve().parent
_SECRET_PATTERN = re.compile(r"(?:\bfak_[A-Za-z0-9_-]+\b|\bsk-[A-Za-z0-9_-]+\b|\bBearer\s+[A-Za-z0-9._~+/-]+)", re.I)


def redact(value: Any) -> Any:
    if isinstance(value, str):
        for key in ("FREYA_API_KEY", "FREYA_BANK_TEAM_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            secret = os.environ.get(key)
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return _SECRET_PATTERN.sub("[REDACTED]", value)
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Trace:
    def __init__(self, path: Path, started: float) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.started = started
        self.path.write_text("", encoding="utf-8")

    def write(self, event: str, **fields: Any) -> None:
        row = redact({"at": utc_now(), "elapsed_seconds": round(time.monotonic() - self.started, 3), "event": event, **fields})
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _remaining(started: float) -> float:
    remaining = MAX_BUILD_SECONDS - (time.monotonic() - started)
    if remaining <= 0:
        raise TimeoutError("Builder reached its 30-minute limit")
    return remaining


@contextmanager
def _within_budget(started: float):
    """Bound a blocking Freya step by the remaining build wall-clock budget."""
    remaining = _remaining(started)
    can_alarm = hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()
    if not can_alarm:
        try:
            yield
        finally:
            _remaining(started)
        return

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def expired(_signum, _frame):
        raise TimeoutError("Builder reached its 30-minute limit during a Freya step")

    entered = time.monotonic()
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        yield
        _remaining(started)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, max(0, previous_timer[0] - (time.monotonic() - entered)), previous_timer[1])


def _input_fingerprint(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _case_inputs(folder: Path) -> tuple[list[Path], Path | None, dict[str, Any] | None]:
    """Find policy documents and an optional machine-readable API contract."""
    if not folder.is_dir():
        raise ValueError(f"Case folder is not a directory: {folder}")
    candidates = sorted((path for path in folder.rglob("*") if path.is_file() and
                         not any(part.startswith(".") or part.lower() in {"out", "output", "__pycache__"}
                                 for part in path.relative_to(folder).parts)),
                        key=lambda path: str(path.relative_to(folder)).lower())
    contracts: list[tuple[Path, dict[str, Any]]] = []
    documents: list[Path] = []
    for path in candidates:
        suffix = path.suffix.lower()
        if suffix not in {".md", ".txt", ".json", ".yaml", ".yml", ".pdf",
                          ".docx", ".xlsx", ".pptx", ".png", ".jpg", ".jpeg", ".webp"}:
            continue
        if path.stat().st_size > 20_000_000:
            raise ValueError(f"Case document exceeds 20 MB: {path}")
        if suffix in {".json", ".yaml", ".yml"}:
            raw = path.read_text(encoding="utf-8")
            if suffix == ".json":
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    data = None
            else:
                try:
                    import yaml  # type: ignore[import-not-found]
                except ImportError as error:
                    if re.search(r"(?m)^\s*(?:openapi|swagger):", raw):
                        try:
                            converted = subprocess.run(
                                ["ruby", "-rjson", "-ryaml", "-e",
                                 "puts JSON.generate(YAML.safe_load(STDIN.read, aliases: true))"],
                                input=raw, capture_output=True, text=True, timeout=30, check=True)
                            data = json.loads(converted.stdout)
                        except (FileNotFoundError, subprocess.CalledProcessError) as convert_error:
                            raise RuntimeError("Install PyYAML or Ruby to read the case's YAML OpenAPI contract") from convert_error
                    else:
                        data = None
                else:
                    data = yaml.safe_load(raw)
            if isinstance(data, dict) and ("openapi" in data or "swagger" in data) and isinstance(data.get("paths"), dict):
                contracts.append((path, data))
                continue
        documents.append(path)
    if len(contracts) > 1:
        raise ValueError(f"Expected at most one OpenAPI contract in {folder}; found {len(contracts)}")
    if not documents:
        raise ValueError(f"No policy or case documents found in {folder}")

    def document_order(path: Path) -> tuple[int, str, str]:
        name = path.name.lower()
        if name.startswith("readme"):
            return 0, "", str(path.relative_to(folder)).lower()
        if name.startswith("brief"):
            return 1, "", str(path.relative_to(folder)).lower()
        dated = re.match(r"(\d{4}-\d{2}-\d{2})", name)
        return (3 if dated else 2, dated.group(1) if dated else "",
                str(path.relative_to(folder)).lower())

    contract = contracts[0] if contracts else (None, None)
    return sorted(documents, key=document_order), contract[0], contract[1]


def _extract_case_text(path: Path) -> str:
    """Extract case prose locally without sending it to a conversion service."""
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt", ".json", ".yaml", ".yml"}:
        return path.read_text(encoding="utf-8")
    if suffix == ".pdf":
        result = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                                capture_output=True, text=True, timeout=90, check=True)
        if result.stdout.strip():
            return result.stdout
        with tempfile.TemporaryDirectory(prefix="btb-pdf-ocr-") as directory:
            prefix = str(Path(directory) / "page")
            subprocess.run(["pdftoppm", "-r", "160", "-png", str(path), prefix],
                           capture_output=True, text=True, timeout=300, check=True)
            pages = sorted(Path(directory).glob("page-*.png"))
            text = []
            for page in pages:
                ocr = subprocess.run(["tesseract", str(page), "stdout", "-l", "tur+eng"],
                                     capture_output=True, text=True, timeout=90, check=True)
                text.append(ocr.stdout)
        if any(page.strip() for page in text):
            return "\n\n".join(text)
        raise ValueError(f"PDF has no extractable text: {path}")
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        result = subprocess.run(["tesseract", str(path), "stdout", "-l", "tur+eng"],
                                capture_output=True, text=True, timeout=90, check=True)
        if result.stdout.strip():
            return result.stdout
        raise ValueError(f"Image OCR produced no text: {path}")
    if suffix == ".xlsx":
        with zipfile.ZipFile(path) as archive:
            strings = []
            if "xl/sharedStrings.xml" in archive.namelist():
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                strings = ["".join(node.text or "" for node in item.iter()
                                   if node.tag.rsplit("}", 1)[-1] == "t")
                           for item in root if item.tag.rsplit("}", 1)[-1] == "si"]
            sheets = []
            for name in sorted(name for name in archive.namelist()
                               if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)):
                root = ET.fromstring(archive.read(name))
                rows = []
                for row in root.iter():
                    if row.tag.rsplit("}", 1)[-1] != "row":
                        continue
                    values = []
                    for cell in row:
                        if cell.tag.rsplit("}", 1)[-1] != "c":
                            continue
                        value = next((node.text or "" for node in cell.iter()
                                      if node.tag.rsplit("}", 1)[-1] == "v"), "")
                        if cell.get("t") == "s" and value.isdecimal():
                            value = strings[int(value)] if int(value) < len(strings) else value
                        elif cell.get("t") == "inlineStr":
                            value = "".join(node.text or "" for node in cell.iter()
                                            if node.tag.rsplit("}", 1)[-1] == "t")
                        values.append(value)
                    if values:
                        rows.append("\t".join(values))
                if rows:
                    sheets.append(name + "\n" + "\n".join(rows))
        if sheets:
            return "\n\n".join(sheets)
        raise ValueError(f"Spreadsheet has no extractable cells: {path}")
    if suffix in {".docx", ".pptx"}:
        with zipfile.ZipFile(path) as archive:
            if suffix == ".docx":
                names = [name for name in archive.namelist() if name.startswith("word/") and
                         (name == "word/document.xml" or name.startswith("word/header") or
                          name.startswith("word/footer") or name in {"word/footnotes.xml", "word/endnotes.xml"})]
            elif suffix == ".pptx":
                names = [name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name) or
                         re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", name)]
            chunks = []
            for name in sorted(names):
                root = ET.fromstring(archive.read(name))
                words = [node.text for node in root.iter() if node.tag.rsplit("}", 1)[-1] in
                         {"t", "v"} and node.text]
                if words:
                    chunks.append(name + "\n" + " ".join(words))
        if chunks:
            return "\n\n".join(chunks)
        raise ValueError(f"Office document has no extractable text: {path}")
    raise ValueError(f"Unsupported case document: {path}")


def _materialize_case_documents(folder: Path, documents: list[Path], out: Path,
                                slot: str, trace: Trace) -> list[Path]:
    sources = out / f"{slot}.case-sources"
    sources.mkdir(parents=True, exist_ok=True)
    result = []
    total = 0
    for index, path in enumerate(documents, 1):
        relative = str(path.relative_to(folder))
        fixture_path = any(re.search(r"(?:^|[-_])(?:test|fixture|sample)(?:[-_]|$)", part, re.I)
                           for part in path.relative_to(folder).parts)
        if path.suffix.lower() == ".json" and path.stat().st_size > 100_000 and fixture_path:
            data = json.loads(path.read_text(encoding="utf-8"))
            record_lists = ([(name, value) for name, value in data.items()
                             if isinstance(value, list) and len(value) >= 10 and
                             all(isinstance(row, dict) for row in value)]
                            if isinstance(data, dict) else [])
            if record_lists:
                record_name, rows = max(record_lists, key=lambda item: len(item[1]))
                sample_keys = sorted({key for row in rows[:10] for key in row})
                extracted = ("TEST FIXTURE METADATA ONLY; individual synthetic records are excluded from policy "
                             "compilation. The original source is retained and fingerprinted in the trace.\n"
                             f"Description: {str(data.get('description') or '')[:500]}\n"
                             f"Record set: {record_name}\nRecord count: {len(rows)}\n"
                             f"Fields: {', '.join(sample_keys)}")
                trace.write("case_fixture_summarized", source=_input_fingerprint(path), record_count=len(rows))
            else:
                extracted = _extract_case_text(path)
        else:
            extracted = _extract_case_text(path)
        if not extracted.strip():
            raise ValueError(f"Case document has no text: {path}")
        content = f"ORIGINAL CASE SOURCE: {relative}\n\n{extracted}\n"
        total += len(content.encode("utf-8"))
        if total > 1_000_000:
            raise ValueError("Case source text exceeds 1 MB; split or summarize documents explicitly")
        target = sources / f"{index:03d}.txt"
        target.write_text(content, encoding="utf-8")
        trace.write("case_document_extracted", source=_input_fingerprint(path),
                    extracted_path=str(target), text_chars=len(extracted))
        result.append(target)
    return result


def _synthesize_openapi(documents: list[Path], call_model: Any, trace: Trace) -> dict[str, Any]:
    """Extract only explicit service operations when no OpenAPI file was supplied."""
    sources = "\n\n".join(path.read_text(encoding="utf-8") for path in documents)
    prompt = (
        "Extract the service/API contract from these case documents into one OpenAPI 3.0 JSON object. "
        "Use only explicit service contract facts in the documents. Do not infer endpoints, HTTP methods, "
        "operationId values, field names, field types, enum values, required flags, authentication, or response fields "
        "from conversation examples or common banking conventions. Preserve exact endpoint paths, operation IDs when documented, "
        "request and response schemas, parameters, security schemes, and descriptions where documented. "
        "If a service operation lacks a documented path, method, request shape, or response shape, return "
        "{\"error\":\"specific missing contract details\"} instead of guessing. "
        "Use an empty servers array; the caller's mock base URL will be installed by the builder. "
        "Return JSON only.\n\nCASE DOCUMENTS:\n" + sources
    )
    trace.write("contract_synthesis_start", purpose="extract documented service contract",
                source_files=[str(path) for path in documents], prompt_chars=len(prompt))
    raw = call_model(prompt, purpose="extract documented service contract")
    try:
        result = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as error:
        raise ValueError("Model returned invalid service contract JSON") from error
    if not isinstance(result, dict) or result.get("error"):
        raise ValueError("Service contract could not be extracted: " +
                         str(result.get("error") if isinstance(result, dict) else "invalid model output"))
    paths = result.get("paths")
    if not isinstance(result.get("openapi"), str) or not isinstance(paths, dict) or not paths:
        raise ValueError("Extracted service contract is missing OpenAPI version or paths")
    operations = [(path, method, spec) for path, path_item in paths.items()
                  if isinstance(path_item, dict) for method, spec in path_item.items()
                  if method.lower() in {"get", "post", "put", "patch", "delete"}]
    if not operations or any(not path.startswith("/") or not isinstance(spec, dict) or
                             not isinstance(spec.get("responses"), dict) or not spec["responses"] or
                             (method.lower() in {"post", "put", "patch"} and "requestBody" not in spec)
                             for path, method, spec in operations):
        raise ValueError("Extracted service contract has incomplete operation definitions")
    trace.write("contract_synthesis_passed", operation_count=len(operations),
                operations=[method.upper() + " " + path for path, method, _ in operations])
    return result


def _mock_openapi(openapi: dict[str, Any], mock_url: str) -> dict[str, Any]:
    """Route every operation to the organizer's mock service, including overrides."""
    url = mock_url.rstrip("/")
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or
            parsed.query or parsed.fragment or any(char.isspace() for char in url)):
        raise ValueError("Mock URL must be an absolute HTTP(S) base URL without credentials, query, or fragment")
    base_path = parsed.path.rstrip("/")
    api_paths = list((openapi.get("paths") or {}).keys())
    if base_path and api_paths and all(isinstance(path, str) and
                                       (path == base_path or path.startswith(base_path + "/"))
                                       for path in api_paths):
        raise ValueError("Mock URL repeats a path prefix already present in every OpenAPI endpoint")
    result = json.loads(json.dumps(openapi))
    result["servers"] = [{"url": url}]
    for path_item in (result.get("paths") or {}).values():
        if not isinstance(path_item, dict):
            continue
        path_item.pop("servers", None)
        for operation in path_item.values():
            if isinstance(operation, dict):
                operation.pop("servers", None)
    return result


def _new_slot_published_version(freya: Any, workflow_id: str) -> tuple[int, str]:
    """Read back the initial version, which create_workflow does not return."""
    result = freya.api(
        "GET", f"/api/v2/versions/workflow/{workflow_id}",
        workspaceId=freya.workspace_id(),
    )
    versions = result.get("versions") if isinstance(result, dict) else None
    published = [version for version in versions or []
                 if isinstance(version, dict) and version.get("isPublished") is True]
    if len(published) != 1:
        raise RuntimeError(f"Cannot confirm one published version for new workflow {workflow_id}")
    version = published[0]
    number, version_id = version.get("versionNumber"), version.get("id")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1 or not isinstance(version_id, str) or not version_id:
        raise RuntimeError(f"Published version metadata is invalid for new workflow {workflow_id}")
    return number, version_id


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _openai_response(payload: dict[str, Any], key: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        OPENAI_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": "build-the-builder/1.0",
        },
        method="POST",
    )
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
        return json.load(response)


class _ModelResponseError(RuntimeError):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


def _model_caller(trace: Trace, started: float):
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        trace.write("model_unavailable", provider="openai", reason="OPENAI_API_KEY is missing")
        raise RuntimeError("OPENAI_API_KEY is required for policy extraction")

    def call(prompt: str, *, purpose: str) -> str:
        begun = time.monotonic()
        trace.write("model_call_start", model=OPENAI_MODEL, requested_model=OPENAI_MODEL,
                    provider="openai", purpose=purpose, max_output_tokens=OPENAI_MAX_OUTPUT_TOKENS,
                    reasoning_effort="medium")
        actual_model = None
        response_id = None
        response_status = None
        incomplete_reason = None
        usage: dict[str, Any] = {}
        status = "failed"
        http_status = None
        try:
            payload = {
                "model": OPENAI_MODEL,
                "input": [{"role": "user", "content": prompt}],
                "text": {"format": {"type": "json_object"}},
                "reasoning": {"effort": "medium"},
                "max_output_tokens": OPENAI_MAX_OUTPUT_TOKENS,
                "store": False,
                "stream": False,
            }
            response = _openai_response(payload, key, min(MODEL_TIMEOUT_SECONDS, _remaining(started)))
            if not isinstance(response, dict) or response.get("error"):
                raise _ModelResponseError("invalid_response", "OpenAI returned an error or invalid response")
            response_id = response.get("id") if isinstance(response.get("id"), str) else None
            actual_model = response.get("model")
            if not isinstance(actual_model, str) or not actual_model:
                raise _ModelResponseError("invalid_response", "OpenAI did not identify the model")
            usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
            response_status = response.get("status")
            details = response.get("incomplete_details")
            if isinstance(details, dict):
                incomplete_reason = details.get("reason")
            if response_status != "completed":
                raise _ModelResponseError("incomplete", f"OpenAI policy response did not complete ({response_status or 'unknown'}; {incomplete_reason or 'no reason'})")
            output = response.get("output")
            if not isinstance(output, list):
                raise _ModelResponseError("invalid_response", "OpenAI returned no output items")
            parts = []
            for item in output:
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                for content in item.get("content") or []:
                    if isinstance(content, dict) and content.get("type") == "output_text" and isinstance(content.get("text"), str):
                        parts.append(content["text"])
            text = "".join(parts)
            if not text.strip():
                raise _ModelResponseError("empty", "OpenAI returned no policy text")
            status = "ok"
            return text
        except _ModelResponseError as error:
            status = error.status
            raise
        except urllib.error.HTTPError as error:
            status = "http_error"
            http_status = error.code
            error.close()
            raise RuntimeError(f"OpenAI HTTP {error.code} during policy extraction") from error
        except (TimeoutError, ConnectionError) as error:
            status = "timeout" if isinstance(error, TimeoutError) else "network_error"
            raise RuntimeError(f"OpenAI policy request {status}") from error
        except urllib.error.URLError as error:
            status = "network_error"
            raise RuntimeError("OpenAI policy request failed on the network") from error
        except (ValueError, TypeError) as error:
            status = "invalid_response"
            raise RuntimeError("OpenAI returned malformed response JSON") from error
        finally:
            output_details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
            trace.write("model_call_end", model=OPENAI_MODEL, requested_model=OPENAI_MODEL,
                        actual_model=actual_model, provider="openai", purpose=purpose, status=status,
                        seconds=round(time.monotonic() - begun, 3), response_id=response_id,
                        response_status=response_status, incomplete_reason=incomplete_reason,
                        http_status=http_status, input_tokens=usage.get("input_tokens"),
                        output_tokens=usage.get("output_tokens"),
                        reasoning_tokens=output_details.get("reasoning_tokens"))

    return call


def _practice_brief(paths: list[Path]) -> str | None:
    names = {p.name.lower() for p in paths}
    if "brief-b.md" in names:
        return "B"
    if "brief-a.md" in names:
        return "A"
    return None


def _validation_failed(result: Any) -> bool:
    if result is False:
        return True
    if isinstance(result, dict):
        if result.get("valid") is False or result.get("ok") is False:
            return True
        if result.get("errors"):
            return True
    if isinstance(result, (list, tuple)) and result and all(isinstance(x, str) for x in result):
        return True
    return False


def _contract_operation_names(openapi: dict[str, Any]) -> set[str]:
    names = set()
    for path, path_item in (openapi.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() in {"get", "post", "put", "patch", "delete"} and isinstance(operation, dict):
                names.add(str(operation.get("operationId") or str(path).rstrip("/").split("/")[-1]))
    return names


_PRACTICE_OPERATIONS = frozenset({"verify_identity", "get_account", "create_promise", "add_note",
                                  "schedule_callback", "transfer_to_agent", "close_call"})


def _run_practice_test(agent_id: str, brief: str, out: Path, trace: Trace, started: float) -> dict[str, Any]:
    result_path = out / "practice-results.json"
    result_path.unlink(missing_ok=True)
    command = [sys.executable, str(KIT / "tools" / "run_cases.py"), agent_id, "--brief", brief, "--out", str(result_path)]
    begun = time.monotonic()
    trace.write("test_start", brief=brief, agent_id=agent_id)
    try:
        result = subprocess.run(command, cwd=KIT, text=True, capture_output=True, timeout=min(20 * 60, _remaining(started)), check=False)
    except subprocess.TimeoutExpired:
        trace.write("test_end", brief=brief, status="timeout", seconds=round(time.monotonic() - begun, 3))
        return {"status": "timeout"}
    (out / "practice-test.log").write_text(redact(result.stdout + ("\n" + result.stderr if result.stderr else "")), encoding="utf-8")
    summary: dict[str, Any] = {"status": "ok" if result.returncode == 0 else "failed", "exit_code": result.returncode}
    if result_path.exists():
        data = json.loads(result_path.read_text(encoding="utf-8"))
        summary.update({key: data.get(key) for key in ("simulation_id", "iteration_id", "passed", "total", "seconds")})
        passed, total = data.get("passed"), data.get("total")
        if not isinstance(passed, int) or not isinstance(total, int) or total <= 0 or passed != total:
            summary["status"] = "failed"
    else:
        summary["status"] = "failed"
        summary["reason"] = "practice runner produced no result file"
    trace.write("test_end", brief=brief, agent_id=agent_id, duration_seconds=round(time.monotonic() - begun, 3), **summary)
    return summary


def run(args: argparse.Namespace) -> dict[str, Any]:
    begun = time.monotonic()
    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    trace = Trace(out / f"{args.slot}.trace.jsonl", begun)
    case_folder = getattr(args, "case_folder", None)
    mock_url = getattr(args, "mock_url", None)
    summary: dict[str, Any] = {"slot": args.slot, "trace": str(trace.path)}
    model_call = None
    try:
        if case_folder:
            folder = Path(case_folder).expanduser().resolve()
            source_documents, source_openapi_path, source_openapi = _case_inputs(folder)
            briefs = _materialize_case_documents(folder, source_documents, out, args.slot, trace)
            if source_openapi is None:
                trace.write("case_contract_missing", source_documents=[_input_fingerprint(path) for path in source_documents])
                model_call = _model_caller(trace, begun)
                source_openapi = _synthesize_openapi(briefs, model_call, trace)
                source_openapi_path = out / f"{args.slot}.derived.openapi.json"
                source_openapi_path.write_text(json.dumps(source_openapi, ensure_ascii=False, indent=2) + "\n",
                                               encoding="utf-8")
            openapi = _mock_openapi(source_openapi, mock_url)
            openapi_path = out / f"{args.slot}.effective.openapi.json"
            openapi_path.write_text(json.dumps(openapi, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            trace.write("case_discovered", folder=str(folder), documents=[str(path) for path in source_documents],
                        source_openapi=str(source_openapi_path), mock_url=mock_url)
        else:
            briefs = [Path(path).expanduser().resolve() for path in args.brief]
            source_openapi_path = Path(args.openapi).expanduser().resolve()
            openapi_path = source_openapi_path
            openapi = json.loads(openapi_path.read_text(encoding="utf-8"))
        trace.write("start", slot=args.slot, briefs=[str(path) for path in briefs], openapi=str(openapi_path), no_live=args.no_live)
        trace.write("inputs", briefs=[_input_fingerprint(path) for path in briefs],
                    source_documents=[_input_fingerprint(path) for path in source_documents] if case_folder else None,
                    openapi=_input_fingerprint(source_openapi_path),
                    effective_openapi=_input_fingerprint(openapi_path))
        if case_folder:
            summary["effective_openapi"] = str(openapi_path)
        if model_call is None:
            model_call = _model_caller(trace, begun)
        model_degraded = model_call is None
        try:
            policy = compile_policy([str(path) for path in briefs], str(openapi_path), model_call)
        except (RuntimeError, ValueError) as error:
            trace.write("policy_compile_failed", error_type=type(error).__name__, reason=str(error)[:200])
            raise
        trace.write("policy_validation_passed", key_count=len(policy),
                    tool_count=len(policy["tool_policy"]))
        policy_path = out / f"{args.slot}.policy.json"
        policy_path.write_text(json.dumps(policy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        trace.write("policy_written", path=str(policy_path), tool_count=len(policy["tool_policy"]), extraction_status=policy.get("constraints", {}).get("extraction_status", "model"))
        summary["policy"] = str(policy_path)

        from workflow_generator import build_workflow
        from qa import validate_workflow

        contract_names = _contract_operation_names(openapi)
        generic_workflow = bool(case_folder and not _PRACTICE_OPERATIONS.issubset(contract_names))
        if not generic_workflow:
            try:
                workflow = build_workflow(policy, openapi)
            except ValueError as error:
                if not case_folder:
                    raise
                generic_workflow = True
                trace.write("deterministic_generator_incompatible", error_type=type(error).__name__,
                            reason=str(error)[:250])
        if generic_workflow:
            from generic_workflow_fallback import build_generic_workflow
            workflow = build_generic_workflow(policy, openapi)
            trace.write("workflow_generator_selected", kind="contract_driven",
                        operation_names=sorted(contract_names))
        else:
            trace.write("workflow_generator_selected", kind="deterministic",
                        operation_names=sorted(contract_names))
        if not isinstance(workflow, dict):
            raise TypeError("build_workflow must return a workflow object")
        local_path = out / f"{args.slot}.local.workflow.json"
        local_path.write_text(json.dumps(workflow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        trace.write("workflow_written", path=str(local_path), node_count=len(workflow.get("nodes", {})), global_node_count=len(workflow.get("global_nodes", {})))
        summary["local_workflow"] = str(local_path)
        if generic_workflow:
            from qa import validate_generic_workflow
            validation = validate_generic_workflow(workflow, openapi)
        else:
            validation = validate_workflow(workflow, openapi)
        if _validation_failed(validation):
            trace.write("validation_failed", result=validation)
            raise ValueError(f"Workflow validation failed: {validation}")
        trace.write("validation_passed", result=validation if isinstance(validation, (str, bool, int, float, type(None))) else "see workflow")

        live = bool(not args.no_live and os.environ.get("FREYA_API_KEY"))
        if live:
            if model_degraded or policy.get("constraints", {}).get("extraction_status") == "source_only":
                trace.write("publish_blocked", reason="source-only policy after unavailable or failed model extraction")
                raise RuntimeError("Refusing to publish a source-only policy; restore model extraction and rerun")
            import freya

            with _within_budget(begun):
                dry_run = freya.validate_workflow(args.slot, workflow)
            trace.write("freya_dry_run", status="ok", result_type=type(dry_run).__name__)
            # Keep slot identity stable even when successive runs use different
            # output directories; Freya has a finite workflow quota.
            state_file = KIT / ".freya-agents.json"
            with _within_budget(begun):
                saved = freya.save_workflow(args.slot, workflow, state_file=str(state_file))
            published_version = saved.get("version")
            version_id = None
            if published_version is None:
                trace.write("freya_created", workflow_id=saved.get("workflow_id"), agent_id=saved.get("agent_id"))
                with _within_budget(begun):
                    published_version, version_id = _new_slot_published_version(freya, saved["workflow_id"])
            trace.write("freya_saved_published", workflow_id=saved.get("workflow_id"), agent_id=saved.get("agent_id"), version=published_version, version_id=version_id)
            summary.update({"workflow_id": saved.get("workflow_id"), "agent_id": saved.get("agent_id"), "version": published_version})
            export_path = out / f"{args.slot}.workflow.json"
            with _within_budget(begun):
                freya.export_workflow(saved["workflow_id"], str(export_path))
            trace.write("freya_exported", path=str(export_path), workflow_id=saved["workflow_id"])
            summary["workflow"] = str(export_path)
            practice = _practice_brief(briefs)
            if not args.no_test and practice and os.environ.get("FREYA_BANK_TEAM_KEY"):
                summary["test"] = _run_practice_test(saved["agent_id"], practice, out, trace, begun)
                if summary["test"]["status"] != "ok":
                    raise RuntimeError(f"Practice test failed: {summary['test'].get('passed', '?')}/{summary['test'].get('total', '?')} passed")
            else:
                reason = "disabled" if args.no_test else "no matching public practice brief or team key"
                trace.write("test_skipped", reason=reason)
        else:
            workflow_path = out / f"{args.slot}.workflow.json"
            workflow_path.write_text(json.dumps(workflow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            trace.write("freya_skipped", reason="--no-live" if args.no_live else "FREYA_API_KEY unavailable", path=str(workflow_path))
            trace.write("test_skipped", reason="live agent unavailable")
            summary["workflow"] = str(workflow_path)
        summary["duration_seconds"] = round(time.monotonic() - begun, 3)
        trace.write("end", status="ok", **summary)
        return summary
    except Exception as error:
        trace.write("end", status="failed", error_type=type(error).__name__, message=str(error)[:1000], duration_seconds=round(time.monotonic() - begun, 3))
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("case_folder", nargs="?", help="Case folder supplied by the organizer")
    parser.add_argument("mock_url", nargs="?", help="Mock service base URL; endpoint paths from the contract are appended")
    parser.add_argument("--brief", action="append", help="Brief path; repeat in precedence order")
    parser.add_argument("--openapi", help="Bank tool OpenAPI JSON")
    parser.add_argument("--slot", help="Reusable Freya workflow slot")
    parser.add_argument("--out", help="Output directory for artifacts and trace")
    parser.add_argument("--no-live", action="store_true", help="Generate locally without Freya save/export/test")
    parser.add_argument("--no-test", action="store_true", help="Save and export but skip the public practice simulation")
    args = parser.parse_args()
    if args.case_folder or args.mock_url:
        if not args.case_folder or not args.mock_url or args.brief or args.openapi:
            parser.error("case-folder mode needs CASE_FOLDER MOCK_URL and no --brief/--openapi flags")
        folder_name = Path(args.case_folder).expanduser().name
        args.slot = args.slot or re.sub(r"[^A-Za-z0-9_.-]+", "-", folder_name).strip(".-")[:80] or "final-case"
        args.out = args.out or str(Path("out") / args.slot)
    else:
        if not args.brief or not args.openapi or not args.slot or not args.out:
            parser.error("practice mode needs --brief, --openapi, --slot, and --out")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", args.slot) or args.slot in {".", ".."}:
        parser.error("--slot must be a simple name using letters, digits, dot, underscore or hyphen")
    try:
        print(json.dumps(run(args), ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(f"builder failed: {type(error).__name__}: {redact(str(error))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
