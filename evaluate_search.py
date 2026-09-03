from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from config import (
    DATABASE_PATH,
    EVALUATION_CASES_PATH,
    EVALUATION_REPORT_PATH,
    INTEGRITY_REPORT_PATH,
    SEMANTIC_INDEX_PATH,
)
from services.evaluation import (
    RELEASE_MIN_ENABLED_CASES,
    RELEASE_MIN_EXPECTED_DECISIONS,
    build_quality_gate,
    compare_extractor_quality,
    compare_retrieval_summaries,
    evaluate_cases,
    evaluation_bank_profile,
    evaluation_bank_signature,
    load_evaluation_report,
    load_evaluation_cases,
    summarize_evaluation,
)
from services.integrity import (
    build_regulatory_quality_snapshot,
    load_integrity_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evalúa los tres modos contra el banco regulatorio"
    )
    parser.add_argument("--cases", type=Path, default=EVALUATION_CASES_PATH)
    parser.add_argument("--output", type=Path, default=EVALUATION_REPORT_PATH)
    parser.add_argument("--allow-empty", action="store_true")
    parser.add_argument(
        "--release",
        action="store_true",
        help="Activa el gate bloqueante de publicación de la versión candidata.",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        help="Reporte generado contra la base publicada antes del reprocesamiento.",
    )
    parser.add_argument(
        "--integrity-report",
        type=Path,
        default=INTEGRITY_REPORT_PATH,
    )
    parser.add_argument(
        "--minimum-enabled-cases",
        type=int,
        default=RELEASE_MIN_ENABLED_CASES,
    )
    parser.add_argument(
        "--minimum-expected-decisions",
        type=int,
        default=RELEASE_MIN_EXPECTED_DECISIONS,
    )
    parser.add_argument(
        "--max-retrieval-drop",
        type=float,
        default=0.0,
        help="Caída máxima absoluta tolerada en Hit@K, Recall@K y MRR.",
    )
    parser.add_argument(
        "--max-completeness-drop",
        type=float,
        default=0.0,
        help="Caída máxima tolerada, en puntos porcentuales, por campo.",
    )
    return parser.parse_args()


def write_report(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


if __name__ == "__main__":
    arguments = parse_args()
    if arguments.minimum_enabled_cases < 1:
        raise SystemExit("--minimum-enabled-cases debe ser mayor que cero")
    if arguments.minimum_expected_decisions < 1:
        raise SystemExit("--minimum-expected-decisions debe ser mayor que cero")
    if arguments.max_retrieval_drop < 0 or arguments.max_completeness_drop < 0:
        raise SystemExit("Los umbrales de caída no pueden ser negativos")

    baseline_report = (
        load_evaluation_report(arguments.baseline) if arguments.baseline else None
    )
    cases, errors = load_evaluation_cases(arguments.cases)
    enabled = [case for case in cases if case.enabled]

    results = evaluate_cases(
        DATABASE_PATH,
        SEMANTIC_INDEX_PATH,
        enabled,
        ("textual", "hybrid", "semantic"),
    ) if enabled and not errors else []
    bank_signature = evaluation_bank_signature(cases)
    summary = summarize_evaluation(results)
    extractor_quality = build_regulatory_quality_snapshot(DATABASE_PATH)
    extractor_comparison = compare_extractor_quality(
        extractor_quality,
        baseline_report,
    )
    retrieval_comparison = compare_retrieval_summaries(
        summary,
        (baseline_report or {}).get("summary")
        if isinstance(baseline_report, dict)
        else None,
        current_bank_signature=bank_signature,
        baseline_bank_signature=(baseline_report or {}).get("bank_signature")
        if isinstance(baseline_report, dict)
        else None,
    )
    integrity_report = load_integrity_report(arguments.integrity_report)
    quality_gate = build_quality_gate(
        release_mode=arguments.release,
        cases=cases,
        results=results,
        extractor_quality=extractor_quality,
        extractor_comparison=extractor_comparison,
        retrieval_comparison=retrieval_comparison,
        integrity_report=integrity_report,
        bank_errors=errors,
        minimum_enabled_cases=arguments.minimum_enabled_cases,
        minimum_expected_decisions=arguments.minimum_expected_decisions,
        max_retrieval_drop=arguments.max_retrieval_drop,
        max_completeness_drop_pp=arguments.max_completeness_drop,
    )
    report = {
        "report_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": (
            "invalid_bank"
            if errors
            else ("evaluated" if results else "empty")
        ),
        "release_mode": arguments.release,
        "cases_total": len(cases),
        "cases_enabled": len(enabled),
        "bank_errors": errors,
        "bank_profile": evaluation_bank_profile(cases),
        "bank_signature": bank_signature,
        "summary": summary,
        "results": [result.as_dict() for result in results],
        "extractor_quality": extractor_quality,
        "extractor_comparison": extractor_comparison,
        "retrieval_comparison": retrieval_comparison,
        "quality_gate": quality_gate,
    }
    write_report(arguments.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)
    if not enabled and not arguments.allow_empty and not arguments.release:
        raise SystemExit(1)
    if arguments.release and not quality_gate["can_publish"]:
        raise SystemExit(1)
