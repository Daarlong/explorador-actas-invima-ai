from __future__ import annotations

import argparse
import json
import sqlite3
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

    verify = subparsers.add_parser("verify")
    verify.add_argument("--database", type=Path, required=True)
    verify.add_argument("--target", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    if arguments.command == "restore":
        result = materialize_database_package(arguments.database, arguments.database)
        if result.exists():
            print(f"Índice restaurado: {result} ({result.stat().st_size} bytes)")
        else:
            print("No existe un índice anterior; se construirá desde cero.")
    elif arguments.command == "create":
        result = create_database_package(
            arguments.database,
            part_size_bytes=arguments.part_size_mib * 1024 * 1024,
            delete_source=arguments.delete_source,
        )
        print(
            f"Índice empaquetado en {len(result['parts'])} parte(s): "
            f"{result['compressed_bytes']} bytes comprimidos"
        )
    else:
        package_exists = any(
            (
                arguments.database.with_name(
                    f"{arguments.database.name}.package.json"
                ).exists(),
                arguments.database.with_name(
                    f"{arguments.database.name}.gz"
                ).exists(),
                any(
                    arguments.database.parent.glob(
                        f"{arguments.database.name}.gz.part-*"
                    )
                ),
            )
        )
        if not package_exists:
            raise SystemExit("No existe un paquete para verificar")
        if arguments.target.exists():
            raise SystemExit(
                "El destino de verificación ya existe; usa una ruta nueva"
            )
        restored = materialize_database_package(
            arguments.database,
            arguments.target,
        )
        if not restored.exists():
            raise SystemExit("No existe un paquete para verificar")
        try:
            with sqlite3.connect(restored) as connection:
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        except sqlite3.DatabaseError as exc:
            raise SystemExit(f"El paquete no contiene una base SQLite válida: {exc}")
        if integrity.lower() != "ok":
            raise SystemExit(f"La base restaurada no supera integridad: {integrity}")
        print(
            json.dumps(
                {
                    "database": str(arguments.database),
                    "restored": str(restored),
                    "bytes": restored.stat().st_size,
                    "sqlite_integrity": integrity,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
