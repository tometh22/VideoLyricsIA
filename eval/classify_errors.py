#!/usr/bin/env python3
"""Prepare, submit, and collect the residual-error taxonomy via OpenAI Batch."""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import urllib.request
from pathlib import Path
from typing import Any

from eval.canonical import read_json, segments_to_lines, write_json

CATEGORIES = (
    "jerga_lunfardo", "nombre_propio", "palabra_otro_idioma",
    "contraccion_oral", "homofono_par_fonetico_minimo",
    "error_segmentacion", "interjeccion", "otro",
)


def prepare(golden: Path, word_errors: Path, output: Path, model: str) -> dict[str, Any]:
    rows = list(csv.DictReader(word_errors.open(encoding="utf-8")))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for index, row in enumerate(rows):
            song_id = row["song_id"]
            raw = read_json(golden / song_id / "raw_pipeline_output.json")["segments"]
            raw_lines = segments_to_lines(raw)
            hyp_index = row.get("hyp_line_idx")
            hypothesis_context = ""
            if hyp_index not in (None, ""):
                try:
                    hypothesis_context = raw_lines[int(hyp_index)]["text"]
                except (IndexError, TypeError, ValueError):
                    pass
            context = {
                "operation": row["type"],
                "expected_word": row.get("ref_word"),
                "raw_word": row.get("hyp_word"),
                "approved_line": row.get("original_reference"),
                "raw_line": hypothesis_context,
            }
            request = {
                "custom_id": f"error-{index:04d}",
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": {
                    "model": model,
                    "temperature": 0,
                    "max_tokens": 100,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": (
                            "Classify one Spanish/English singing-ASR word error. Return JSON with "
                            "category and reason. category must be exactly one of: " + ", ".join(CATEGORIES) +
                            ". Use otro only when none apply; do not infer facts absent from context."
                        )},
                        {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
                    ],
                },
            }
            handle.write(json.dumps(request, ensure_ascii=False) + "\n")
    metadata = {
        "schema_version": 1, "requests": len(rows), "model": model,
        "categories": list(CATEGORIES), "input": str(output),
    }
    write_json(output.with_suffix(".meta.json"), metadata)
    return metadata


def submit(batch_input: Path, state: Path) -> dict[str, Any]:
    if os.environ.get("ALLOW_EXTERNAL_CLIENT_TEXT_BATCH") != "1":
        raise RuntimeError(
            "External client-text egress is policy-blocked. Set "
            "ALLOW_EXTERNAL_CLIENT_TEXT_BATCH=1 only after explicit authorization."
        )
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required")
    from openai import OpenAI

    client = OpenAI()
    with batch_input.open("rb") as handle:
        uploaded = client.files.create(file=handle, purpose="batch")
    batch = client.batches.create(
        input_file_id=uploaded.id,
        endpoint="/v1/chat/completions",
        completion_window="24h",
        metadata={"project": "genly-golden-autopsy", "baseline": "2026-08-29"},
    )
    result = {
        "schema_version": 1, "batch_id": batch.id, "input_file_id": uploaded.id,
        "status": batch.status, "output_file_id": batch.output_file_id,
        "error_file_id": batch.error_file_id,
    }
    write_json(state, result)
    return result


def collect(state: Path, output: Path) -> dict[str, Any]:
    from openai import OpenAI

    client = OpenAI()
    saved = read_json(state)
    batch = client.batches.retrieve(saved["batch_id"])
    current = {**saved, "status": batch.status, "output_file_id": batch.output_file_id, "error_file_id": batch.error_file_id}
    write_json(state, current)
    if batch.status != "completed" or not batch.output_file_id:
        return current
    content = client.files.content(batch.output_file_id).text
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
    return current


