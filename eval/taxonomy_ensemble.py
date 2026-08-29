#!/usr/bin/env python3
"""Build a local three-family taxonomy consensus and adjudication queue."""

from __future__ import annotations

import argparse
import csv
import json
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from eval.canonical import read_json, write_json
from eval.classify_errors import CATEGORIES


PROMPTS = {
    "gemma3:4b": (
        "You are a bilingual corpus linguist. Classify each ASR edit by its immediate linguistic cause. "
        "Use nombre_propio only for named entities, palabra_otro_idioma only for code-switching, "
        "contraccion_oral for a spoken contraction, homofono_par_fonetico_minimo when expected and raw "
        "sound nearly alike, error_segmentacion only for joined/split tokens, interjeccion for non-lexical "
        "vocalizations, jerga_lunfardo for regional slang, otherwise otro."
    ),
    "mistral:7b-instruct": (
        "You are an ASR error analyst. The operation field matters: a deletion or insertion alone is NOT "
        "token segmentation. Apply: vocal sound/ad-lib => interjeccion; a single expected token split into "
        "multiple raw tokens or the reverse => error_segmentacion; shortened spoken morphology => "
        "contraccion_oral; person/place/brand => nombre_propio; genuinely code-switched token => "
        "palabra_otro_idioma; regional slang => jerga_lunfardo; a substitution with very similar phonemes "
        "=> homofono_par_fonetico_minimo; all ordinary omissions, insertions and unrelated substitutions => otro."
    ),
}


def _existing_qwen(path: Path) -> dict[str, dict[str, str]]:
    predictions = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        content = item["response"]["body"]["choices"][0]["message"]["content"]
        predictions[item["custom_id"]] = json.loads(content)
    return predictions


