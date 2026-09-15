from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.ann import (
    DEFAULT_BITS_PER_TABLE,
    DEFAULT_BUILD_BATCH_SIZE,
    DEFAULT_TABLE_COUNT,
    ann_index_status,
    build_ann_index,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Construye el índice ANN global reutilizando los embeddings de "
            "semantic.db"
        )
    )
    parser.add_argument("--semantic", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tables", type=int, default=DEFAULT_TABLE_COUNT)
    parser.add_argument("--bits", type=int, default=DEFAULT_BITS_PER_TABLE)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BUILD_BATCH_SIZE)
    parser.add_argument(
        "--status",
        action="store_true",
        help="Solo valida la correspondencia del índice existente",
    )
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    if arguments.status:
        result = ann_index_status(arguments.output, arguments.semantic)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        if not result.get("available"):
            raise SystemExit(1)
    else:
        summary = build_ann_index(
            arguments.semantic,
            arguments.output,
            table_count=arguments.tables,
            bits_per_table=arguments.bits,
            batch_size=arguments.batch_size,
        )
        print(
            json.dumps(
                summary.as_dict(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
