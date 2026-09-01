from __future__ import annotations

import argparse
from pathlib import Path

from services.database_package import (
    create_database_package,
    materialize_database_package,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Empaqueta o restaura actas.db")
    subparsers = parser.add_subparsers(dest="command", required=True)

    restore = subparsers.add_parser("restore")
    restore.add_argument("--database", type=Path, required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--database", type=Path, required=True)
    create.add_argument("--part-size-mib", type=int, default=90)
    create.add_argument("--delete-source", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    if arguments.command == "restore":
        result = materialize_database_package(arguments.database, arguments.database)
        if result.exists():
            print(f"Índice restaurado: {result} ({result.stat().st_size} bytes)")
        else:
            print("No existe un índice anterior; se construirá desde cero.")
    else:
        result = create_database_package(
            arguments.database,
            part_size_bytes=arguments.part_size_mib * 1024 * 1024,
            delete_source=arguments.delete_source,
        )
        print(
            f"Índice empaquetado en {len(result['parts'])} parte(s): "
            f"{result['compressed_bytes']} bytes comprimidos"
        )
