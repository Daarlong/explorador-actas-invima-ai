from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.ann import build_ann_index
from services.database import initialize_database, insert_document, sync_regulatory_extractions
from services.database_package import create_database_package
from services.models import DocumentMetadata
from services.regulatory import RegulatoryRecord
from services.semantic import (
    DEFAULT_NEURAL_MODEL_ID,
    DEFAULT_NEURAL_MODEL_REVISION,
    build_semantic_index,
)
from validate_search import (
    load_validation_config,
    main,
    render_markdown_summary,
    resolve_config_path,
    resolve_manifest_path,
    validate_published_corpus,
)


class _Encoder:
    model_id = DEFAULT_NEURAL_MODEL_ID
    model_revision = DEFAULT_NEURAL_MODEL_REVISION

    def encode_passages(self, texts, *, batch_size):
        del batch_size
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    def encode_query(self, text):
        del text
        return [1.0, 0.0, 0.0, 0.0]


class ValidateSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "actas.db"
        self.semantic = self.root / "semantic.db"
        self.ann = self.root / "semantic-ann.db"
        initialize_database(self.database)
        for number in (1, 2):
            insert_document(
                self.database,
                DocumentMetadata(
                    title=f"Documento fuente {number}",
                    url=f"https://www.invima.gov.co/biblioteca/download/{number}",
                    year=2026,
                    acta_number=f"{number:02d}",
                    section="SEMPB",
                ),
                f"hash-{number}",
                [
                    {
                        "page": 7,
                        "text": (
                            "Solicitud técnica persistida para validación documental. "
                            "Concepto fuente con balance beneficio riesgo favorable."
                        ),
                        "chunks": [
                            "Solicitud técnica persistida para validación documental.",
                            "Concepto fuente con balance beneficio riesgo favorable.",
                        ],
                    }
                ],
            )
        record = RegulatoryRecord(
            producto="PRODUCTO DE PRUEBA",
            principio_activo="SUSTANCIA DE PRUEBA",
            interesado="INTERESADO DE PRUEBA",
            expediente="EXP-123",
            radicado="RAD-456",
            solicitud="Solicitud técnica persistida",
            concepto="Concepto fuente con balance beneficio riesgo favorable",
            resultado_normalizado="aprobado",
            pagina=7,
            pagina_final=7,
            tipo_solicitud="indicaciones",
        )
        with patch(
            "services.database.extract_regulatory_records",
            return_value=[record],
        ):
            sync_regulatory_extractions(self.database)
        create_database_package(
            self.database,
            part_size_bytes=4096,
            delete_source=False,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _config(self) -> dict:
        return {
            "format_version": 1,
            "schema": {
                "expected_version": 7,
                "allowed_document_hosts": ["www.invima.gov.co"],
                "temporal_coverage": {
                    "start_year_at_most": 2026,
                    "end_year_at_least": 2026,
                    "minimum_span_years": 1,
                    "minimum_distinct_years": 1,
                },
                "required_tables": [
                    "documents",
                    "pages",
                    "chunks",
                    "chunks_fts",
                    "regulatory_records",
                    "regulatory_records_fts",
                ],
                "minimum_rows": {
                    "documents": 2,
                    "pages": 2,
                    "chunks": 4,
                    "regulatory_records": 2,
                },
                "fts_cardinality": {"enabled": True},
                "regulatory_relationships": {"enabled": True},
            },
            "packages": {
                "required": ["actas.db"],
                "verify_sha256": True,
            },
            "semantic": {
                "required": False,
                "require_neural_ready": False,
                "ann_required": False,
            },
            "probes": {
                "candidate_rows": 20,
                "textual": {"enabled": True},
                "hybrid": {"enabled": False},
                "phrase": {"enabled": True, "words": 4},
                "fields": {
                    "enabled": True,
                    "scopes": ["active_ingredient", "concept"],
                    "minimum_successful_scopes": 2,
                },
                "pagination": {
                    "enabled": True,
                    "page_size": 1,
                    "minimum_documents": 2,
                },
                "export": {
                    "enabled": True,
                    "formats": ["csv", "xlsx"],
                    "maximum_results": 2,
                },
            },
            "explicit_cases": [],
        }

    def _manifest(self, *, include_missing: bool = False) -> Path:
        path = self.root / "documents_manifest.csv"
        rows = [
            "title,url,year,catalog_id",
            "Documento fuente 1,https://www.invima.gov.co/biblioteca/download/1,2026,",
            "Documento fuente 2,https://www.invima.gov.co/biblioteca/download/2,2026,",
        ]
        if include_missing:
            rows.append(
                "Documento fuente 3,https://www.invima.gov.co/biblioteca/download/3,2026,CAT-3"
            )
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        return path

    def test_runner_validates_searches_and_traceable_evidence(self) -> None:
        before = self.database.read_bytes()
        report = validate_published_corpus(
            database_path=self.database,
            semantic_path=self.semantic,
            ann_path=self.ann,
            config=self._config(),
        )

        self.assertEqual(report["status"], "passed")
        ids = {check["check_id"] for check in report["checks"]}
        self.assertIn("probe.textual", ids)
        self.assertIn("probe.phrase", ids)
        self.assertIn("probe.field.active_ingredient", ids)
        self.assertIn("probe.field.concept", ids)
        self.assertIn("probe.pagination", ids)
        self.assertIn("probe.export.csv", ids)
        self.assertIn("probe.export.xlsx", ids)
        self.assertIn("schema.temporal_coverage", ids)
        self.assertIn("schema.fts_cardinality", ids)
        self.assertIn("schema.regulatory_relationships", ids)
        self.assertEqual(report["summary"]["blocking_failures"], 0)
        self.assertEqual(self.database.read_bytes(), before)

    def test_runner_executes_real_neural_ann_hybrid_probe(self) -> None:
        encoder = _Encoder()
        build_semantic_index(
            self.database,
            self.semantic,
            lexical_dimension=32,
            semantic_dimension=16,
            min_df=1,
            neural_enabled=True,
            neural_model_id=encoder.model_id,
            neural_model_revision=encoder.model_revision,
            neural_encoder=encoder,
        )
        build_ann_index(
            self.semantic,
            self.ann,
            table_count=4,
            bits_per_table=4,
        )
        config = self._config()
        config["semantic"] = {
            "required": True,
            "require_neural_ready": True,
            "ann_required": True,
        }
        config["probes"]["hybrid"] = {
            "enabled": True,
            "candidate_limit": 30,
        }

        with (
            patch("services.semantic._fastembed_encoder", return_value=encoder),
            patch("services.semantic.neural_runtime_installed", return_value=True),
        ):
            report = validate_published_corpus(
                database_path=self.database,
                semantic_path=self.semantic,
                ann_path=self.ann,
                config=config,
            )

        hybrid = next(
            check for check in report["checks"] if check["check_id"] == "probe.hybrid"
        )
        self.assertEqual(report["status"], "passed")
        self.assertEqual(hybrid["status"], "passed")
        self.assertTrue(hybrid["details"]["ann_used"])
        self.assertTrue(hybrid["details"]["neural_used"])

    def test_explicit_relevance_case_is_only_advisory(self) -> None:
        config = self._config()
        config["explicit_cases"] = [
            {
                "name": "Sin coincidencias esperadas",
                "query": "terminoabsolutamenteinexistente",
                "minimum_results": 1,
            }
        ]

        report = validate_published_corpus(
            database_path=self.database,
            semantic_path=self.semantic,
            ann_path=self.ann,
            config=config,
        )

        explicit = next(
            check for check in report["checks"] if check["check_id"] == "explicit.1"
        )
        self.assertEqual(report["status"], "passed")
        self.assertEqual(explicit["status"], "warning")
        self.assertEqual(explicit["severity"], "advisory")

    def test_temporal_coverage_is_a_configurable_technical_contract(self) -> None:
        config = self._config()
        config["schema"]["temporal_coverage"]["end_year_at_least"] = 2027

        report = validate_published_corpus(
            database_path=self.database,
            semantic_path=self.semantic,
            ann_path=self.ann,
            config=config,
        )

        coverage = next(
            check
            for check in report["checks"]
            if check["check_id"] == "schema.temporal_coverage"
        )
        self.assertEqual(report["status"], "failed")
        self.assertEqual(coverage["status"], "failed")
        self.assertEqual(coverage["details"]["maximum_year"], 2026)

    def test_manifest_coverage_detects_a_truncated_corpus(self) -> None:
        config = self._config()
        config["manifest"] = {
            "enabled": True,
            "start_year": 2013,
            "require_no_missing": True,
        }

        complete = validate_published_corpus(
            database_path=self.database,
            semantic_path=self.semantic,
            ann_path=self.ann,
            config=config,
            manifest_path=self._manifest(),
        )
        complete_check = next(
            check
            for check in complete["checks"]
            if check["check_id"] == "manifest.coverage"
        )
        self.assertEqual(complete["status"], "passed")
        self.assertEqual(complete_check["details"]["expected"], 2)
        self.assertEqual(complete_check["details"]["missing"], 0)
        self.assertEqual(complete_check["details"]["coverage_percent"], 100.0)
        self.assertIn("Faltantes: **0**", render_markdown_summary(complete))

        truncated = validate_published_corpus(
            database_path=self.database,
            semantic_path=self.semantic,
            ann_path=self.ann,
            config=config,
            manifest_path=self._manifest(include_missing=True),
        )
        truncated_check = next(
            check
            for check in truncated["checks"]
            if check["check_id"] == "manifest.coverage"
        )
        self.assertEqual(truncated["status"], "failed")
        self.assertEqual(truncated_check["details"]["expected"], 3)
        self.assertEqual(truncated_check["details"]["missing"], 1)
        self.assertAlmostEqual(
            truncated_check["details"]["coverage_percent"], 66.6667
        )

    def test_manifest_prefers_catalog_id_and_falls_back_to_url(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE documents SET catalog_id = ? WHERE manifest_url = ?",
                (
                    "CAT-1",
                    "https://www.invima.gov.co/biblioteca/download/1",
                ),
            )
        manifest = self.root / "identity-manifest.csv"
        manifest.write_text(
            "title,url,year,catalog_id\n"
            "Documento movido,https://www.invima.gov.co/biblioteca/download/101,2026,CAT-1\n"
            "Documento fuente 2,https://www.invima.gov.co/biblioteca/download/2,2026,\n",
            encoding="utf-8",
        )
        config = self._config()
        config["manifest"] = {
            "enabled": True,
            "start_year": 2013,
            "require_no_missing": True,
        }
        config["packages"]["required"] = []

        report = validate_published_corpus(
            database_path=self.database,
            semantic_path=self.semantic,
            ann_path=self.ann,
            config=config,
            manifest_path=manifest,
        )

        coverage = next(
            check
            for check in report["checks"]
            if check["check_id"] == "manifest.coverage"
        )
        self.assertEqual(report["status"], "passed")
        self.assertEqual(coverage["details"]["matched"], 2)
        self.assertEqual(
            coverage["details"]["identity_rule"],
            "catalog_id_then_manifest_url",
        )

    def test_manifest_temporal_contract_rejects_a_stale_complete_inventory(self) -> None:
        manifest = self.root / "stale-manifest.csv"
        manifest.write_text(
            "title,url,year,catalog_id\n"
            "Documento fuente 1,https://www.invima.gov.co/biblioteca/download/1,2020,\n"
            "Documento fuente 2,https://www.invima.gov.co/biblioteca/download/2,2026,\n",
            encoding="utf-8",
        )
        config = self._config()
        config["packages"]["required"] = []
        config["manifest"] = {
            "enabled": True,
            "start_year": 2013,
            "require_no_missing": True,
            "temporal_coverage": {
                "start_year_at_most": 2013,
                "end_year_at_least": 2026,
                "minimum_distinct_years": 14,
            },
        }

        report = validate_published_corpus(
            database_path=self.database,
            semantic_path=self.semantic,
            ann_path=self.ann,
            config=config,
            manifest_path=manifest,
        )

        coverage = next(
            check
            for check in report["checks"]
            if check["check_id"] == "manifest.coverage"
        )
        temporal = next(
            check
            for check in report["checks"]
            if check["check_id"] == "manifest.temporal_coverage"
        )
        self.assertEqual(coverage["status"], "passed")
        self.assertEqual(coverage["details"]["matched"], 2)
        self.assertEqual(temporal["status"], "failed")
        self.assertEqual(temporal["details"]["minimum_year"], 2020)
        self.assertEqual(temporal["details"]["maximum_year"], 2026)
        self.assertEqual(temporal["details"]["distinct_years"], 2)
        self.assertEqual(report["status"], "failed")
        self.assertIn("**2020–2026**", render_markdown_summary(report))

    def test_manifest_requires_each_expected_document_to_be_searchable(self) -> None:
        with sqlite3.connect(self.database) as connection:
            document_id = int(
                connection.execute(
                    "SELECT id FROM documents WHERE manifest_url = ?",
                    ("https://www.invima.gov.co/biblioteca/download/2",),
                ).fetchone()[0]
            )
            connection.execute(
                "DELETE FROM chunks WHERE page_id IN "
                "(SELECT id FROM pages WHERE document_id = ?)",
                (document_id,),
            )
            connection.execute(
                "DELETE FROM pages WHERE document_id = ?", (document_id,)
            )
            connection.execute(
                "UPDATE documents SET indexed_page_count = 0, "
                "page_inventory_complete = 0 WHERE id = ?",
                (document_id,),
            )
        config = self._config()
        config["packages"]["required"] = []
        config["manifest"] = {
            "enabled": True,
            "start_year": 2013,
            "require_no_missing": True,
        }

        report = validate_published_corpus(
            database_path=self.database,
            semantic_path=self.semantic,
            ann_path=self.ann,
            config=config,
            manifest_path=self._manifest(),
        )

        coverage = next(
            check
            for check in report["checks"]
            if check["check_id"] == "manifest.coverage"
        )
        self.assertEqual(coverage["status"], "failed")
        self.assertEqual(coverage["details"]["expected"], 2)
        self.assertEqual(coverage["details"]["matched"], 1)
        self.assertEqual(coverage["details"]["indexed"], 1)
        self.assertEqual(coverage["details"]["indexed_rows_total"], 2)
        self.assertEqual(coverage["details"]["missing"], 1)
        self.assertEqual(coverage["details"]["unsearchable"], 1)
        self.assertEqual(
            coverage["details"]["missing_examples"][0]["reason"],
            "indexed_but_not_searchable",
        )
        self.assertIn(
            "Filas presentes pero no consultables: **1**",
            render_markdown_summary(report),
        )

    def test_fts_cardinality_mismatch_is_a_technical_failure(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO chunks_fts(rowid, title, text) VALUES (?, ?, ?)",
                (999_999, "registro espurio", "registro espurio"),
            )

        report = validate_published_corpus(
            database_path=self.database,
            semantic_path=self.semantic,
            ann_path=self.ann,
            config=self._config(),
        )
        cardinality = next(
            check
            for check in report["checks"]
            if check["check_id"] == "schema.fts_cardinality"
        )
        self.assertEqual(report["status"], "failed")
        self.assertEqual(cardinality["status"], "failed")
        self.assertEqual(
            cardinality["details"]["chunks_fts"],
            cardinality["details"]["chunks"] + 1,
        )

    def test_package_corruption_is_a_blocking_technical_failure(self) -> None:
        part = next(self.root.glob("actas.db.gz.part-*"))
        part.write_bytes(part.read_bytes() + b"corruption")

        report = validate_published_corpus(
            database_path=self.database,
            semantic_path=self.semantic,
            ann_path=self.ann,
            config=self._config(),
        )

        self.assertEqual(report["status"], "failed")
        package = next(
            check for check in report["checks"] if check["check_id"] == "package.actas.db"
        )
        self.assertEqual(package["severity"], "required")
        self.assertEqual(package["status"], "failed")

    def test_cli_writes_json_and_markdown_reports(self) -> None:
        config_path = self.root / "cases.json"
        report_path = self.root / "report.json"
        summary_path = self.root / "summary.md"
        config_path.write_text(
            json.dumps(self._config(), ensure_ascii=False), encoding="utf-8"
        )

        exit_code = main(
            [
                "--database",
                str(self.database),
                "--semantic",
                str(self.semantic),
                "--ann",
                str(self.ann),
                "--config",
                str(config_path),
                "--report",
                str(report_path),
                "--summary",
                str(summary_path),
            ]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(report_path.read_text())["status"], "passed")
        self.assertIn("Validación técnica de búsquedas", summary_path.read_text())

    def test_invalid_configuration_produces_a_controlled_failure(self) -> None:
        config_path = self.root / "invalid.json"
        report_path = self.root / "report.json"
        config_path.write_text('{"format_version": 99}', encoding="utf-8")

        exit_code = main(
            ["--config", str(config_path), "--report", str(report_path)]
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(report_path.read_text())["status"], "failed")

    def test_config_loader_rejects_missing_sections(self) -> None:
        path = self.root / "missing.json"
        path.write_text('{"format_version": 1}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Falta la sección"):
            load_validation_config(path)

    def test_workflow_config_path_cannot_escape_its_allowed_root(self) -> None:
        allowed = self.root / "checkout"
        allowed.mkdir()
        inside = allowed / "cases.json"
        inside.write_text("{}", encoding="utf-8")

        self.assertEqual(
            resolve_config_path(Path("cases.json"), allowed), inside.resolve()
        )
        with self.assertRaisesRegex(ValueError, "relativa"):
            resolve_config_path(inside.resolve(), allowed)
        with self.assertRaisesRegex(ValueError, "checkout"):
            resolve_config_path(Path("../cases.json"), allowed)
        with self.assertRaisesRegex(ValueError, "JSON"):
            resolve_config_path(Path("cases.yml"), allowed)

        manifest = allowed / "manifest.csv"
        manifest.write_text("title,url\n", encoding="utf-8")
        self.assertEqual(
            resolve_manifest_path(Path("manifest.csv"), allowed),
            manifest.resolve(),
        )
        with self.assertRaisesRegex(ValueError, "checkout"):
            resolve_manifest_path(Path("../manifest.csv"), allowed)
        with self.assertRaisesRegex(ValueError, "CSV"):
            resolve_manifest_path(Path("manifest.json"), allowed)


class ValidateSearchWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "validate-search.yml"
        ).read_text(encoding="utf-8")

    def test_workflow_is_manual_read_only_and_restores_three_packages(self) -> None:
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertIn("contents: read", self.workflow)
        self.assertNotIn("contents: write", self.workflow)
        self.assertNotIn("git push", self.workflow)
        for database in ("actas.db", "semantic.db", "semantic-ann.db"):
            self.assertIn(
                f"package_index.py restore --database data/{database}", self.workflow
            )

    def test_workflow_always_publishes_report_and_summarizes_result(self) -> None:
        self.assertIn("python validate_search.py", self.workflow)
        self.assertIn('--summary "$GITHUB_STEP_SUMMARY"', self.workflow)
        self.assertIn("if: ${{ always() }}", self.workflow)
        self.assertIn("actions/upload-artifact@v6", self.workflow)
        self.assertIn("search-validation-report-${{ github.run_id }}", self.workflow)
        self.assertIn("Restaurar modelo semántico multilingüe", self.workflow)
        self.assertIn(
            "VALIDATION_CONFIG_PATH: ${{ inputs.config_path }}", self.workflow
        )
        self.assertIn(
            "VALIDATION_MANIFEST_PATH: ${{ inputs.manifest_path }}", self.workflow
        )
        self.assertIn('--config "$VALIDATION_CONFIG_PATH"', self.workflow)
        self.assertIn('--config-root "$GITHUB_WORKSPACE"', self.workflow)
        self.assertIn('--manifest "$VALIDATION_MANIFEST_PATH"', self.workflow)
        self.assertIn('--manifest-root "$GITHUB_WORKSPACE"', self.workflow)
        self.assertNotIn('--config "${{ inputs.config_path }}"', self.workflow)
        self.assertNotIn('--manifest "${{ inputs.manifest_path }}"', self.workflow)

    def test_official_cases_enable_hybrid_export_and_temporal_coverage(self) -> None:
        config = load_validation_config(
            Path(__file__).resolve().parents[1] / "search-validation-cases.json"
        )

        self.assertTrue(config["probes"]["hybrid"]["enabled"])
        self.assertTrue(config["probes"]["export"]["enabled"])
        self.assertTrue(config["manifest"]["enabled"])
        self.assertEqual(
            config["manifest"]["temporal_coverage"]["start_year_at_most"],
            2013,
        )
        self.assertEqual(
            config["manifest"]["temporal_coverage"]["end_year_at_least"],
            2026,
        )
        self.assertEqual(
            config["manifest"]["temporal_coverage"]["minimum_distinct_years"],
            14,
        )
        self.assertTrue(config["schema"]["fts_cardinality"]["enabled"])
        self.assertTrue(config["schema"]["regulatory_relationships"]["enabled"])
        self.assertEqual(
            config["schema"]["temporal_coverage"]["start_year_at_most"],
            2013,
        )
        self.assertIn("xlsx", config["probes"]["export"]["formats"])

    def test_build_workflow_validates_candidate_before_committing_it(self) -> None:
        build_workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "build-index.yml"
        ).read_text(encoding="utf-8")
        validation = build_workflow.index(
            "name: Validar búsquedas sobre la publicación candidata"
        )
        commit = build_workflow.index(
            "name: Guardar índice e informe en el repositorio privado"
        )

        self.assertLess(validation, commit)
        block = build_workflow[validation:commit]
        self.assertIn("python validate_search.py", block)
        self.assertIn("--database .package-smoke/actas.db", block)
        self.assertIn("--summary \"$GITHUB_STEP_SUMMARY\"", block)
        self.assertIn("--manifest documents_manifest.csv", block)
        self.assertIn('--manifest-root "$GITHUB_WORKSPACE"', block)
        self.assertIn('config["explicit_cases"] = []', block)


if __name__ == "__main__":
    unittest.main()