def local_classify(batch_input: Path, output: Path, model: str) -> dict[str, Any]:
    """Run the same taxonomy locally through Ollama, with no client-data egress."""
    requests = [json.loads(line) for line in batch_input.read_text(encoding="utf-8").splitlines()]

    def ask(items: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
        payload_items = []
        for item in items:
            context = json.loads(item["body"]["messages"][1]["content"])
            payload_items.append({"custom_id": item["custom_id"], **context})
        prompt = (
            "Classify each singing-ASR error. Return only JSON as "
            "{\"results\":{\"custom_id\":{\"category\":\"...\",\"reason\":\"short\"}}}. "
            "category must be exactly one of: " + ", ".join(CATEGORIES) +
            ". Use otro only if none applies. Items:\n" + json.dumps(payload_items, ensure_ascii=False)
        )
        request = urllib.request.Request(
            "http://127.0.0.1:11434/api/generate",
            data=json.dumps({
                "model": model, "prompt": prompt, "format": "json", "stream": False,
                "think": False, "options": {"temperature": 0, "num_predict": 2000},
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=300) as response:
            envelope = json.loads(response.read())
        parsed = json.loads(envelope.get("response") or "{}")
        return parsed.get("results") or {}

    checkpoint = output.with_suffix(".partial.json")
    predictions: dict[str, dict[str, str]] = read_json(checkpoint) if checkpoint.is_file() else {}
    for offset in range(0, len(requests), 10):
        chunk = requests[offset:offset + 10]
        pending = [item for item in chunk if item["custom_id"] not in predictions]
        if not pending:
            continue
        print(f"local taxonomy {offset + 1}-{offset + len(chunk)}/{len(requests)}", flush=True)
        try:
            answer = ask(pending)
        except (json.JSONDecodeError, KeyError, TypeError):
            answer = {}
        predictions.update(answer)
        for item in pending:
            prediction = predictions.get(item["custom_id"]) or {}
            if prediction.get("category") not in CATEGORIES:
                predictions.update(ask([item]))
        write_json(checkpoint, predictions)
    missing = [item["custom_id"] for item in requests if item["custom_id"] not in predictions]
    invalid = [key for key, value in predictions.items() if value.get("category") not in CATEGORIES]
    if missing or invalid:
        raise RuntimeError(f"local taxonomy incomplete; missing={missing[:5]}, invalid={invalid[:5]}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for item in requests:
            prediction = predictions[item["custom_id"]]
            envelope = {
                "custom_id": item["custom_id"],
                "response": {"status_code": 200, "body": {"choices": [{"message": {"content": json.dumps(prediction, ensure_ascii=False)}}]}},
            }
            handle.write(json.dumps(envelope, ensure_ascii=False) + "\n")
    return {"requests": len(requests), "model": model, "data_egress": False}


def validation_sample(results: Path, word_errors: Path, output: Path, count: int = 30) -> None:
    source_rows = list(csv.DictReader(word_errors.open(encoding="utf-8")))
    by_id = {}
    for line in results.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        content = item["response"]["body"]["choices"][0]["message"]["content"]
        by_id[item["custom_id"]] = json.loads(content)
    generator = random.Random(20260829)
    indices = sorted(generator.sample(range(len(source_rows)), min(count, len(source_rows))))
    rows = []
    for index in indices:
        source = source_rows[index]
        prediction = by_id[f"error-{index:04d}"]
        rows.append({
            "custom_id": f"error-{index:04d}", "song_id": source["song_id"],
            "operation": source["type"], "expected_word": source.get("ref_word"),
            "raw_word": source.get("hyp_word"), "approved_line": source.get("original_reference"),
            "llm_category": prediction.get("category"), "llm_reason": prediction.get("reason"),
            "human_category": "", "human_agrees": "", "human_note": "",
        })
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    prepare_parser.add_argument("--word-errors", type=Path, required=True)
    prepare_parser.add_argument("--output", type=Path, required=True)
    prepare_parser.add_argument("--model", default="gpt-4o-mini")
    submit_parser = sub.add_parser("submit")
    submit_parser.add_argument("--input", type=Path, required=True)
    submit_parser.add_argument("--state", type=Path, required=True)
    collect_parser = sub.add_parser("collect")
    collect_parser.add_argument("--state", type=Path, required=True)
    collect_parser.add_argument("--output", type=Path, required=True)
    sample_parser = sub.add_parser("sample")
    sample_parser.add_argument("--results", type=Path, required=True)
    sample_parser.add_argument("--word-errors", type=Path, required=True)
    sample_parser.add_argument("--output", type=Path, required=True)
    local_parser = sub.add_parser("local")
    local_parser.add_argument("--input", type=Path, required=True)
    local_parser.add_argument("--output", type=Path, required=True)
    local_parser.add_argument("--model", default="qwen3.5:9b")
    args = parser.parse_args()
    if args.command == "prepare":
        print(json.dumps(prepare(args.golden, args.word_errors, args.output, args.model), indent=2))
    elif args.command == "submit":
        print(json.dumps(submit(args.input, args.state), indent=2))
    elif args.command == "collect":
        print(json.dumps(collect(args.state, args.output), indent=2))
    elif args.command == "local":
        print(json.dumps(local_classify(args.input, args.output, args.model), indent=2))
    else:
        validation_sample(args.results, args.word_errors, args.output)
        print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
