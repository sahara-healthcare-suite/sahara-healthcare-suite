import os
import json
import math
import asyncio
import argparse
import csv
from pathlib import Path
from statistics import fmean

try:
    import jiwer
except ImportError:
    jiwer = None

DATASET_INDEX_PATH = os.getenv("DATASET_INDEX", "./evaluation_dataset.json")
OUTPUT_REPORT_PATH = os.getenv("OUTPUT_REPORT", "./benchmark_report.json")
OUTPUT_MARKDOWN_PATH = os.getenv("OUTPUT_MARKDOWN", "./BENCHMARK_RESULTS.md")
AFRISWITCH_ROOT = Path(os.getenv("AFRISWITCH_ROOT", "./clinical_validation/afriswitch"))

INTRON_API_KEY = os.getenv("INTRON_API_KEY", "")

CLINICAL_ENTITIES = [
    "fever", "cough", "headache", "hypertension", "diabetes", "paracetamol",
    "amoxicillin", "metformin", "bp", "pulse", "chills", "tb", "malaria",
    "pneumonia", "dyspnea", "tachycardia", "tuberculosis", "mg", "ml"
]

def calculate_wer(reference: str, hypothesis: str) -> float:
    ref_clean = reference.lower().strip()
    hyp_clean = hypothesis.lower().strip()
    if not ref_clean:
        return 0.0 if not hyp_clean else 1.0
    if jiwer:
        return float(jiwer.wer(ref_clean, hyp_clean))

    ref_words = ref_clean.split()
    hyp_words = hyp_clean.split()
    distances = list(range(len(hyp_words) + 1))
    for ref_word in ref_words:
        next_distances = [distances[0] + 1]
        for index, hyp_word in enumerate(hyp_words, start=1):
            substitution = distances[index - 1] + (ref_word != hyp_word)
            insertion = next_distances[index - 1] + 1
            deletion = distances[index] + 1
            next_distances.append(min(substitution, insertion, deletion))
        distances = next_distances
    return distances[-1] / len(ref_words)

def calculate_entity_accuracy(reference: str, hypothesis: str) -> float:
    ref_words = set(reference.lower().split())
    hyp_words = set(hypothesis.lower().split())
    target_entities = [e for e in CLINICAL_ENTITIES if e in ref_words or any(e in w for w in ref_words)]
    if not target_entities:
        return 1.0
    matches = sum(1 for entity in target_entities if any(entity in w for w in hyp_words))
    return matches / len(target_entities)

def calculate_faas(overall_score: float, wer: float) -> float:
    if wer <= 0.0001:
        wer = 0.0001
    if overall_score <= 0.0:
        overall_score = 0.001
    return float(10.0 * math.log10(overall_score / wer))

BENCHMARK_SAMPLES = [
    {
        "id": "sample_001",
        "audio_path": "./samples/sample_001.wav",
        "reference": "patient unique identification. patient presents with severe headache and fever spanning 3 days. prescribed paracetamol 500mg twice daily.",
        "language_pair": "English-Amharic Code-Switch",
        "hypotheses": {
            "Intron Sahara v2.5": "patient unique identification. patient presents with severe headache and fever spanning 3 days. prescribed paracetamol 500mg twice daily.",
            "OpenAI Whisper (Medium)": "patient unique identification patient present with severe headache and high fever 3 days prescribed paracetamol 500 daily",
            "Meta Wav2Vec2 (XLS-R)": "patient unique identification patient severe headache fever 3 days prescribed paracetamol"
        }
    },
    {
        "id": "sample_002",
        "audio_path": "./samples/sample_002.wav",
        "reference": "የጤና ተቋም። chief complaint is chest pain with short breath. clinical assessment shows blood pressure 140 over 90.",
        "language_pair": "English-Amharic Code-Switch",
        "hypotheses": {
            "Intron Sahara v2.5": "የጤና ተቋም። chief complaint is chest pain with short breath. clinical assessment shows blood pressure 140 over 90.",
            "OpenAI Whisper (Medium)": "የጤና ተቋም chief complaint chest pain short breath blood pressure 140 over 90",
            "Meta Wav2Vec2 (XLS-R)": "chief complaint chest pain short breath blood pressure 140 90"
        }
    },
    {
        "id": "sample_003",
        "audio_path": "./samples/sample_003.wav",
        "reference": "patient has suspected malaria and pneumonia. recommended amoxicillin 500mg and urgent lab workup.",
        "language_pair": "English Clinical Standard",
        "hypotheses": {
            "Intron Sahara v2.5": "patient has suspected malaria and pneumonia. recommended amoxicillin 500mg and urgent lab workup.",
            "OpenAI Whisper (Medium)": "patient suspected malaria and pneumonia recommended amoxicillin 500mg urgent lab workup",
            "Meta Wav2Vec2 (XLS-R)": "patient suspect malaria pneumonia recommended amoxicillin lab workup"
        }
    }
]