def _ask(model: str, prompt: str, items: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    request = urllib.request.Request(
        "http://127.0.0.1:11434/api/generate",
        data=json.dumps({
            "model": model,
            "prompt": (
                prompt + " Return only JSON as "
                "{\"results\":{\"custom_id\":{\"category\":\"...\",\"reason\":\"short\"}}}. "
                "Allowed categories: " + ", ".join(CATEGORIES) + ". Items:\n" +
                json.dumps(items, ensure_ascii=False)
            ),
            "format": "json", "stream": False, "think": False,
            "options": {"temperature": 0, "num_predict": 2200},
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        envelope = json.loads(response.read())
    parsed = json.loads(envelope.get("response") or "{}")
    if (
        isinstance(parsed, dict)
        and parsed.get("category") in CATEGORIES
        and len(items) == 1
    ):
        return {
            items[0]["custom_id"]: {
                "category": parsed["category"],
                "reason": str(parsed.get("reason") or ""),
            }
        }
    if (
        isinstance(parsed, dict)
        and parsed.get("custom_id")
        and parsed.get("category") in CATEGORIES
    ):
        return {
            str(parsed["custom_id"]): {
                "category": parsed["category"],
                "reason": str(parsed.get("reason") or ""),
            }
        }
    results = (parsed.get("results") or {}) if isinstance(parsed, dict) else {}
    if isinstance(results, list):
        return {
            str(item.get("custom_id")): {
                "category": item.get("category"), "reason": item.get("reason", ""),
            }
            for item in results if isinstance(item, dict) and item.get("custom_id")
        }
    if isinstance(results, dict):
        if results.get("category") in CATEGORIES and len(items) == 1:
            return {items[0]["custom_id"]: results}
        return {
            str(custom_id): (
                value if isinstance(value, dict)
                else {"category": value, "reason": ""}
            )
            for custom_id, value in results.items()
        }
    return {}


def classify_model(
    model: str, prompt: str, contexts: list[dict[str, Any]], checkpoint: Path,
) -> dict[str, dict[str, str]]:
    predictions: dict[str, dict[str, str]] = read_json(checkpoint) if checkpoint.is_file() else {}
    def valid(custom_id: str) -> bool:
        value = predictions.get(custom_id)
        return isinstance(value, dict) and value.get("category") in CATEGORIES
    for offset in range(0, len(contexts), 8):
        pending = [item for item in contexts[offset:offset + 8] if not valid(item["custom_id"])]
        if not pending:
            continue
        print(f"{model} taxonomy {offset + 1}-{offset + len(pending)}/{len(contexts)}", flush=True)
        try:
            predictions.update(_ask(model, prompt, pending))
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
        for item in pending:
            value = predictions.get(item["custom_id"]) or {}
            if not isinstance(value, dict):
                value = {}
            if value.get("category") not in CATEGORIES:
                single = _ask(
                    model,
                    prompt + " CRITICAL: substitution, insertion and deletion are operations, never categories. "
                    "Choose exactly one category from the allowed list; use otro when none applies.",
                    [item],
                )
                predictions.update(single)
        write_json(checkpoint, predictions)
    for item in contexts:
        custom_id = item["custom_id"]
        value = predictions.get(custom_id)
        if isinstance(value, dict) and value.get("category") in {"substitution", "insertion", "deletion"}:
            predictions[custom_id] = {
                "category": "otro",
                "reason": "invalid operation label coerced to fallback after corrective retry",
            }
    write_json(checkpoint, predictions)
    invalid = [
        item["custom_id"] for item in contexts
        if not isinstance(predictions.get(item["custom_id"]), dict)
        or predictions[item["custom_id"]].get("category") not in CATEGORIES
    ]
    if invalid:
        raise RuntimeError(f"{model} taxonomy incomplete: {invalid[:10]}")
    return predictions


def run(
    word_errors: Path, batch_input: Path, qwen_results: Path, output: Path,
) -> dict[str, Any]:
    rows = list(csv.DictReader(word_errors.open(encoding="utf-8")))
    requests = [json.loads(line) for line in batch_input.read_text(encoding="utf-8").splitlines()]
    contexts = []
    for request in requests:
        context = json.loads(request["body"]["messages"][1]["content"])
        contexts.append({"custom_id": request["custom_id"], **context})
    output.mkdir(parents=True, exist_ok=True)
    predictions = {"qwen3.5:9b": _existing_qwen(qwen_results)}
    for model, prompt in PROMPTS.items():
        predictions[model] = classify_model(model, prompt, contexts, output / f"{model.replace(':', '_')}.partial.json")

    enriched = []
    unanimous_counts = Counter()
    cross = defaultdict(Counter)
    for index, row in enumerate(rows):
        custom_id = f"error-{index:04d}"
        votes = {model: values[custom_id]["category"] for model, values in predictions.items()}
        unanimous = len(set(votes.values())) == 1
        category = next(iter(votes.values())) if unanimous else None
        enriched_row = {**row, **{f"vote_{model.split(':')[0]}": value for model, value in votes.items()},
                        "unanimous": unanimous, "consensus_category": category or ""}
        enriched.append(enriched_row)
        if unanimous:
            unanimous_counts[category] += 1
            for dimension in ("language", "position", "repeat_context", "song_id"):
                cross[(dimension, str(row.get(dimension) or "unknown"))][category] += 1
    queue = [row for row in enriched if not row["unanimous"]]
    with (output / "taxonomy_enriched_local.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(enriched[0]))
        writer.writeheader(); writer.writerows(enriched)
    with (output / "taxonomy_adjudication_queue.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(enriched[0]) + ["human_category", "human_note"])
        writer.writeheader(); writer.writerows({**row, "human_category": "", "human_note": ""} for row in queue)
    report = {
        "schema_version": 1,
        "models": ["qwen3.5:9b", *PROMPTS], "data_egress": False,
        "errors": len(rows), "unanimous": len(rows) - len(queue), "disputed": len(queue),
        "unanimous_rate": (len(rows) - len(queue)) / max(1, len(rows)),
        "unanimous_category_counts": dict(unanimous_counts.most_common()),
        "invalid_operation_fallbacks": {
            model: sum(
                str(value.get("reason") or "").startswith("invalid operation label")
                for value in values.values() if isinstance(value, dict)
            )
            for model, values in predictions.items()
        },
        "cross_tabs": [
            {"dimension": dimension, "value": value, "categories": dict(counts)}
            for (dimension, value), counts in sorted(cross.items())
        ],
        "publication_rule": {
            "unanimous_layer": "usable_provisionally as a feature; not human ground truth",
            "disputed_layer": "requires human adjudication",
        },
    }
    write_json(output / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--word-errors", type=Path, required=True)
    parser.add_argument("--batch-input", type=Path, required=True)
    parser.add_argument("--qwen-results", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("eval/runs/taxonomy_ensemble"))
    args = parser.parse_args()
    print(json.dumps(run(args.word_errors, args.batch_input, args.qwen_results, args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
