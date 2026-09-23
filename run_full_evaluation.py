#!/usr/bin/env python3
"""Multi-dimensional clinical ASR evaluation for code-switched speech.

This script evaluates ASR transcripts using a more faithful set of metrics than
standard WER for mixed-script clinical conversations. It reports:

- overall WER
- English-only WER
- Amharic/Ge'ez CER
- code-switch transliteration MER
- clinical entity extraction accuracy

The metric split is important because plain WER treats Ethiopic and transliterated
Latin tokens as substitutions or insertions even when the spoken meaning is the
same (for example, "ሆድ" vs "HOD").
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from pathlib import Path
from typing import Iterable

ETHIOPIC_BLOCK = re.compile(r"[\u1200-\u137F]+")
LATIN_WORD_RE = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)?")
NON_WORD_RE = re.compile(r"[^\w\u1200-\u137F\s'-]+")


def normalize_text(value: str) -> str:
    value = (value or "").strip()
    return NON_WORD_RE.sub(" ", value).lower().strip()


def tokenize_english(text: str) -> list[str]:
    return LATIN_WORD_RE.findall(normalize_text(text))


def tokenize_ethiopic(text: str) -> str:
    joined = "".join(ch for ch in (text or "") if "\u1200" <= ch <= "\u137F")
    return joined.strip()


def levenshtein_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    prev = list(range(len(b) + 1))
    for i, ch_a in enumerate(a, start=1):
        curr = [i]
        for j, ch_b in enumerate(b, start=1):
            cost = 0 if ch_a == ch_b else 1
            curr.append(
                min(
                    prev[j] + 1,
                    curr[j - 1] + 1,
                    prev[j - 1] + cost,
                )
            )
        prev = curr
    return prev[-1]


def word_wer(reference: str, hypothesis: str) -> float:
    ref_tokens = normalize_text(reference).split()
    hyp_tokens = normalize_text(hypothesis).split()
    if not ref_tokens:
        return 0.0 if not hyp_tokens else 1.0

    rows = list(range(len(hyp_tokens) + 1))
    for ref_index, ref_token in enumerate(ref_tokens, start=1):
        next_row = [ref_index]
        for hyp_index, hyp_token in enumerate(hyp_tokens, start=1):
            insertion = next_row[hyp_index - 1] + 1
            deletion = rows[hyp_index] + 1
            substitution = rows[hyp_index - 1] + (ref_token != hyp_token)
            next_row.append(min(insertion, deletion, substitution))
        rows = next_row
    return rows[-1] / max(1, len(ref_tokens))


def char_cer(reference: str, hypothesis: str) -> float:
    ref = normalize_text(reference)
    hyp = normalize_text(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    return levenshtein_distance(ref, hyp) / max(1, len(ref))


def mixed_error_rate(reference: str, hypothesis: str) -> float:
    """Compute MER on transliterated / code-switched Latin terms.

    A transliteration span is defined as a sequence of alphabetic tokens in a
    script-mixed sentence. This is deliberately separate from English-only WER so
    that code-switched terms like 'HOD' and 'ሆድ' do not get scored as full-word
    replacements when the phonetic meaning is the same.
    """

    ref_tokens = [token.lower() for token in LATIN_WORD_RE.findall(normalize_text(reference))]
    hyp_tokens = [token.lower() for token in LATIN_WORD_RE.findall(normalize_text(hypothesis))]
    if not ref_tokens:
        return 0.0 if not hyp_tokens else 1.0

    rows = list(range(len(hyp_tokens) + 1))
    for ref_index, ref_token in enumerate(ref_tokens, start=1):
        next_row = [ref_index]
        for hyp_index, hyp_token in enumerate(hyp_tokens, start=1):
            insertion = next_row[hyp_index - 1] + 1
            deletion = rows[hyp_index] + 1
            substitution = rows[hyp_index - 1] + (ref_token != hyp_token)
            next_row.append(min(insertion, deletion, substitution))
        rows = next_row
    return rows[-1] / max(1, len(ref_tokens))


def clinical_entity_accuracy(reference: str, hypothesis: str) -> float:
    entities = {
        "fever",
        "cough",
        "headache",
        "chest pain",
        "dyspnea",
        "wheezing",
        "amoxicillin",
        "paracetamol",
        "metformin",
        "bp",
        "pulse",
        "temperature",
        "malaria",
        "pneumonia",
        "tb",
        "appendicitis",
        "pregnancy",
    }

    ref_text = normalize_text(reference)
    hyp_text = normalize_text(hypothesis)
    ref_matches = {entity for entity in entities if entity in ref_text}
    if not ref_matches:
        return 1.0

    hits = sum(1 for entity in ref_matches if entity in hyp_text)
    return hits / len(ref_matches)


def calculate_faas_ms(first_frame_sent_ms: float | int, first_partial_token_ms: float | int) -> float:
    """Return the elapsed time in milliseconds between the first audio frame and the first partial transcript token."""
    start_ms = float(first_frame_sent_ms)
    end_ms = float(first_partial_token_ms)
    return max(0.0, end_ms - start_ms)


def calculate_partial_token_delay_ms(last_partial_token_ms: float | int, current_partial_token_ms: float | int) -> float:
    return max(0.0, float(current_partial_token_ms) - float(last_partial_token_ms))


def faas_sla_met(faas_ms: float | int | None, target_ms: float = 250.0) -> bool:
    if faas_ms is None:
        return False
    return float(faas_ms) <= float(target_ms)


def evaluate_case(reference: str, hypothesis: str, *, first_frame_sent_ms: float | int | None = None, first_partial_token_ms: float | int | None = None, prose_completed_ms: float | int | None = None) -> dict:
    ref_clean = normalize_text(reference)
    hyp_clean = normalize_text(hypothesis)

    english_ref = " ".join(tokenize_english(ref_clean))
    english_hyp = " ".join(tokenize_english(hyp_clean))
    english_wer = word_wer(english_ref, english_hyp)

    ethiopic_ref = tokenize_ethiopic(reference)
    ethiopic_hyp = tokenize_ethiopic(hypothesis)
    ethiopic_cer = char_cer(ethiopic_ref, ethiopic_hyp)

    translit_mer = mixed_error_rate(reference, hypothesis)

    faas_ms = None
    if first_frame_sent_ms is not None and first_partial_token_ms is not None:
        faas_ms = calculate_faas_ms(first_frame_sent_ms, first_partial_token_ms)

    soap_latency_ms = None
    if first_partial_token_ms is not None and prose_completed_ms is not None:
        soap_latency_ms = max(0.0, float(prose_completed_ms) - float(first_partial_token_ms))

    return {
        "standard_wer": word_wer(reference, hypothesis),
        "english_wer": english_wer,
        "amharic_geez_cer": ethiopic_cer,
        "transliteration_mer": translit_mer,
        "clinical_entity_accuracy": clinical_entity_accuracy(reference, hypothesis),
        "faas_ms": faas_ms,
        "faas_sla_met": faas_sla_met(faas_ms),
        "soap_generation_latency_ms": soap_latency_ms,
    }


def read_cases(path: Path) -> list[dict[str, str]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("cases"), list):
            return payload["cases"]
        raise ValueError("JSON input must be a list of case objects or contain a 'cases' list.")

    if suffix in {".csv"}:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        if not rows:
            raise ValueError(f"CSV input {path} is empty.")
        return rows

    raise ValueError(f"Unsupported evaluation input type: {path.suffix}")


def extract_reference_and_hypothesis(row: dict[str, object], ref_column: str, hyp_column: str) -> tuple[str, str]:
    ref = str(row.get(ref_column) or "")
    hyp = str(row.get(hyp_column) or "")
    if not ref and not hyp:
        raise ValueError(f"Row {row} is missing both reference and hypothesis values.")
    if not ref:
        raise ValueError(f"Reference column '{ref_column}' is empty for row: {row}")
    if not hyp:
        raise ValueError(f"Hypothesis column '{hyp_column}' is empty for row: {row}")
    return ref, hyp


def summarize(results: Iterable[dict]) -> dict:
    scores = list(results)
    if not scores:
        raise ValueError("No evaluation rows were supplied.")

    def mean(key: str) -> float:
        return sum(item[key] for item in scores) / len(scores)

    return {
        "cases_evaluated": len(scores),
        "overall_wer": mean("standard_wer"),
        "english_wer": mean("english_wer"),
        "amharic_geez_cer": mean("amharic_geez_cer"),
        "transliteration_mer": mean("transliteration_mer"),
        "clinical_entity_accuracy": mean("clinical_entity_accuracy"),
        "by_case": scores,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate code-switched clinical ASR transcripts using language-aware "
            "WER/CER/MER metrics instead of a single standard WER score."
        )
    )
    parser.add_argument("--input", required=True, help="CSV/JSON file with reference and hypothesis columns.")
    parser.add_argument("--reference-column", default="reference_transcript", help="Reference transcript column name.")
    parser.add_argument("--hypothesis-column", default="hypothesis_transcript", help="Hypothesis transcript column name.")
    parser.add_argument("--first-frame-column", default="first_frame_sent_ms", help="Optional column containing the first audio-frame send time in ms.")
    parser.add_argument("--first-partial-column", default="first_partial_token_ms", help="Optional column containing the first partial-token receive time in ms.")
    parser.add_argument("--soap-complete-column", default="soap_complete_ms", help="Optional column containing SOAP generation completion time in ms.")
    parser.add_argument("--output", help="Optional JSON report output path.")
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"Evaluation input not found: {input_path}")

    rows = read_cases(input_path)
    evaluated = []
    for index, row in enumerate(rows, start=1):
        ref, hyp = extract_reference_and_hypothesis(row, args.reference_column, args.hypothesis_column)
        first_frame_ms = row.get(args.first_frame_column)
        first_partial_ms = row.get(args.first_partial_column)
        soap_complete_ms = row.get(args.soap_complete_column)
        result = evaluate_case(
            ref,
            hyp,
            first_frame_sent_ms=float(first_frame_ms) if first_frame_ms not in (None, "") else None,
            first_partial_token_ms=float(first_partial_ms) if first_partial_ms not in (None, "") else None,
            prose_completed_ms=float(soap_complete_ms) if soap_complete_ms not in (None, "") else None,
        )
        result["case_id"] = str(row.get("case_id") or f"case_{index}")
        evaluated.append(result)

    summary = summarize(evaluated)
    latency_values = [case.get("faas_ms") for case in evaluated if case.get("faas_ms") is not None]
    mean_faas = sum(latency_values) / len(latency_values) if latency_values else None
    faas_sla_pass_rate = None
    if latency_values:
        faas_sla_pass_rate = sum(1 for value in latency_values if faas_sla_met(value)) / len(latency_values)
    report = {
        "dataset": input_path.name,
        "evaluation_type": "language_aware_clinical_asr",
        "metrics": {
            "overall_wer": round(summary["overall_wer"], 4),
            "english_wer": round(summary["english_wer"], 4),
            "amharic_geez_cer": round(summary["amharic_geez_cer"], 4),
            "transliteration_mer": round(summary["transliteration_mer"], 4),
            "clinical_entity_accuracy": round(summary["clinical_entity_accuracy"], 4),
            "mean_faas_ms": round(mean_faas, 2) if mean_faas is not None else None,
            "faas_sla_target_ms": 250,
            "faas_sla_pass_rate": round(faas_sla_pass_rate, 4) if faas_sla_pass_rate is not None else None,
        },
        "cases_evaluated": summary["cases_evaluated"],
        "by_case": evaluated,
    }

    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))

    if args.output:
        output_path = Path(args.output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nReport written to {output_path}")


if __name__ == "__main__":
    main()
