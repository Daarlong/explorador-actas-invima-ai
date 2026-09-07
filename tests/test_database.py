import tempfile
import unittest
import sqlite3
import zlib
from difflib import SequenceMatcher as StandardSequenceMatcher
from pathlib import Path
from unittest.mock import patch

from services.database import (
    DATABASE_SCHEMA_VERSION,
    REGULATORY_EXTRACTOR_VERSION,
    _mutual_scored_matches,
    _precompute_record_link_scores,
    _record_link_score,
    _record_match_has_evidence,
    _regulatory_record_key,
    connect,
    count_regulatory_records,
    database_schema_version,
    database_stats,
    extraction_uid_reconciliation,
    get_filter_options,
    initialize_database,
    insert_document,
    list_regulatory_records,
    migrate_database_schema,
    optimize_database,
    regulatory_records_for_chunks,
    reconcile_database_decision_uids,
    search_chunks,
    source_pages_for_document,
    sync_regulatory_extractions,
    table_exists,
)
from services.models import DocumentMetadata
from services.regulatory import FieldEvidence, RegulatoryRecord
from services.reviews import new_review_event


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "test.db"
        initialize_database(self.database_path)
        insert_document(
            self.database_path,
            DocumentMetadata(
                title="Acta No 01 de 2026 SEMPB Primera Parte",
                url="https://www.invima.gov.co/biblioteca/download/1",
                year=2026,
                acta_number="01",
                section="SEMPB",
                part="Primera Parte",
            ),
            "hash-1",
            [
                {
                    "page": 7,
                    "text": "La comisión evaluó semaglutida para el trámite.",
                    "chunks": ["La comisión evaluó semaglutida para el trámite."],
                }
            ],
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_search_returns_page_and_source(self) -> None:
        results = search_chunks(self.database_path, "semaglutida")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].page, 7)
        self.assertEqual(results[0].acta_number, "01")

    def test_compact_schema_and_optimization_keep_search_working(self) -> None:
        with connect(self.database_path) as connection:
            page_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(pages)")
            }
            chunk_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(chunks)")
            }
            fts_sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'chunks_fts'"
                ).fetchone()[0]
            ).lower()
        self.assertNotIn("text", page_columns)
        self.assertNotIn("normalized_text", chunk_columns)
        self.assertIn("content=''", fts_sql)
        self.assertEqual(database_schema_version(self.database_path), DATABASE_SCHEMA_VERSION)

        optimize_database(self.database_path)
        self.assertEqual(len(search_chunks(self.database_path, "semaglutida")), 1)

    def test_preserves_original_page_text_and_source_metadata(self) -> None:
        source_pages = source_pages_for_document(self.database_path, 1)

        self.assertEqual(
            source_pages[0]["text"],
            "La comisión evaluó semaglutida para el trámite.",
        )
        self.assertEqual(source_pages[0]["text_source"], "native_pdf")
        with connect(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT raw_text_compressed, raw_text_codec, text_source,
                       text_extractor_version
                FROM pages WHERE document_id = 1
                """
            ).fetchone()
        self.assertIsInstance(row["raw_text_compressed"], bytes)
        self.assertEqual(row["raw_text_codec"], "zlib-utf8-v1")
        self.assertEqual(row["text_source"], "native_pdf")
        self.assertTrue(row["text_extractor_version"])

    def test_source_text_keeps_line_breaks_and_ocr_metadata(self) -> None:
        insert_document(
            self.database_path,
            DocumentMetadata(
                title="Acta OCR",
                url="https://www.invima.gov.co/biblioteca/download/ocr",
            ),
            "hash-ocr",
            [
                {
                    "page": 1,
                    "text": "Producto: ALFA\nPrincipio activo: Semaglutida",
                    "chunks": ["Producto: ALFA Principio activo: Semaglutida"],
                    "ocr_used": True,
                    "text_quality": 0.82,
                    "extraction_error": "rotación corregida",
                    "text_extractor_version": "ocr-v2",
                }
            ],
        )

        pages = source_pages_for_document(self.database_path, 2)

        self.assertEqual(
            pages[0]["text"],
            "Producto: ALFA\nPrincipio activo: Semaglutida",
        )
        self.assertEqual(pages[0]["text_source"], "ocr")
        self.assertEqual(pages[0]["text_quality"], 0.82)
        self.assertEqual(pages[0]["extraction_error"], "rotación corregida")
        self.assertEqual(pages[0]["text_extractor_version"], "ocr-v2")

    def test_persists_failed_page_inventory_without_fabricating_text(self) -> None:
        document_id = insert_document(
            self.database_path,
            DocumentMetadata(
                title="Acta con página no legible",
                url="https://www.invima.gov.co/biblioteca/download/fallo",
            ),
            "hash-fallo",
            [
                {
                    "page": 1,
                    "text": None,
                    "chunks": [],
                    "text_source": None,
                    "text_quality": 0.0,
                    "extraction_error": "OCR falló: tiempo agotado",
                    "text_extractor_version": "pymupdf-text-v1+tesseract-ocr-v1",
                }
            ],
            pdf_page_count=1,
            possible_scans=[1],
        )

        with connect(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT raw_text_compressed, raw_text_codec, text_source,
                       text_quality, extraction_error, text_extractor_version
                FROM pages WHERE document_id = ?
                """,
                (document_id,),
            ).fetchone()

        self.assertIsNone(row["raw_text_compressed"])
        self.assertIsNone(row["raw_text_codec"])
        self.assertIsNone(row["text_source"])
        self.assertEqual(row["text_quality"], 0.0)
        self.assertIn("tiempo agotado", row["extraction_error"])
        self.assertIn("tesseract-ocr-v1", row["text_extractor_version"])
        self.assertEqual(source_pages_for_document(self.database_path, document_id), [])

    def test_regulatory_backfill_prefers_source_text_over_chunks(self) -> None:
        original = "Producto: ALFA\nPrincipio activo: Semaglutida"
        with connect(self.database_path) as connection:
            connection.execute(
                "UPDATE pages SET raw_text_compressed = ?, raw_text_codec = ?",
                (zlib.compress(original.encode("utf-8")), "zlib-utf8-v1"),
            )
        with patch(
            "services.database.extract_regulatory_records", return_value=[]
        ) as extractor:
            summary = sync_regulatory_extractions(self.database_path)

        self.assertEqual(summary["documents_processed"], 1)
        self.assertEqual(extractor.call_args.args[0][0]["text"], original)
        with connect(self.database_path) as connection:
            method = connection.execute(
                "SELECT extractor_version, status FROM document_extractions"
            ).fetchone()
        self.assertEqual(
            tuple(method), (REGULATORY_EXTRACTOR_VERSION, "complete")
        )

    def test_corrupt_source_text_degrades_to_legacy_chunks(self) -> None:
        with connect(self.database_path) as connection:
            connection.execute(
                "UPDATE pages SET raw_text_compressed = X'0102', "
                "raw_text_codec = 'zlib-utf8-v1'"
            )
        with patch(
            "services.database.extract_regulatory_records", return_value=[]
        ) as extractor:
            summary = sync_regulatory_extractions(self.database_path)

        self.assertEqual(summary["documents_failed"], 0)
        self.assertEqual(
            extractor.call_args.args[0][0]["text"],
            "La comisión evaluó semaglutida para el trámite.",
        )

    def test_stores_field_evidence_and_reconciles_uid_by_expediente(self) -> None:
        initial = RegulatoryRecord(
            producto=None,
            principio_activo=None,
            interesado=None,
            expediente="EXP-2018-01",
            radicado=None,
            solicitud="Solicitud inicial",
            concepto="La Sala requiere información.",
            resultado_normalizado="requerido",
            pagina=7,
        )
        enhanced = RegulatoryRecord(
            producto="OZEMPIC",
            principio_activo="Semaglutida",
            interesado=None,
            expediente="EXP-2018-01",
            radicado=None,
            solicitud="Solicitud inicial ajustada",
            concepto="La Sala requiere información adicional.",
            resultado_normalizado="requerido",
            pagina=7,
            numeral="3.1.2",
            principios_activos=("Semaglutida",),
            principios_activos_normalizados=("semaglutida",),
            principios_activos_canonicos=("Semaglutida",),
            evidencias_campos=(
                FieldEvidence(
                    campo="principio_activo",
                    valor_literal="Semaglutida",
                    valor_normalizado="semaglutida",
                    valor_canonico="Semaglutida",
                    pagina=7,
                    pagina_final=7,
                    fragmento="Principio activo: Semaglutida",
                    metodo="etiqueta_explicita",
                    confianza=0.99,
                ),
            ),
        )
        with patch(
            "services.database.extract_regulatory_records", return_value=[initial]
        ):
            sync_regulatory_extractions(self.database_path, extractor_version="3-test")
        with connect(self.database_path) as connection:
            original_uid = connection.execute(
                "SELECT decision_uid FROM regulatory_records"
            ).fetchone()[0]

        with patch(
            "services.database.extract_regulatory_records", return_value=[enhanced]
        ):
            summary = sync_regulatory_extractions(
                self.database_path, extractor_version="4-test"
            )

        with connect(self.database_path) as connection:
            record = connection.execute(
                "SELECT decision_uid, identity_strategy FROM regulatory_records"
            ).fetchone()
            evidence = connection.execute(
                """
                SELECT field_name, literal_value, normalized_value,
                       canonical_value, page_number, extraction_method, confidence
                FROM regulatory_field_evidence
                """
            ).fetchone()
        self.assertEqual(record["decision_uid"], original_uid)
        self.assertEqual(record["identity_strategy"], "reconciled_v6")
        self.assertEqual(summary["uids_reconciled"], 1)
        self.assertEqual(summary["uids_orphaned"], 0)
        self.assertEqual(
            tuple(evidence)[:6],
            (
                "principio_activo",
                "Semaglutida",
                "semaglutida",
                "Semaglutida",
                7,
                "etiqueta_explicita",
            ),
        )
        self.assertAlmostEqual(evidence["confidence"], 0.99)
        self.assertEqual(
            extraction_uid_reconciliation(summary),
            {
                "uids_reconciled": 1,
                "uids_generated": 0,
                "uids_ambiguous": 0,
                "uids_orphaned": 0,
            },
        )

    def test_store_renumbers_colliding_evidence_without_losing_sources(self) -> None:
        record = RegulatoryRecord(
            producto="MEDICAMENTO HISTÓRICO",
            principio_activo="Semaglutida; Cagrilintida",
            interesado=None,
            expediente="EXP-HIST-1",
            radicado=None,
            solicitud="Registro sanitario",
            concepto="La Sala aprueba.",
            resultado_normalizado="aprobado",
            pagina=70,
            evidencias_campos=(
                FieldEvidence(
                    campo="principio_activo",
                    valor_literal="Semaglutida 1 mg",
                    valor_normalizado="semaglutida 1 mg",
                    valor_canonico="Semaglutida",
                    pagina=70,
                    pagina_final=70,
                    fragmento="Cada tableta contiene Semaglutida 1 mg",
                    metodo="dosage_statement",
                    confianza=0.94,
                    ordinal=1,
                ),
                FieldEvidence(
                    campo="principio_activo",
                    valor_literal="Cagrilintida 2 mg",
                    valor_normalizado="cagrilintida 2 mg",
                    valor_canonico="Cagrilintida",
                    pagina=71,
                    pagina_final=71,
                    fragmento="Cada tableta contiene Cagrilintida 2 mg",
                    metodo="dosage_statement",
                    confianza=0.93,
                    ordinal=1,
                ),
            ),
        )

        with patch(
            "services.database.extract_regulatory_records", return_value=[record]
        ):
            summary = sync_regulatory_extractions(
                self.database_path, extractor_version="4-colliding-evidence"
            )

        with connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT field_name, ordinal, canonical_value, page_number
                FROM regulatory_field_evidence
                ORDER BY field_name, ordinal
                """
            ).fetchall()
        self.assertEqual(summary["documents_failed"], 0)
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                ("principio_activo", 1, "Semaglutida", 70),
                ("principio_activo", 2, "Cagrilintida", 71),
            ],
        )

    def test_record_key_ignores_v4_evidence_only_changes(self) -> None:
        base = RegulatoryRecord(
            producto="OZEMPIC",
            principio_activo="Semaglutida",
            interesado="Ejemplo",
            expediente="EXP-1",
            radicado="RAD-1",
            solicitud="Solicitud",
            concepto="Concepto",
            resultado_normalizado="aprobado",
            pagina=7,
        )
        enriched = RegulatoryRecord(
            **{
                **{
                    field: getattr(base, field)
                    for field in (
                        "producto",
                        "principio_activo",
                        "interesado",
                        "expediente",
                        "radicado",
                        "solicitud",
                        "concepto",
                        "resultado_normalizado",
                        "pagina",
                    )
                },
                "principios_activos": ("Semaglutida",),
                "principios_activos_normalizados": ("semaglutida",),
                "principios_activos_canonicos": ("Semaglutida",),
                "evidencias_campos": (
                    FieldEvidence(
                        campo="principio_activo",
                        valor_literal="Semaglutida",
                        valor_normalizado="semaglutida",
                        valor_canonico="Semaglutida",
                        pagina=7,
                        pagina_final=7,
                        fragmento="Principio activo: Semaglutida",
                        metodo="etiqueta_explicita",
                        confianza=0.99,
                    ),
                ),
            }
        )

        self.assertEqual(
            _regulatory_record_key(base), _regulatory_record_key(enriched)
        )

    def test_reconciles_rebuilt_database_and_preserves_review_uid(self) -> None:
        baseline_path = Path(self.temp_dir.name) / "baseline.db"
        candidate_path = Path(self.temp_dir.name) / "candidate.db"
        catalog_id = "2018-sempb-01-completa"

        baseline_records = [
            RegulatoryRecord(
                producto=None,
                principio_activo=None,
                interesado="Novo Nordisk Colombia S.A.S",
                expediente="EXP-100",
                radicado=None,
                solicitud="Solicitud histórica",
                concepto="La Sala requiere información.",
                resultado_normalizado="requerido",
                pagina=7,
            ),
            RegulatoryRecord(
                producto="BETA",
                principio_activo="Activo B",
                interesado=None,
                expediente="EXP-200",
                radicado="RAD-200",
                solicitud="Solicitud B",
                concepto="La Sala aprueba.",
                resultado_normalizado="aprobado",
                pagina=8,
            ),
            RegulatoryRecord(
                producto="RETIRADO",
                principio_activo=None,
                interesado=None,
                expediente="EXP-300",
                radicado="RAD-300",
                solicitud="Solicitud C",
                concepto="La Sala archiva.",
                resultado_normalizado="archivado",
                pagina=9,
            ),
        ]
        candidate_records = [
            RegulatoryRecord(
                producto="OZEMPIC",
                principio_activo="Semaglutida",
                interesado="Novo Nordisk Colombia S.A.S",
                expediente="EXP-100",
                radicado=None,
                solicitud="Solicitud histórica ajustada",
                concepto="La Sala requiere información adicional.",
                resultado_normalizado="requerido",
                pagina=7,
                numeral="3.1.2",
            ),
            baseline_records[1],
            RegulatoryRecord(
                producto="NUEVO",
                principio_activo="Activo D",
                interesado=None,
                expediente="EXP-400",
                radicado="RAD-400",
                solicitud="Solicitud D",
                concepto="La Sala aprueba.",
                resultado_normalizado="aprobado",
                pagina=10,
            ),
        ]

        def build_database(
            path: Path, url: str, records: list[RegulatoryRecord]
        ) -> None:
            initialize_database(path)
            insert_document(
                path,
                DocumentMetadata(
                    title="Acta No 01 de 2018 SEMPB",
                    url=url,
                    year=2018,
                    acta_number="01",
                    section="SEMPB",
                    catalog_id=catalog_id,
                ),
                "same-pdf-hash",
                [
                    {
                        "page": page,
                        "text": f"Texto fuente página {page}",
                        "chunks": [f"Texto fuente página {page}"],
                    }
                    for page in range(7, 11)
                ],
                pdf_page_count=10,
            )
            with patch(
                "services.database.extract_regulatory_records",
                return_value=records,
            ):
                sync_regulatory_extractions(path, extractor_version="fixture")

        build_database(
            baseline_path,
            "https://www.invima.gov.co/biblioteca/download/old",
            baseline_records,
        )
        build_database(
            candidate_path,
            "https://www.invima.gov.co/biblioteca/download/new",
            candidate_records,
        )
        with connect(baseline_path) as connection:
            reviewed = dict(
                connection.execute(
                    """
                    SELECT r.decision_uid, r.record_key, r.page_number,
                           r.end_page_number, d.document_hash, d.pdf_page_count
                    FROM regulatory_records r
                    JOIN documents d ON d.id = r.document_id
                    WHERE r.normalized_expediente = 'exp100'
                    """
                ).fetchone()
            )
        review = new_review_event(
            reviewed,
            status="reviewed",
            reviewer="Prueba",
            notes="Corrección verificada",
            corrections={"active_ingredient": "Semaglutida"},
        )

        report = reconcile_database_decision_uids(
            baseline_path, candidate_path
        )

        with connect(candidate_path) as connection:
            candidate_uid = connection.execute(
                "SELECT decision_uid FROM regulatory_records "
                "WHERE normalized_expediente = 'exp100'"
            ).fetchone()[0]
        self.assertEqual(report["exact"], 1)
        self.assertEqual(report["reconciled"], 1)
        self.assertEqual(report["generated"], 1)
        self.assertEqual(report["orphaned"], 1)
        self.assertEqual(report["ambiguous"], 0)
        self.assertEqual(candidate_uid, review.decision_uid)
        self.assertEqual(report["uid_map"][review.decision_uid], review.decision_uid)
        self.assertTrue(report["record_key_map"][review.decision_uid]["changed"])

    def test_rebuilt_database_does_not_guess_ambiguous_record_uid(self) -> None:
        baseline_path = Path(self.temp_dir.name) / "ambiguous-baseline.db"
        candidate_path = Path(self.temp_dir.name) / "ambiguous-candidate.db"

        def prepare(path: Path, records: list[RegulatoryRecord]) -> None:
            initialize_database(path)
            insert_document(
                path,
                DocumentMetadata(
                    title="Acta No 02 de 2019 SEMPB",
                    url="https://www.invima.gov.co/biblioteca/download/ambiguous",
                    catalog_id="2019-sempb-02-completa",
                ),
                "hash",
                [
                    {
                        "page": 1,
                        "text": "Texto",
                        "chunks": ["Texto"],
                    }
                ],
            )
            with patch(
                "services.database.extract_regulatory_records",
                return_value=records,
            ):
                sync_regulatory_extractions(path, extractor_version="fixture")

        prepare(
            baseline_path,
            [
                RegulatoryRecord(
                    producto="A",
                    principio_activo=None,
                    interesado=None,
                    expediente="EXP-DUP",
                    radicado=None,
                    solicitud="Primera",
                    concepto="Concepto uno",
                    resultado_normalizado="sin_clasificar",
                    pagina=3,
                ),
                RegulatoryRecord(
                    producto="B",
                    principio_activo=None,
                    interesado=None,
                    expediente="EXP-DUP",
                    radicado=None,
                    solicitud="Segunda",
                    concepto="Concepto dos",
                    resultado_normalizado="sin_clasificar",
                    pagina=4,
                ),
            ],
        )
        prepare(
            candidate_path,
            [
                RegulatoryRecord(
                    producto="C",
                    principio_activo=None,
                    interesado=None,
                    expediente="EXP-DUP",
                    radicado=None,
                    solicitud="Distinta",
                    concepto="Sin correspondencia inequívoca",
                    resultado_normalizado="sin_clasificar",
                    pagina=9,
                )
            ],
        )

        report = reconcile_database_decision_uids(
            baseline_path, candidate_path
        )

        self.assertEqual(report["exact"], 0)
        self.assertEqual(report["reconciled"], 0)
        self.assertEqual(report["generated"], 0)
        self.assertEqual(report["ambiguous"], 1)
        self.assertEqual(report["orphaned"], 2)
        self.assertEqual(report["uid_map"], {})
        self.assertEqual(len(report["ambiguous_examples"]), 1)
        self.assertEqual(
            report["ambiguous_examples"][0]["expediente"], "expdup"
        )

    def test_reconciliation_uses_page_to_disambiguate_repeated_identifier(self) -> None:
        baseline_path = Path(self.temp_dir.name) / "page-baseline.db"
        candidate_path = Path(self.temp_dir.name) / "page-candidate.db"

        def records(prefix: str) -> list[RegulatoryRecord]:
            return [
                RegulatoryRecord(
                    producto=f"{prefix} A",
                    principio_activo=None,
                    interesado="Titular",
                    expediente="EXP-DUP",
                    radicado=None,
                    solicitud=f"{prefix} solicitud A",
                    concepto=f"{prefix} concepto A",
                    resultado_normalizado="sin_clasificar",
                    pagina=3,
                ),
                RegulatoryRecord(
                    producto=f"{prefix} B",
                    principio_activo=None,
                    interesado="Titular",
                    expediente="EXP-DUP",
                    radicado=None,
                    solicitud=f"{prefix} solicitud B",
                    concepto=f"{prefix} concepto B",
                    resultado_normalizado="sin_clasificar",
                    pagina=4,
                ),
            ]

        def prepare(path: Path, values: list[RegulatoryRecord]) -> None:
            initialize_database(path)
            insert_document(
                path,
                DocumentMetadata(
                    title="Acta No 02 de 2019 SEMPB",
                    url="https://www.invima.gov.co/biblioteca/download/pages",
                    catalog_id="2019-sempb-02-completa",
                ),
                "hash",
                [
                    {"page": page, "text": "Texto", "chunks": ["Texto"]}
                    for page in (3, 4)
                ],
            )
            with patch(
                "services.database.extract_regulatory_records",
                return_value=values,
            ):
                sync_regulatory_extractions(path, extractor_version="fixture")

        prepare(baseline_path, records("Anterior"))
        prepare(candidate_path, records("Nuevo"))
        with connect(baseline_path) as connection:
            baseline_uids = {
                int(row["page_number"]): str(row["decision_uid"])
                for row in connection.execute(
                    "SELECT page_number, decision_uid FROM regulatory_records"
                )
            }

        report = reconcile_database_decision_uids(
            baseline_path, candidate_path
        )

        with connect(candidate_path) as connection:
            candidate_uids = {
                int(row["page_number"]): str(row["decision_uid"])
                for row in connection.execute(
                    "SELECT page_number, decision_uid FROM regulatory_records"
                )
            }
        self.assertEqual(candidate_uids, baseline_uids)
        self.assertEqual(report["ambiguous"], 0)
        self.assertEqual(report["reconciliation_strategies"]["composite"], 2)

    def test_new_record_reusing_consumed_identifier_is_not_ambiguous(self) -> None:
        baseline_path = Path(self.temp_dir.name) / "consumed-baseline.db"
        candidate_path = Path(self.temp_dir.name) / "consumed-candidate.db"
        original = RegulatoryRecord(
            producto="ALFA",
            principio_activo=None,
            interesado=None,
            expediente="EXP-SHARED",
            radicado=None,
            solicitud="Solicitud original",
            concepto="Concepto original",
            resultado_normalizado="aprobado",
            pagina=2,
        )
        new_record = RegulatoryRecord(
            producto="BETA",
            principio_activo=None,
            interesado=None,
            expediente="EXP-SHARED",
            radicado=None,
            solicitud="Solicitud posterior distinta",
            concepto="Concepto posterior distinto",
            resultado_normalizado="sin_clasificar",
            pagina=3,
        )

        def prepare(path: Path, values: list[RegulatoryRecord]) -> None:
            initialize_database(path)
            insert_document(
                path,
                DocumentMetadata(
                    title="Acta No 03 de 2020 SEMPB",
                    url="https://www.invima.gov.co/biblioteca/download/consumed",
                    catalog_id="2020-sempb-03-completa",
                ),
                "hash",
                [
                    {"page": page, "text": "Texto", "chunks": ["Texto"]}
                    for page in (2, 3)
                ],
            )
            with patch(
                "services.database.extract_regulatory_records",
                return_value=values,
            ):
                sync_regulatory_extractions(path, extractor_version="fixture")

        prepare(baseline_path, [original])
        prepare(candidate_path, [original, new_record])

        report = reconcile_database_decision_uids(
            baseline_path, candidate_path
        )

        self.assertEqual(report["exact"], 1)
        self.assertEqual(report["generated"], 1)
        self.assertEqual(report["ambiguous"], 0)

    def test_radicado_prefix_alone_never_inherits_an_identity(self) -> None:
        baseline_path = Path(self.temp_dir.name) / "prefix-baseline.db"
        candidate_path = Path(self.temp_dir.name) / "prefix-candidate.db"

        def prepare(path: Path, record: RegulatoryRecord) -> str:
            initialize_database(path)
            insert_document(
                path,
                DocumentMetadata(
                    title="Acta No 03 de 2021 SEMPB",
                    url="https://www.invima.gov.co/biblioteca/download/prefix",
                    catalog_id="2021-sempb-03-completa",
                ),
                "hash-prefix",
                [
                    {
                        "page": record.pagina,
                        "text": "Texto",
                        "chunks": ["Texto"],
                    }
                ],
            )
            with patch(
                "services.database.extract_regulatory_records",
                return_value=[record],
            ):
                sync_regulatory_extractions(path, extractor_version="fixture")
            with connect(path) as connection:
                return str(
                    connection.execute(
                        "SELECT decision_uid FROM regulatory_records"
                    ).fetchone()[0]
                )

        baseline_uid = prepare(
            baseline_path,
            RegulatoryRecord(
                producto="ALFA",
                principio_activo=None,
                interesado=None,
                expediente=None,
                radicado="20130000001",
                solicitud="Solicitud original",
                concepto="Concepto original",
                resultado_normalizado="sin_clasificar",
                pagina=3,
            ),
        )
        prepare(
            candidate_path,
            RegulatoryRecord(
                producto="BETA",
                principio_activo=None,
                interesado=None,
                expediente=None,
                radicado="20130000002",
                solicitud="Solicitud distinta",
                concepto="Concepto distinto",
                resultado_normalizado="sin_clasificar",
                pagina=30,
            ),
        )

        report = reconcile_database_decision_uids(
            baseline_path,
            candidate_path,
            protected_decision_uids=set(),
        )

        with connect(candidate_path) as connection:
            candidate_uid = str(
                connection.execute(
                    "SELECT decision_uid FROM regulatory_records"
                ).fetchone()[0]
            )
        self.assertNotEqual(candidate_uid, baseline_uid)
        self.assertEqual(report["reconciled"], 0)
        self.assertEqual(report["not_inherited"], 1)

    def test_unreviewed_ambiguity_gets_explicit_new_identity(self) -> None:
        baseline_path = Path(self.temp_dir.name) / "unprotected-baseline.db"
        candidate_path = Path(self.temp_dir.name) / "unprotected-candidate.db"

        def prepare(path: Path, values: list[RegulatoryRecord]) -> None:
            initialize_database(path)
            insert_document(
                path,
                DocumentMetadata(
                    title="Acta No 04 de 2020 SEMPB",
                    url="https://www.invima.gov.co/biblioteca/download/unprotected",
                    catalog_id="2020-sempb-04-completa",
                ),
                "hash",
                [{"page": 1, "text": "Texto", "chunks": ["Texto"]}],
            )
            with patch(
                "services.database.extract_regulatory_records",
                return_value=values,
            ):
                sync_regulatory_extractions(path, extractor_version="fixture")

        prepare(
            baseline_path,
            [
                RegulatoryRecord(
                    producto="A",
                    principio_activo=None,
                    interesado=None,
                    expediente="EXP-DUP",
                    radicado=None,
                    solicitud="Primera",
                    concepto="Concepto uno",
                    resultado_normalizado="sin_clasificar",
                    pagina=3,
                ),
                RegulatoryRecord(
                    producto="B",
                    principio_activo=None,
                    interesado=None,
                    expediente="EXP-DUP",
                    radicado=None,
                    solicitud="Segunda",
                    concepto="Concepto dos",
                    resultado_normalizado="sin_clasificar",
                    pagina=4,
                ),
            ],
        )
        prepare(
            candidate_path,
            [
                RegulatoryRecord(
                    producto="C",
                    principio_activo=None,
                    interesado=None,
                    expediente="EXP-DUP",
                    radicado=None,
                    solicitud="Distinta",
                    concepto="Sin correspondencia inequívoca",
                    resultado_normalizado="sin_clasificar",
                    pagina=9,
                )
            ],
        )

        report = reconcile_database_decision_uids(
            baseline_path,
            candidate_path,
            protected_decision_uids=set(),
        )

        with connect(candidate_path) as connection:
            strategy = str(
                connection.execute(
                    "SELECT identity_strategy FROM regulatory_records"
                ).fetchone()[0]
            )
        self.assertEqual(report["ambiguous"], 0)
        self.assertEqual(report["not_inherited"], 1)
        self.assertEqual(report["protected_uids"], 0)
        self.assertEqual(strategy, "candidate_not_inherited_v6")

    def test_reviewed_uid_keeps_unresolved_identity_blocking(self) -> None:
        baseline_path = Path(self.temp_dir.name) / "protected-baseline.db"
        candidate_path = Path(self.temp_dir.name) / "protected-candidate.db"

        def prepare(path: Path, values: list[RegulatoryRecord]) -> None:
            initialize_database(path)
            insert_document(
                path,
                DocumentMetadata(
                    title="Acta No 05 de 2020 SEMPB",
                    url="https://www.invima.gov.co/biblioteca/download/protected",
                    catalog_id="2020-sempb-05-completa",
                ),
                "hash",
                [{"page": 1, "text": "Texto", "chunks": ["Texto"]}],
            )
            with patch(
                "services.database.extract_regulatory_records",
                return_value=values,
            ):
                sync_regulatory_extractions(path, extractor_version="fixture")

        baseline_records = [
            RegulatoryRecord(
                producto=product,
                principio_activo=None,
                interesado=None,
                expediente="EXP-DUP",
                radicado=None,
                solicitud=f"Solicitud {product}",
                concepto=f"Concepto {product}",
                resultado_normalizado="sin_clasificar",
                pagina=page,
            )
            for product, page in (("A", 3), ("B", 4))
        ]
        prepare(baseline_path, baseline_records)
        prepare(
            candidate_path,
            [
                RegulatoryRecord(
                    producto="C",
                    principio_activo=None,
                    interesado=None,
                    expediente="EXP-DUP",
                    radicado=None,
                    solicitud="Distinta",
                    concepto="Sin correspondencia inequívoca",
                    resultado_normalizado="sin_clasificar",
                    pagina=9,
                )
            ],
        )
        with connect(baseline_path) as connection:
            protected_uid = str(
                connection.execute(
                    "SELECT decision_uid FROM regulatory_records "
                    "ORDER BY page_number LIMIT 1"
                ).fetchone()[0]
            )

        report = reconcile_database_decision_uids(
            baseline_path,
            candidate_path,
            protected_decision_uids={protected_uid},
        )

        self.assertEqual(report["ambiguous"], 1)
        self.assertEqual(report["not_inherited"], 0)
        self.assertEqual(report["protected_uids"], 1)

    def test_reconciliation_matches_document_by_unique_pdf_hash(self) -> None:
        baseline_path = Path(self.temp_dir.name) / "hash-baseline.db"
        candidate_path = Path(self.temp_dir.name) / "hash-candidate.db"
        record = RegulatoryRecord(
            producto="OZEMPIC",
            principio_activo="Semaglutida",
            interesado="Novo Nordisk",
            expediente="EXP-HASH",
            radicado="RAD-HASH",
            solicitud="Evaluación farmacológica",
            concepto="La Sala aprueba.",
            resultado_normalizado="aprobado",
            pagina=5,
        )

        def prepare(
            path: Path,
            *,
            title: str,
            url: str,
            catalog_id: str,
            year: int,
            acta_number: str,
        ) -> str:
            initialize_database(path)
            insert_document(
                path,
                DocumentMetadata(
                    title=title,
                    url=url,
                    year=year,
                    acta_number=acta_number,
                    section="SEMPB",
                    catalog_id=catalog_id,
                ),
                "same-unique-pdf-hash",
                [{"page": 5, "text": "Texto", "chunks": ["Texto"]}],
            )
            with patch(
                "services.database.extract_regulatory_records",
                return_value=[record],
            ):
                sync_regulatory_extractions(path, extractor_version="fixture")
            with connect(path) as connection:
                return str(
                    connection.execute(
                        "SELECT decision_uid FROM regulatory_records"
                    ).fetchone()[0]
                )

        baseline_uid = prepare(
            baseline_path,
            title="Acta antigua",
            url="https://www.invima.gov.co/biblioteca/download/old-hash",
            catalog_id="catalogo-antiguo",
            year=2018,
            acta_number="01",
        )
        candidate_uid_before = prepare(
            candidate_path,
            title="Acta corregida",
            url="https://www.invima.gov.co/biblioteca/download/new-hash",
            catalog_id="catalogo-nuevo",
            year=2019,
            acta_number="99",
        )
        self.assertNotEqual(candidate_uid_before, baseline_uid)

        report = reconcile_database_decision_uids(
            baseline_path, candidate_path
        )

        with connect(candidate_path) as connection:
            candidate_uid_after = str(
                connection.execute(
                    "SELECT decision_uid FROM regulatory_records"
                ).fetchone()[0]
            )
        self.assertEqual(report["documents_matched"], 1)
        self.assertEqual(report["exact"], 1)
        self.assertEqual(candidate_uid_after, baseline_uid)

    def test_reconciliation_rotates_a_conflicting_candidate_uid(self) -> None:
        baseline_path = Path(self.temp_dir.name) / "collision-baseline.db"
        candidate_path = Path(self.temp_dir.name) / "collision-candidate.db"
        shared = RegulatoryRecord(
            producto="ALFA",
            principio_activo=None,
            interesado=None,
            expediente="EXP-SHARED",
            radicado=None,
            solicitud="Solicitud original",
            concepto="Concepto original",
            resultado_normalizado="aprobado",
            pagina=2,
        )
        changed = RegulatoryRecord(
            producto="ALFA MEJORADO",
            principio_activo="Activo A",
            interesado=None,
            expediente="EXP-SHARED",
            radicado=None,
            solicitud="Solicitud modificada",
            concepto="Concepto modificado",
            resultado_normalizado="aprobado",
            pagina=2,
        )
        unrelated = RegulatoryRecord(
            producto="NUEVO",
            principio_activo=None,
            interesado=None,
            expediente="EXP-NEW",
            radicado=None,
            solicitud="Otra solicitud",
            concepto="Otro concepto",
            resultado_normalizado="sin_clasificar",
            pagina=3,
        )

        def prepare(path: Path, records: list[RegulatoryRecord]) -> None:
            initialize_database(path)
            insert_document(
                path,
                DocumentMetadata(
                    title="Acta No 03 de 2020 SEMPB",
                    url="https://www.invima.gov.co/biblioteca/download/collision",
                    catalog_id="2020-sempb-03-completa",
                ),
                "hash",
                [
                    {"page": 2, "text": "Texto 2", "chunks": ["Texto 2"]},
                    {"page": 3, "text": "Texto 3", "chunks": ["Texto 3"]},
                ],
            )
            with patch(
                "services.database.extract_regulatory_records",
                return_value=records,
            ):
                sync_regulatory_extractions(path, extractor_version="fixture")

        prepare(baseline_path, [shared])
        prepare(candidate_path, [changed, unrelated])
        with connect(baseline_path) as connection:
            protected_uid = connection.execute(
                "SELECT decision_uid FROM regulatory_records"
            ).fetchone()[0]
        with connect(candidate_path) as connection:
            matching_id = connection.execute(
                "SELECT id FROM regulatory_records "
                "WHERE normalized_expediente = 'expshared'"
            ).fetchone()[0]
            unrelated_id = connection.execute(
                "SELECT id FROM regulatory_records "
                "WHERE normalized_expediente = 'expnew'"
            ).fetchone()[0]
            connection.execute(
                "UPDATE regulatory_records SET decision_uid = ? WHERE id = ?",
                ("temporary-matching-uid", matching_id),
            )
            connection.execute(
                "UPDATE regulatory_records SET decision_uid = ? WHERE id = ?",
                (protected_uid, unrelated_id),
            )

        report = reconcile_database_decision_uids(
            baseline_path, candidate_path
        )

        with connect(candidate_path) as connection:
            rows = {
                row["normalized_expediente"]: row["decision_uid"]
                for row in connection.execute(
                    "SELECT normalized_expediente, decision_uid "
                    "FROM regulatory_records"
                )
            }
            duplicate_count = connection.execute(
                "SELECT COUNT(*) - COUNT(DISTINCT decision_uid) "
                "FROM regulatory_records WHERE decision_uid != ''"
            ).fetchone()[0]
        self.assertEqual(rows["expshared"], protected_uid)
        self.assertNotEqual(rows["expnew"], protected_uid)
        self.assertEqual(duplicate_count, 0)
        self.assertIn(protected_uid, report["candidate_uid_replacements"])

    def test_scored_reconciliation_reuses_pair_scores_between_rounds(self) -> None:
        candidates = [{"id": value} for value in (1, 2, 3)]
        baselines = [{"id": value} for value in (101, 102, 103)]
        score_matrix = {
            (1, 101): 150.0,
            (2, 101): 100.0,
            (2, 102): 90.0,
            (3, 102): 50.0,
            (3, 103): 40.0,
        }

        def reconcile(*, cached: bool) -> tuple[list[tuple[int, int]], int]:
            candidate_unused = {1, 2, 3}
            baseline_unused = {101, 102, 103}
            accepted: list[tuple[int, int]] = []
            calls = 0

            def score(candidate: dict, baseline: dict, *_args) -> float | None:
                nonlocal calls
                calls += 1
                return score_matrix.get((candidate["id"], baseline["id"]))

            with patch("services.database._record_link_score", side_effect=score):
                precomputed_scores = None
                if cached:
                    precomputed_scores = _precompute_record_link_scores(
                        candidates,
                        baselines,
                        {},
                    )
                while True:
                    matches = _mutual_scored_matches(
                        candidates,
                        baselines,
                        candidate_unused,
                        baseline_unused,
                        precomputed_scores=precomputed_scores,
                    )
                    if not matches:
                        break
                    for candidate_id, baseline_id in matches:
                        accepted.append((candidate_id, baseline_id))
                        candidate_unused.remove(candidate_id)
                        baseline_unused.remove(baseline_id)
            return accepted, calls

        uncached_matches, uncached_calls = reconcile(cached=False)
        cached_matches, cached_calls = reconcile(cached=True)

        self.assertEqual(cached_matches, uncached_matches)
        self.assertEqual(cached_matches, [(1, 101), (2, 102), (3, 103)])
        self.assertEqual(cached_calls, 9)
        self.assertGreater(uncached_calls, cached_calls)

    def test_text_similarity_is_shared_by_scoring_and_ambiguity_check(self) -> None:
        common = {
            "page_number": 7,
            "end_page_number": 7,
            "page_span": (7, 7),
            "radicado": "",
            "radicado_prefix": "",
            "expediente": "",
            "stable_numeral": "",
            "product": "producto",
            "active_ingredient": "activo",
            "interested_party": "titular",
            "ordinal": 1,
        }
        candidate = {
            **common,
            "id": 1,
            "text_signature": (
                "inicio candidato "
                + ("texto regulatorio " * 20)
                + "cierre candidato"
            ),
        }
        baseline = {
            **common,
            "id": 101,
            "text_signature": (
                "inicio referencia "
                + ("texto regulatorio " * 19)
                + "cierre referencia"
            ),
        }
        cache: dict[tuple[str, str], tuple[float, bool]] = {}

        with patch(
            "services.database.SequenceMatcher",
            wraps=StandardSequenceMatcher,
        ) as matcher:
            cached_score = _record_link_score(candidate, baseline, cache)
            cached_evidence = _record_match_has_evidence(
                candidate,
                baseline,
                cache,
            )
            # Otro par con el mismo contenido debe compartir la comparación,
            # aunque sus identificadores internos sean distintos.
            duplicate_candidate = {**candidate, "id": 2}
            duplicate_baseline = {**baseline, "id": 102}
            self.assertEqual(
                _record_link_score(duplicate_candidate, duplicate_baseline, cache),
                cached_score,
            )

        self.assertEqual(matcher.call_count, 1)
        self.assertEqual(len(cache), 1)
        self.assertEqual(cached_score, _record_link_score(candidate, baseline))
        self.assertEqual(
            cached_evidence,
            _record_match_has_evidence(candidate, baseline),
        )

    def test_identical_long_text_skips_sequence_matcher_without_changing_score(
        self,
    ) -> None:
        text = "concepto regulatorio repetido " * 200
        common = {
            "page_number": 9,
            "end_page_number": 9,
            "page_span": (9, 9),
            "radicado": "",
            "radicado_prefix": "",
            "expediente": "",
            "stable_numeral": "",
            "product": "producto",
            "active_ingredient": "activo",
            "interested_party": "titular",
            "ordinal": 1,
            "text_signature": text,
        }
        candidate = {**common, "id": 1}
        baseline = {**common, "id": 101}

        expected = _record_link_score(candidate, baseline)
        with patch("services.database.SequenceMatcher") as matcher:
            actual = _record_link_score(candidate, baseline, {})

        self.assertEqual(actual, expected)
        matcher.assert_not_called()

    def test_contained_long_text_uses_exact_ratio_without_sequence_matcher(
        self,
    ) -> None:
        shorter = "fundamento regulatorio " * 100
        longer = "introduccion " + shorter + " conclusion"
        common = {
            "page_number": 9,
            "end_page_number": 9,
            "page_span": (9, 9),
            "radicado": "",
            "radicado_prefix": "",
            "expediente": "",
            "stable_numeral": "",
            "product": "producto",
            "active_ingredient": "activo",
            "interested_party": "titular",
            "ordinal": 1,
        }
        candidate = {**common, "id": 1, "text_signature": shorter}
        baseline = {**common, "id": 101, "text_signature": longer}

        expected = _record_link_score(candidate, baseline)
        expected_ratio = StandardSequenceMatcher(
            None,
            shorter,
            longer,
            autojunk=False,
        ).ratio()
        self.assertEqual(
            expected_ratio,
            (2.0 * len(shorter)) / (len(shorter) + len(longer)),
        )
        with patch("services.database.SequenceMatcher") as matcher:
            actual = _record_link_score(candidate, baseline, {})

        self.assertEqual(actual, expected)
        matcher.assert_not_called()

    def test_filters_are_applied(self) -> None:
        self.assertEqual(
            search_chunks(
                self.database_path,
                "semaglutida",
                filters={"years": [2025]},
            ),
            [],
        )

    def test_stats_and_filter_options(self) -> None:
        stats = database_stats(self.database_path)
        self.assertEqual(stats, {"documents": 1, "pages": 1, "chunks": 1})
        options = get_filter_options(self.database_path)
        self.assertEqual(options["years"], [2026])
        self.assertEqual(options["sections"], ["SEMPB"])

    def test_filter_options_include_current_human_only_categories(self) -> None:
        extracted = RegulatoryRecord(
            producto="Producto de prueba",
            principio_activo="Semaglutida",
            interesado="Titular",
            expediente="EXP-FILTRO",
            radicado="RAD-FILTRO",
            solicitud="Solicitud",
            concepto="La Sala requiere información.",
            resultado_normalizado="requerido",
            pagina=7,
            tipo_solicitud="indicaciones",
        )
        with patch(
            "services.database.extract_regulatory_records",
            return_value=[extracted],
        ):
            sync_regulatory_extractions(self.database_path)
        with connect(self.database_path) as connection:
            connection.execute("UPDATE documents SET pdf_page_count = 7")
        record = list_regulatory_records(self.database_path, limit=1)[0]
        review = new_review_event(
            record,
            status="approved",
            reviewer="Prueba",
            notes="Categorías corregidas contra la fuente",
            corrections={
                "outcome_code": "archivado",
                "request_type_code": "cancelacion",
            },
        )

        automatic = get_filter_options(self.database_path)
        effective = get_filter_options(self.database_path, review_events=[review])

        self.assertNotIn("archivado", automatic["outcomes"])
        self.assertNotIn("cancelacion", automatic["request_types"])
        self.assertIn("archivado", effective["outcomes"])
        self.assertIn("cancelacion", effective["request_types"])

    def test_adds_source_type_to_legacy_database(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy.db"
        with sqlite3.connect(legacy_path) as legacy_connection:
            legacy_connection.execute(
                "CREATE TABLE documents (id INTEGER PRIMARY KEY, title TEXT NOT NULL)"
            )
        with connect(legacy_path) as migrated_connection:
            columns = {
                row[1]
                for row in migrated_connection.execute(
                    "PRAGMA table_info(documents)"
                ).fetchall()
            }
        self.assertIn("source_type", columns)

    def test_migrates_v2_additively_without_changing_text_index(self) -> None:
        with connect(self.database_path) as connection:
            connection.execute("DROP TABLE regulatory_records")
            connection.execute("DROP TABLE document_extractions")
            connection.execute(
                "UPDATE app_metadata SET value = '2' WHERE key = 'schema_version'"
            )

        migrated = migrate_database_schema(self.database_path)

        self.assertTrue(migrated)
        self.assertEqual(
            database_schema_version(self.database_path), DATABASE_SCHEMA_VERSION
        )
        self.assertTrue(table_exists(self.database_path, "regulatory_records"))
        with connect(self.database_path) as connection:
            record_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(regulatory_records)"
                )
            }
        self.assertIn("end_page_number", record_columns)
        self.assertEqual(len(search_chunks(self.database_path, "semaglutida")), 1)

    def test_accepts_v5_as_an_additive_migration_source(self) -> None:
        with connect(self.database_path) as connection:
            connection.execute("DROP TABLE regulatory_field_evidence")
            connection.execute(
                "UPDATE app_metadata SET value = '5' WHERE key = 'schema_version'"
            )
            before = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("documents", "pages", "chunks", "chunks_fts")
            }

        self.assertTrue(migrate_database_schema(self.database_path))

        with connect(self.database_path) as connection:
            after = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in before
            }
        self.assertEqual(before, after)
        self.assertEqual(database_schema_version(self.database_path), 6)
        self.assertTrue(table_exists(self.database_path, "regulatory_field_evidence"))

    def test_migrates_v3_records_to_page_ranges(self) -> None:
        with connect(self.database_path) as connection:
            columns = [
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(regulatory_records)"
                )
                if row[1] != "end_page_number"
            ]
            selected = ", ".join(columns)
            connection.execute(
                f"CREATE TABLE regulatory_records_v3 AS "
                f"SELECT {selected} FROM regulatory_records"
            )
            connection.execute("DROP TABLE regulatory_records")
            connection.execute(
                "ALTER TABLE regulatory_records_v3 RENAME TO regulatory_records"
            )
            connection.execute(
                "UPDATE app_metadata SET value = '3' WHERE key = 'schema_version'"
            )

        self.assertTrue(migrate_database_schema(self.database_path))
        self.assertEqual(
            database_schema_version(self.database_path), DATABASE_SCHEMA_VERSION
        )
        with connect(self.database_path) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(regulatory_records)"
                )
            }
        self.assertIn("end_page_number", columns)

    def test_migrates_v4_to_v6_without_changing_index_counts(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "schema-v4.db"
        with sqlite3.connect(legacy_path) as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;

                CREATE TABLE app_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO app_metadata (key, value)
                VALUES ('schema_version', '4');

                CREATE TABLE documents (
                    id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    normalized_title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    manifest_url TEXT NOT NULL UNIQUE,
                    year INTEGER,
                    acta_number TEXT,
                    section TEXT,
                    part TEXT,
                    source_type TEXT NOT NULL DEFAULT 'official',
                    document_hash TEXT NOT NULL,
                    pdf_page_count INTEGER NOT NULL,
                    indexed_page_count INTEGER NOT NULL,
                    ocr_candidate_pages TEXT NOT NULL DEFAULT '',
                    page_inventory_complete INTEGER NOT NULL DEFAULT 1,
                    indexed_at TEXT NOT NULL
                );
                CREATE TABLE pages (
                    id INTEGER PRIMARY KEY,
                    document_id INTEGER NOT NULL
                        REFERENCES documents(id) ON DELETE CASCADE,
                    page_number INTEGER NOT NULL,
                    UNIQUE(document_id, page_number)
                );
                CREATE TABLE chunks (
                    id INTEGER PRIMARY KEY,
                    page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    UNIQUE(page_id, chunk_index)
                );
                CREATE VIRTUAL TABLE chunks_fts USING fts5(
                    title,
                    text,
                    content='',
                    detail=column,
                    tokenize = 'unicode61 remove_diacritics 2'
                );
                CREATE TABLE document_extractions (
                    document_id INTEGER PRIMARY KEY
                        REFERENCES documents(id) ON DELETE CASCADE,
                    document_hash TEXT NOT NULL,
                    extractor_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_count INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    extracted_at TEXT NOT NULL
                );
                CREATE TABLE regulatory_records (
                    id INTEGER PRIMARY KEY,
                    document_id INTEGER NOT NULL
                        REFERENCES documents(id) ON DELETE CASCADE,
                    record_key TEXT NOT NULL,
                    page_number INTEGER NOT NULL,
                    end_page_number INTEGER NOT NULL,
                    product_name TEXT,
                    normalized_product_name TEXT,
                    active_ingredient TEXT,
                    normalized_active_ingredient TEXT,
                    interested_party TEXT,
                    normalized_interested_party TEXT,
                    expediente TEXT,
                    normalized_expediente TEXT,
                    radicado TEXT,
                    normalized_radicado TEXT,
                    request_text TEXT,
                    concept_text TEXT,
                    outcome_code TEXT NOT NULL,
                    extraction_method TEXT NOT NULL,
                    confidence REAL,
                    needs_review INTEGER NOT NULL DEFAULT 1,
                    extractor_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(document_id, record_key),
                    CHECK(page_number >= 1),
                    CHECK(end_page_number >= page_number),
                    CHECK(confidence IS NULL OR confidence BETWEEN 0 AND 1)
                );
                """
            )
            connection.execute(
                """
                INSERT INTO documents (
                    id, title, normalized_title, url, manifest_url, year,
                    acta_number, section, part, source_type, document_hash,
                    pdf_page_count, indexed_page_count, ocr_candidate_pages,
                    page_inventory_complete, indexed_at
                ) VALUES (
                    1, 'Acta No 01 de 2024 SEMPB',
                    'acta no 01 de 2024 sempb',
                    'https://www.invima.gov.co/biblioteca/download/v4',
                    'https://www.invima.gov.co/biblioteca/download/v4',
                    2024, '01', 'SEMPB', NULL, 'official', 'pdf-hash-v4',
                    1, 1, '', 1, '2024-01-02T00:00:00+00:00'
                )
                """
            )
            connection.execute(
                "INSERT INTO pages (id, document_id, page_number) VALUES (1, 1, 3)"
            )
            chunk_text = "Concepto de la Sala sobre semaglutida."
            connection.execute(
                "INSERT INTO chunks (id, page_id, chunk_index, text) "
                "VALUES (1, 1, 0, ?)",
                (chunk_text,),
            )
            connection.execute(
                "INSERT INTO chunks_fts (rowid, title, text) VALUES (1, ?, ?)",
                ("Acta No 01 de 2024 SEMPB", chunk_text),
            )
            connection.execute(
                """
                INSERT INTO regulatory_records (
                    id, document_id, record_key, page_number, end_page_number,
                    product_name, normalized_product_name, active_ingredient,
                    normalized_active_ingredient, expediente,
                    normalized_expediente, request_text, concept_text,
                    outcome_code, extraction_method, confidence, needs_review,
                    extractor_version, created_at
                ) VALUES (
                    1, 1, 'record-key-v4', 3, 3, 'OZEMPIC', 'ozempic',
                    'Semaglutida', 'semaglutida', 'EXP-4', 'exp4',
                    'Evaluación farmacológica', 'La Sala aprueba.',
                    'aprobado', 'deterministic', 0.9, 1, '2',
                    '2024-01-02T00:00:00+00:00'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO document_extractions (
                    document_id, document_hash, extractor_version, status,
                    record_count, error_message, extracted_at
                ) VALUES (
                    1, 'pdf-hash-v4', '2', 'complete', 1, NULL,
                    '2024-01-02T00:00:00+00:00'
                )
                """
            )
            before = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "documents",
                    "pages",
                    "chunks",
                    "chunks_fts",
                    "regulatory_records",
                )
            }

        self.assertTrue(migrate_database_schema(legacy_path))
        self.assertFalse(migrate_database_schema(legacy_path))

        with connect(legacy_path) as connection:
            after = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in before
            }
            record_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(regulatory_records)")
            }
            document_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(documents)")
            }
            page_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(pages)")
            }
            indexes = {
                row[1]
                for row in connection.execute("PRAGMA index_list(regulatory_records)")
            }
            record = connection.execute(
                "SELECT record_key, product_name, active_ingredient, outcome_code, "
                "decision_uid, identity_strategy "
                "FROM regulatory_records WHERE id = 1"
            ).fetchone()
        self.assertEqual(before, after)
        self.assertEqual(database_schema_version(legacy_path), DATABASE_SCHEMA_VERSION)
        self.assertIn("decision_uid", record_columns)
        self.assertIn("request_type_code", record_columns)
        self.assertIn("catalog_id", document_columns)
        self.assertIn("publication_date", document_columns)
        self.assertIn("raw_text_compressed", page_columns)
        self.assertIn("text_source", page_columns)
        self.assertTrue(table_exists(legacy_path, "regulatory_field_evidence"))
        self.assertIn("idx_records_decision_uid", indexes)
        self.assertEqual(tuple(record)[:4], ("record-key-v4", "OZEMPIC", "Semaglutida", "aprobado"))
        self.assertTrue(str(record[4]).startswith("dec_"))
        self.assertEqual(record[5], "migration_generated_v6")
        self.assertEqual(len(search_chunks(legacy_path, "semaglutida")), 1)

    def test_sync_repairs_blank_uid_even_when_extraction_is_current(self) -> None:
        record = RegulatoryRecord(
            producto="ALFA",
            principio_activo="Semaglutida",
            interesado=None,
            expediente="EXP-UID",
            radicado=None,
            solicitud="Evaluación",
            concepto="La Sala aprueba.",
            resultado_normalizado="aprobado",
            pagina=7,
        )
        with patch(
            "services.database.extract_regulatory_records", return_value=[record]
        ):
            sync_regulatory_extractions(self.database_path)
        with connect(self.database_path) as connection:
            connection.execute("UPDATE regulatory_records SET decision_uid = ''")

        with patch(
            "services.database.extract_regulatory_records", return_value=[record]
        ) as extractor:
            summary = sync_regulatory_extractions(self.database_path)

        with connect(self.database_path) as connection:
            uid = connection.execute(
                "SELECT decision_uid FROM regulatory_records"
            ).fetchone()[0]
        self.assertEqual(summary["documents_processed"], 1)
        self.assertEqual(extractor.call_count, 1)
        self.assertTrue(str(uid).startswith("dec_"))

    def test_regulatory_uid_is_stable_when_reextracted(self) -> None:
        insert_document(
            self.database_path,
            DocumentMetadata(
                title="Acta No 04 de 2026 SEMPB",
                url="https://www.invima.gov.co/biblioteca/download/4",
                year=2026,
                acta_number="04",
                section="SEMPB",
            ),
            "hash-4",
            [
                {
                    "page": 12,
                    "chunks": [
                        "3.1.1 EVALUACIONES FARMACOLÓGICAS\n"
                        "Producto: ALFA\nExpediente: 12345\n"
                        "Solicitud: Evaluación farmacológica.\n"
                        "Concepto: La Sala aprueba lo solicitado."
                    ],
                }
            ],
        )
        sync_regulatory_extractions(self.database_path)
        with connect(self.database_path) as connection:
            first = connection.execute(
                "SELECT decision_uid FROM regulatory_records "
                "WHERE document_id = (SELECT id FROM documents WHERE title LIKE 'Acta No 04%')"
            ).fetchone()[0]
            connection.execute(
                "UPDATE document_extractions SET extractor_version = 'anterior' "
                "WHERE document_id = (SELECT id FROM documents WHERE title LIKE 'Acta No 04%')"
            )
        sync_regulatory_extractions(self.database_path)
        with connect(self.database_path) as connection:
            second = connection.execute(
                "SELECT decision_uid FROM regulatory_records "
                "WHERE document_id = (SELECT id FROM documents WHERE title LIKE 'Acta No 04%')"
            ).fetchone()[0]

        self.assertEqual(first, second)

    def test_extracts_and_filters_structured_regulatory_fields(self) -> None:
        insert_document(
            self.database_path,
            DocumentMetadata(
                title="Acta No 02 de 2026 SEMPB",
                url="https://www.invima.gov.co/biblioteca/download/2",
                year=2026,
                acta_number="02",
                section="SEMPB",
            ),
            "hash-2",
            [
                {
                    "page": 10,
                    "chunks": [
                        "Producto: Ozempic\nPrincipio activo: Semaglutida\n"
                        "Interesado: Compañía Ejemplo\nExpediente: 123456\n"
                        "Radicado: 2026123456\nSolicitud: Evaluación farmacológica\n"
                        "Concepto: La Sala requiere aportar información adicional."
                    ],
                }
            ],
        )

        summary = sync_regulatory_extractions(self.database_path)
        results = search_chunks(
            self.database_path,
            "información adicional",
            filters={"products": ["Ozempic"], "outcomes": ["requerido"]},
        )
        mapped = regulatory_records_for_chunks(
            self.database_path,
            [results[0].chunk_id],
        )

        self.assertGreaterEqual(summary["records_extracted"], 1)
        self.assertEqual(results[0].acta_number, "02")
        record = mapped[results[0].chunk_id][0]
        self.assertEqual(record["product_name"], "Ozempic")
        self.assertEqual(record["radicado"], "2026123456")
        self.assertEqual(record["outcome_code"], "requerido")
        self.assertEqual(
            count_regulatory_records(
                self.database_path,
                years=[2026],
                outcomes=["requerido"],
            ),
            1,
        )
        self.assertEqual(
            count_regulatory_records(
                self.database_path,
                missing_field="request_type",
            ),
            0,
        )

    def test_structured_filter_does_not_leak_to_another_decision(self) -> None:
        insert_document(
            self.database_path,
            DocumentMetadata(
                title="Acta No 03 de 2026 SEMPB",
                url="https://www.invima.gov.co/biblioteca/download/3",
                year=2026,
                acta_number="03",
                section="SEMPB",
            ),
            "hash-3",
            [
                {
                    "page": 20,
                    "chunks": [
                        "Producto: ALFA\nExpediente: 11111\n"
                        "Solicitud: Renovación.\nConcepto: La Sala aprueba."
                    ],
                },
                {
                    "page": 21,
                    "chunks": [
                        "Producto: BETA\nExpediente: 22222\n"
                        "Solicitud: Evaluación de estabilidad.\n"
                        "Concepto: La Sala niega lo solicitado."
                    ],
                },
            ],
        )
        sync_regulatory_extractions(self.database_path)

        self.assertEqual(
            search_chunks(
                self.database_path,
                "estabilidad",
                filters={"products": ["ALFA"]},
            ),
            [],
        )
        beta = search_chunks(
            self.database_path,
            "estabilidad",
            filters={"products": ["BETA"], "outcomes": ["negado"]},
        )
        self.assertEqual(len(beta), 1)

    def test_missing_active_ingredient_filters_and_counts_the_complete_queue(self) -> None:
        insert_document(
            self.database_path,
            DocumentMetadata(
                title="Acta No 05 de 2026 SEMPB",
                url="https://www.invima.gov.co/biblioteca/download/5",
                year=2026,
                acta_number="05",
                section="SEMPB",
            ),
            "hash-5",
            [
                {
                    "page": 30,
                    "text": (
                        "Producto: ALFA\nExpediente: 5001\n"
                        "Solicitud: Renovación.\nConcepto: La Sala aprueba."
                    ),
                    "chunks": [
                        "Producto: ALFA\nExpediente: 5001\n"
                        "Solicitud: Renovación.\nConcepto: La Sala aprueba."
                    ],
                },
                {
                    "page": 31,
                    "text": (
                        "Producto: BETA\nPrincipio activo: Semaglutida\n"
                        "Expediente: 5002\nSolicitud: Renovación.\n"
                        "Concepto: La Sala aprueba."
                    ),
                    "chunks": [
                        "Producto: BETA\nPrincipio activo: Semaglutida\n"
                        "Expediente: 5002\nSolicitud: Renovación.\n"
                        "Concepto: La Sala aprueba."
                    ],
                },
            ],
        )
        sync_regulatory_extractions(self.database_path)

        total = count_regulatory_records(
            self.database_path, missing_field="active_ingredient"
        )
        records = list_regulatory_records(
            self.database_path,
            missing_field="active_ingredient",
            limit=1,
            offset=0,
        )

        self.assertEqual(total, 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["product_name"], "ALFA")

    def test_search_diversifies_fragments_before_grouping_documents(self) -> None:
        diverse_path = Path(self.temp_dir.name) / "diverse.db"
        initialize_database(diverse_path)
        insert_document(
            diverse_path,
            DocumentMetadata(
                title="Acta dominante",
                url="https://www.invima.gov.co/biblioteca/download/dominante",
            ),
            "dominante",
            [
                {
                    "page": 1,
                    "chunks": [
                        f"semaglutida fragmento dominante {index}"
                        for index in range(400)
                    ],
                }
            ],
        )
        for index in range(20):
            insert_document(
                diverse_path,
                DocumentMetadata(
                    title=f"Acta breve {index}",
                    url=(
                        "https://www.invima.gov.co/biblioteca/download/"
                        f"breve-{index}"
                    ),
                ),
                f"breve-{index}",
                [
                    {
                        "page": 1,
                        "chunks": [f"semaglutida evidencia breve {index}"],
                    }
                ],
            )

        results = search_chunks(
            diverse_path,
            "semaglutida",
            top_k=360,
            max_chunks_per_document=3,
        )
        self.assertEqual(len({result.url for result in results}), 21)
        dominant = [
            result for result in results if result.title == "Acta dominante"
        ]
        self.assertEqual(len(dominant), 3)


if __name__ == "__main__":
    unittest.main()