def run_afriswitch_pilot(root: Path) -> None:
    """Validate the imported pilot and report reference coverage only."""
    manifest_dir = root / "manifests"
    metadata_path = root / "import_metadata.json"
    if not manifest_dir.is_dir() or not metadata_path.is_file():
        raise FileNotFoundError(
            f"AfriSwitch import not found at {root}. Run afriswitch_import.py first."
        )

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    configs = {}
    total_duration = 0.0
    for manifest_path in sorted(manifest_dir.glob("*.csv")):
        rows = list(csv.DictReader(manifest_path.open(encoding="utf-8", newline="")))
        missing_audio = sum(
            not (root / row["local_audio"]).is_file()
            for row in rows
        )
        duration = sum(float(row["duration"]) for row in rows if row.get("duration"))
        switches = sum(int(row["num_switch_points"]) for row in rows if row.get("num_switch_points"))
        configs[manifest_path.stem] = {
            "utterances": len(rows),
            "audio_files": len(rows) - missing_audio,
            "missing_audio": missing_audio,
            "duration_seconds": round(duration, 2),
            "switch_points": switches,
        }
        total_duration += duration

    report = {
        "dataset_id": metadata["dataset_id"],
        "dataset_revision": metadata["dataset_revision"],
        "split": metadata["split"],
        "license": metadata["license"],
        "configs": configs,
        "total_utterances": sum(item["utterances"] for item in configs.values()),
        "total_duration_seconds": round(total_duration, 2),
        "evaluation_status": "reference_only",
        "note": "No model hypotheses were supplied; WER and model rankings are intentionally not reported.",
    }
    output_path = root / "afriswitch_pilot_report.json"
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Pilot report exported to {output_path}")

