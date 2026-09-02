from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from config import (
    DATABASE_PATH,
    EVALUATION_CASES_PATH,
    EVALUATION_REPORT_PATH,
    SEMANTIC_INDEX_PATH,
)
from services.evaluation import (
    evaluate_cases,
    load_evaluation_cases,
    summarize_evaluation,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evalúa los tres modos contra el banco regulatorio"
    )
    parser.add_argument("--cases", type=Path, default=EVALUATION_CASES_PATH)
    parser.add_argument("--output", type=Path, default=EVALUATION_REPORT_PATH)
    parser.add_argument("--allow-empty", action="store_true")
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
    cases, errors = load_evaluation_cases(arguments.cases)
    enabled = [case for case in cases if case.enabled]
    if errors:
        raise SystemExit("El banco de evaluación contiene errores: " + " | ".join(errors))
    if not enabled and not arguments.allow_empty:
        raise SystemExit("El banco no contiene casos habilitados")

    results = evaluate_cases(
        DATABASE_PATH,
        SEMANTIC_INDEX_PATH,
        enabled,
        ("textual", "hybrid", "semantic"),
    ) if enabled else []
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "evaluated" if results else "empty",
        "cases_total": len(cases),
        "cases_enabled": len(enabled),
        "summary": summarize_evaluation(results),
        "results": [result.as_dict() for result in results],
    }
    write_report(arguments.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