async def run_benchmark():
    print("============================================================")
    print("Starting Multi-Model Speech Recognition Benchmark...")
    print("Models: Intron Sahara v2.5 | OpenAI Whisper | Meta Wav2Vec2")
    print("============================================================")

    models = ["Intron Sahara v2.5", "OpenAI Whisper (Medium)", "Meta Wav2Vec2 (XLS-R)"]
    results = {m: {"wers": [], "entity_accuracies": []} for m in models}

    for item in BENCHMARK_SAMPLES:
        ref = item["reference"]
        for model_name in models:
            hyp = item["hypotheses"][model_name]
            wer = calculate_wer(ref, hyp)
            ea = calculate_entity_accuracy(ref, hyp)
            results[model_name]["wers"].append(wer)
            results[model_name]["entity_accuracies"].append(ea)

    summary = {}
    print("\n============================================================")
    print("FINAL BENCHMARK RESULTS")
    print("============================================================")

    for m in models:
        mean_wer = fmean(results[m]["wers"])
        mean_ea = fmean(results[m]["entity_accuracies"])
        faas = calculate_faas(overall_score=mean_ea, wer=mean_wer)
        
        summary[m] = {
            "mean_wer": round(mean_wer, 4),
            "clinical_entity_accuracy": round(mean_ea, 4),
            "faas_score": round(faas, 2)
        }
        print(f"Model: {m}")
        print(f"  - Mean WER: {summary[m]['mean_wer'] * 100:.2f}%")
        print(f"  - Clinical Entity Accuracy: {summary[m]['clinical_entity_accuracy'] * 100:.2f}%")
        print(f"  - FAAS Score: {summary[m]['faas_score']} dB\n")

    with open(OUTPUT_REPORT_PATH, "w") as f:
        json.dump(summary, f, indent=2)

    intron_wer = f"{summary['Intron Sahara v2.5']['mean_wer']*100:.2f}%"
    intron_ea = f"{summary['Intron Sahara v2.5']['clinical_entity_accuracy']*100:.2f}%"
    intron_faas = summary['Intron Sahara v2.5']['faas_score']

    whisper_wer = f"{summary['OpenAI Whisper (Medium)']['mean_wer']*100:.2f}%"
    whisper_ea = f"{summary['OpenAI Whisper (Medium)']['clinical_entity_accuracy']*100:.2f}%"
    whisper_faas = summary['OpenAI Whisper (Medium)']['faas_score']

    w2v_wer = f"{summary['Meta Wav2Vec2 (XLS-R)']['mean_wer']*100:.2f}%"
    w2v_ea = f"{summary['Meta Wav2Vec2 (XLS-R)']['clinical_entity_accuracy']*100:.2f}%"
    w2v_faas = summary['Meta Wav2Vec2 (XLS-R)']['faas_score']

    md_content = f"""# Fixture Speech Recognition Benchmark Report

> **Evidence limitation:** This is a reproducible software fixture, not an
> independent audio benchmark. The hypotheses are embedded in
> `benchmark_suite.py`, and the referenced sample audio is not included.
> Do not present these values as production performance or a real model
> ranking.

| Model | Average WER ↓ | Clinical Entity Accuracy ↑ | FAAS Score (dB) ↑ | Status |
| :--- | :---: | :---: | :---: | :---: |
| **Intron Sahara v2.5** | **{intron_wer}** | **{intron_ea}** | **{intron_faas}** | Fixture reference |
| OpenAI Whisper (Medium) | {whisper_wer} | {whisper_ea} | {whisper_faas} | Baseline |
| Meta Wav2Vec2 (XLS-R) | {w2v_wer} | {w2v_ea} | {w2v_faas} | Baseline |

### Evaluation Methodology
1. **Word Error Rate (WER)**: Normalized string distance metric (S + D + I) / N.
2. **Clinical Entity Accuracy**: Recall rate of medical terms (symptoms, dosages, diagnoses).
3. **Fairness-Adjusted ASR Score (FAAS)**: Calculated as 10 * log10(Clinical Entity Accuracy / WER).

The separate 15-case clinical validation baseline reported 56.38% mean WER,
44.33% target-term recall, and critical-term misses in 6 cases. That result is
for clinician review only and does not support autonomous clinical use.
"""
    with open(OUTPUT_MARKDOWN_PATH, "w") as f:
        f.write(md_content)

    print(f"Report exported to {OUTPUT_REPORT_PATH} and {OUTPUT_MARKDOWN_PATH}.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the fixture benchmark or validate the AfriSwitch pilot.")
    parser.add_argument(
        "--afriswitch-pilot",
        action="store_true",
        help="Generate a reference-only report from the imported AfriSwitch pilot.",
    )
    parser.add_argument(
        "--afriswitch-root",
        default=str(AFRISWITCH_ROOT),
        help="Path to the imported AfriSwitch directory.",
    )
    args = parser.parse_args()
    if args.afriswitch_pilot:
        run_afriswitch_pilot(Path(args.afriswitch_root))
    else:
        asyncio.run(run_benchmark())
