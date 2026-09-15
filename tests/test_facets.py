from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from services.database import connect, initialize_database, insert_document, sync_regulatory_extractions
from services.facets import FACET_KEYS, get_search_facets
from services.models import DocumentMetadata
from services.regulatory import RegulatoryRecord


class SearchFacetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "actas.db"
        initialize_database(self.database_path)
        self.chunk_ids: dict[str, int] = {}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _add(
        self,
        key: str,
        *,
        year: int,
        section: str,
        text: str = "La Sala analiza el beneficio cardiovascular.",
        outcome: str = "aprobado",
        request_type: str = "indicaciones",
        product: str = "Ozempic",
        active_ingredient: str = "Semaglutida",
        interested_party: str = "Novo Nordisk",
        chunks: list[str] | None = None,
        acta_number: str | None = None,
        part: str | None = None,
    ) -> None:
        page_chunks = chunks or [text]
        document_id = insert_document(
            self.database_path,
            DocumentMetadata(
                title=f"Acta {key} de {year}",
                url=f"https://invima.example/{key}",
                year=year,
                acta_number=acta_number or key,
                section=section,
                part=part,
            ),
            f"hash-{key}",
            [{"page": 1, "text": text, "chunks": page_chunks}],
        )
        record = RegulatoryRecord(
            producto=product,
            principio_activo=active_ingredient,
            interesado=interested_party,
            expediente=f"EXP-{key}",
            radicado=f"RAD-{key}",
            solicitud="Texto de solicitud",
            concepto=text,
            resultado_normalizado=outcome,
            pagina=1,
            pagina_final=1,
            tipo_solicitud=request_type,
        )
        with patch(
            "services.database.extract_regulatory_records", return_value=[record]
        ):
            summary = sync_regulatory_extractions(
                self.database_path, extractor_version="facet-fixture"
            )
        self.assertEqual(summary["documents_failed"], 0)
        with connect(self.database_path) as connection:
            self.chunk_ids[key] = int(
                connection.execute(
                    """
                    SELECT MIN(c.id)
                    FROM chunks c
                    JOIN pages p ON p.id = c.page_id
                    WHERE p.document_id = ?
                    """,
                    (document_id,),
                ).fetchone()[0]
            )

    @staticmethod
    def _mapping(summary, key: str) -> dict[str | int, int]:
        return {
            (item.label if item.label is not None else item.value): item.count
            for item in summary.facets[key]
        }

    def _populate(self) -> None:
        self._add("01", year=2026, section="SEMPB")
        self._add(
            "02",
            year=2025,
            section="SEMPB",
            outcome="rechazado",
            request_type="renovacion",
            product="Saxenda",
            active_ingredient="Liraglutida",
        )
        self._add(
            "03",
            year=2025,
            section="OTRA",
            product="Wegovy",
            interested_party="Laboratorio Acme",
        )
        self._add(
            "04",
            year=2024,
            section="SEMPB",
            text="Documento sin la consulta objetivo.",
            product="Producto ajeno",
            active_ingredient="Dulaglutida",
        )

    def test_textual_facets_cover_the_full_unpaginated_universe(self) -> None:
        self._populate()

        summary = get_search_facets(self.database_path, "beneficio cardiovascular")

        self.assertTrue(summary.exact)
        self.assertEqual(summary.scope, "full_textual")
        self.assertEqual(summary.total_documents, 3)
        self.assertEqual(summary.total_acts, 3)
        self.assertEqual(summary.total_fragments, 3)
        self.assertEqual(self._mapping(summary, "years"), {2026: 1, 2025: 2})
        self.assertEqual(self._mapping(summary, "sections"), {"SEMPB": 2, "OTRA": 1})
        self.assertEqual(self._mapping(summary, "outcomes"), {"aprobado": 2, "rechazado": 1})
        self.assertEqual(
            self._mapping(summary, "request_types"),
            {"indicaciones": 2, "renovacion": 1},
        )
        self.assertEqual(
            self._mapping(summary, "active_ingredients"),
            {"Semaglutida": 2, "Liraglutida": 1},
        )
        self.assertEqual(summary.metadata["years"].unit, "acts")
        self.assertTrue(summary.metadata["years"].exact)

    def test_each_dimension_ignores_itself_and_respects_other_filters(self) -> None:
        self._populate()

        summary = get_search_facets(
            self.database_path,
            "beneficio",
            filters={"years": [2025], "outcomes": ["aprobado"]},
        )

        # La interseccion completa deja solamente Acta 03.
        self.assertEqual(summary.total_documents, 1)
        # Año omite solo el año seleccionado y conserva resultado=aprobado.
        self.assertEqual(self._mapping(summary, "years"), {2026: 1, 2025: 1})
        # Resultado omite solo resultado y conserva año=2025.
        self.assertEqual(
            self._mapping(summary, "outcomes"),
            {"aprobado": 1, "rechazado": 1},
        )
        self.assertEqual(self._mapping(summary, "sections"), {"OTRA": 1})
        self.assertEqual(
            self._mapping(summary, "request_types"), {"indicaciones": 1}
        )

    def test_structured_filters_interact_on_the_matching_page(self) -> None:
        self._populate()

        summary = get_search_facets(
            self.database_path,
            "beneficio",
            filters={
                "active_ingredients": ["semaglutida"],
                "interested_parties": ["novo"],
            },
        )

        self.assertEqual(summary.total_documents, 1)
        self.assertEqual(self._mapping(summary, "products"), {"Ozempic": 1})
        # La faceta de interesado se autoexcluye, pero mantiene semaglutida.
        self.assertEqual(
            self._mapping(summary, "interested_parties"),
            {"Laboratorio Acme": 1, "Novo Nordisk": 1},
        )
        self.assertEqual(
            summary.metadata["products"].basis,
            "record_overlapping_matching_page",
        )

    def test_record_facets_never_mix_two_decisions_on_the_same_page(self) -> None:
        insert_document(
            self.database_path,
            DocumentMetadata(
                title="Acta con dos decisiones",
                url="https://invima.example/two-records",
                year=2026,
                acta_number="20",
                section="SEMPB",
            ),
            "hash-two-records",
            [
                {
                    "page": 1,
                    "text": "La Sala analiza el beneficio de dos productos.",
                    "chunks": ["La Sala analiza el beneficio de dos productos."],
                }
            ],
        )
        records = [
            RegulatoryRecord(
                producto="Producto A",
                principio_activo="Semaglutida",
                interesado="Titular A",
                expediente="EXP-A",
                radicado="RAD-A",
                solicitud="Solicitud A",
                concepto="Concepto A aprobado",
                resultado_normalizado="aprobado",
                pagina=1,
                pagina_final=1,
                tipo_solicitud="indicaciones",
            ),
            RegulatoryRecord(
                producto="Producto B",
                principio_activo="Liraglutida",
                interesado="Titular B",
                expediente="EXP-B",
                radicado="RAD-B",
                solicitud="Solicitud B",
                concepto="Concepto B rechazado",
                resultado_normalizado="rechazado",
                pagina=1,
                pagina_final=1,
                tipo_solicitud="renovacion",
            ),
        ]
        with patch(
            "services.database.extract_regulatory_records", return_value=records
        ):
            sync_regulatory_extractions(
                self.database_path, extractor_version="facet-two-records"
            )

        summary = get_search_facets(
            self.database_path,
            "beneficio",
            filters={"active_ingredients": ["semaglutida"]},
        )

        self.assertEqual(self._mapping(summary, "outcomes"), {"aprobado": 1})
        self.assertEqual(self._mapping(summary, "products"), {"Producto A": 1})
        self.assertEqual(
            self._mapping(summary, "request_types"), {"indicaciones": 1}
        )

    def test_bounded_semantic_candidates_do_not_require_a_text_match(self) -> None:
        self._populate()

        summary = get_search_facets(
            self.database_path,
            "palabras que no existen",
            candidate_chunk_ids=(self.chunk_ids["01"], self.chunk_ids["02"]),
        )

        self.assertFalse(summary.exact)
        self.assertEqual(summary.scope, "bounded_candidates")
        self.assertEqual(summary.candidate_limit, 2)
        self.assertEqual(summary.total_documents, 2)
        self.assertEqual(self._mapping(summary, "years"), {2026: 1, 2025: 1})
        self.assertFalse(summary.metadata["outcomes"].exact)

        forced = get_search_facets(
            self.database_path,
            "consulta irrelevante",
            candidate_chunk_ids=(self.chunk_ids["01"],),
            counts_exact=True,
        )
        self.assertFalse(forced.exact)

    def test_high_cardinality_facets_are_limited_and_marked_truncated(self) -> None:
        self._populate()

        summary = get_search_facets(
            self.database_path, "beneficio", max_values=1
        )

        self.assertEqual(len(summary.facets["products"]), 1)
        self.assertTrue(summary.metadata["products"].truncated)
        self.assertEqual(summary.metadata["products"].limit, 1)
        self.assertFalse(summary.metadata["years"].truncated)

    def test_compound_values_use_individual_evidence_with_per_record_fallback(self) -> None:
        self._add(
            "51",
            year=2026,
            section="SEMPB",
            active_ingredient="Semaglutida + Cagrilintida",
            product="Producto Uno + Producto Dos",
            interested_party="Titular Uno + Titular Dos",
        )
        self._add(
            "52",
            year=2025,
            section="SEMPB",
            active_ingredient="Liraglutida",
            product="Saxenda",
            interested_party="Titular Respaldo",
        )
        with connect(self.database_path) as connection:
            record_id = int(
                connection.execute(
                    """
                    SELECT rr.id
                    FROM regulatory_records rr
                    JOIN documents d ON d.id = rr.document_id
                    WHERE d.acta_number = '51'
                    """
                ).fetchone()[0]
            )
            evidence = (
                ("principio_activo", 0, "Semaglutida", "semaglutida"),
                ("principio_activo", 1, "Cagrilintida", "cagrilintida"),
                # La misma entidad repetida no debe duplicar el conteo.
                ("principio_activo", 2, "Semaglutida", "semaglutida"),
                ("producto", 0, "Producto Uno", "producto uno"),
                ("producto", 1, "Producto Dos", "producto dos"),
                ("interesado", 0, "Titular Uno", "titular uno"),
                ("interesado", 1, "Titular Dos", "titular dos"),
            )
            connection.executemany(
                """
                INSERT INTO regulatory_field_evidence (
                    record_id, field_name, ordinal, literal_value,
                    normalized_value, canonical_value, page_number,
                    end_page_number, evidence_text, extraction_method,
                    confidence
                ) VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, 'explicit_label', 0.98)
                """,
                (
                    (
                        record_id,
                        field_name,
                        ordinal,
                        label,
                        normalized,
                        normalized,
                        f"{field_name}: {label}",
                    )
                    for field_name, ordinal, label, normalized in evidence
                ),
            )

        summary = get_search_facets(self.database_path, "beneficio")

        self.assertEqual(
            self._mapping(summary, "active_ingredients"),
            {"Cagrilintida": 1, "Liraglutida": 1, "Semaglutida": 1},
        )
        self.assertNotIn(
            "Semaglutida + Cagrilintida",
            self._mapping(summary, "active_ingredients"),
        )
        self.assertEqual(
            self._mapping(summary, "products"),
            {"Producto Dos": 1, "Producto Uno": 1, "Saxenda": 1},
        )
        self.assertEqual(
            self._mapping(summary, "interested_parties"),
            {"Titular Dos": 1, "Titular Respaldo": 1, "Titular Uno": 1},
        )

    def test_compound_facet_exposes_filter_key_separately_from_literal_label(self) -> None:
        self._add(
            "53",
            year=2026,
            section="SEMPB",
            active_ingredient="Paracetamol",
            product="Producto con dosis",
            interested_party="Titular",
        )
        with connect(self.database_path) as connection:
            record_id = int(
                connection.execute(
                    """
                    SELECT rr.id
                    FROM regulatory_records rr
                    JOIN documents d ON d.id = rr.document_id
                    WHERE d.acta_number = '53'
                    """
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO regulatory_field_evidence (
                    record_id, field_name, ordinal, literal_value,
                    normalized_value, canonical_value, page_number,
                    end_page_number, evidence_text, extraction_method,
                    confidence
                ) VALUES (
                    ?, 'principio_activo', 0, 'Acetaminofén 500 mg',
                    'paracetamol 500 mg', 'acetaminofen', 1, 1,
                    'Principio activo: Acetaminofén 500 mg',
                    'explicit_label', 0.99
                )
                """,
                (record_id,),
            )

        summary = get_search_facets(self.database_path, "beneficio")
        ingredient = summary.facets["active_ingredients"][0]

        self.assertEqual(ingredient.value, "acetaminofen")
        self.assertEqual(ingredient.label, "Acetaminofén 500 mg")
        self.assertEqual(ingredient.count, 1)

        # La clave canonical no aparece en el consolidado ('paracetamol'). La
        # selección de la UI debe seguir encontrando la ficha por su evidencia.
        filtered = get_search_facets(
            self.database_path,
            "beneficio",
            filters={"active_ingredients": [ingredient.value]},
        )
        self.assertEqual(filtered.total_acts, 1)
        self.assertEqual(filtered.total_documents, 1)

    def test_over_budget_textual_facets_degrade_explicitly(self) -> None:
        self._add(
            "30",
            year=2021,
            section="SEMPB",
            text="beneficio uno beneficio dos beneficio tres beneficio cuatro",
            chunks=[
                "beneficio uno",
                "beneficio dos",
                "beneficio tres",
                "beneficio cuatro",
            ],
        )

        exact = get_search_facets(
            self.database_path,
            "beneficio",
            exact_chunk_budget=10,
        )
        bounded = get_search_facets(
            self.database_path,
            "beneficio",
            exact_chunk_budget=3,
            bounded_chunk_limit=2,
        )

        self.assertTrue(exact.exact)
        self.assertEqual(exact.total_fragments, 4)
        self.assertFalse(bounded.exact)
        self.assertEqual(bounded.scope, "bounded_textual")
        self.assertEqual(bounded.candidate_limit, 2)
        self.assertEqual(bounded.total_fragments, 2)
        self.assertEqual(bounded.total_acts, 1)
        self.assertFalse(bounded.metadata["years"].exact)

    def test_broad_synthetic_query_stays_within_interactive_budget(self) -> None:
        pages = [
            {
                "page": page_number,
                "text": " ".join(f"beneficio marcador {index}" for index in range(200)),
                "chunks": [
                    f"beneficio marcador {page_number}-{index}"
                    for index in range(200)
                ],
            }
            for page_number in range(1, 61)
        ]
        insert_document(
            self.database_path,
            DocumentMetadata(
                title="Acta sintética amplia",
                url="https://invima.example/broad",
                year=2020,
                acta_number="99",
                section="SEMPB",
            ),
            "hash-broad",
            pages,
        )

        started = time.monotonic()
        summary = get_search_facets(
            self.database_path,
            "beneficio",
            exact_chunk_budget=1_000,
            bounded_chunk_limit=500,
        )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 5.0)
        self.assertFalse(summary.exact)
        self.assertEqual(summary.total_fragments, 500)
        self.assertEqual(summary.total_documents, 1)

    def test_field_search_facets_use_the_same_matching_record(self) -> None:
        self._populate()

        summary = get_search_facets(
            self.database_path,
            "semaglutida",
            field_scope="active_ingredient",
        )

        self.assertEqual(summary.total_documents, 2)
        self.assertEqual(self._mapping(summary, "outcomes"), {"aprobado": 2})
        self.assertEqual(self._mapping(summary, "products"), {"Ozempic": 1, "Wegovy": 1})
        self.assertEqual(summary.metadata["products"].basis, "matching_record")

    def test_exact_phrase_facets_can_cross_chunks_on_one_page(self) -> None:
        self._add(
            "09",
            year=2023,
            section="SEMPB",
            text="La Sala considera el balance beneficio riesgo favorable.",
            chunks=[
                "La Sala considera el balance beneficio",
                "riesgo favorable.",
            ],
        )

        summary = get_search_facets(
            self.database_path,
            "balance beneficio riesgo favorable",
            exact_phrase=True,
        )

        self.assertEqual(summary.total_documents, 1)
        self.assertEqual(self._mapping(summary, "years"), {2023: 1})

    def test_common_exact_phrase_is_bounded_and_never_claims_exactness(self) -> None:
        insert_document(
            self.database_path,
            DocumentMetadata(
                title="Acta con frase repetida",
                url="https://invima.example/common-phrase",
                year=2023,
                acta_number="44",
                section="SEMPB",
            ),
            "hash-common-phrase",
            [
                {
                    "page": page,
                    "text": "la sala considera favorable el beneficio riesgo",
                    "chunks": [
                        "la sala considera favorable el beneficio",
                        "riesgo",
                    ],
                }
                for page in range(1, 7)
            ],
        )

        summary = get_search_facets(
            self.database_path,
            "la sala considera favorable",
            exact_phrase=True,
            exact_page_budget=3,
            bounded_page_limit=2,
        )

        self.assertFalse(summary.exact)
        self.assertEqual(summary.scope, "bounded_phrase")
        self.assertEqual(summary.candidate_limit, 2)
        self.assertEqual(summary.total_fragments, 2)
        self.assertFalse(summary.metadata["years"].exact)

    def test_query_alternatives_expand_the_same_textual_universe(self) -> None:
        self._add(
            "10",
            year=2022,
            section="SEMPB",
            text="Se verificaron las buenas practicas de manufactura.",
            active_ingredient="Dulaglutida",
        )

        without_expansion = get_search_facets(self.database_path, "BPM")
        expanded = get_search_facets(
            self.database_path,
            "BPM",
            query_alternatives=("buenas practicas de manufactura",),
        )

        self.assertEqual(without_expansion.total_documents, 0)
        self.assertEqual(expanded.total_documents, 1)
        self.assertEqual(self._mapping(expanded, "years"), {2022: 1})

    def test_two_pdf_parts_of_one_act_count_as_one_act(self) -> None:
        self._add(
            "12-parte-1",
            year=2024,
            section="SEMPB",
            acta_number="12",
            part="1",
            product="Producto dividido",
        )
        self._add(
            "12-parte-2",
            year=2024,
            section="SEMPB",
            acta_number="12",
            part="2",
            product="Producto dividido",
        )

        summary = get_search_facets(self.database_path, "beneficio")

        self.assertEqual(summary.total_documents, 2)
        self.assertEqual(summary.total_acts, 1)
        self.assertEqual(self._mapping(summary, "years"), {2024: 1})
        self.assertEqual(self._mapping(summary, "sections"), {"SEMPB": 1})
        self.assertEqual(self._mapping(summary, "products"), {"Producto dividido": 1})

    def test_missing_act_identity_falls_back_to_each_document(self) -> None:
        for suffix in ("a", "b"):
            insert_document(
                self.database_path,
                DocumentMetadata(
                    title=f"Documento incompleto {suffix}",
                    url=f"https://invima.example/incomplete-{suffix}",
                    year=2020,
                    acta_number=None,
                    section="SEMPB",
                ),
                f"hash-incomplete-{suffix}",
                [
                    {
                        "page": 1,
                        "text": "beneficio clinico",
                        "chunks": ["beneficio clinico"],
                    }
                ],
            )

        summary = get_search_facets(self.database_path, "beneficio")

        self.assertEqual(summary.total_documents, 2)
        self.assertEqual(summary.total_acts, 2)
        self.assertEqual(self._mapping(summary, "years"), {2020: 2})

    def test_legacy_database_without_structured_tables_keeps_document_facets(self) -> None:
        with connect(self.database_path) as connection:
            connection.execute("DROP TABLE regulatory_records_fts")
            connection.execute("DROP TABLE regulatory_field_evidence")
            connection.execute("DROP TABLE regulatory_records")
            connection.execute("DROP TABLE document_extractions")
            connection.execute(
                "UPDATE app_metadata SET value='6' WHERE key='schema_version'"
            )
        insert_document(
            self.database_path,
            DocumentMetadata(
                title="Acta heredada",
                url="https://invima.example/legacy",
                year=2020,
                acta_number="01",
                section="SEMPB",
            ),
            "legacy-hash",
            [{"page": 1, "text": "beneficio clinico", "chunks": ["beneficio clinico"]}],
        )

        summary = get_search_facets(self.database_path, "beneficio")

        self.assertEqual(summary.total_documents, 1)
        self.assertEqual(self._mapping(summary, "years"), {2020: 1})
        for key in FACET_KEYS[2:]:
            self.assertEqual(summary.facets[key], ())

    def test_rejects_unknown_field_and_unbounded_top_n(self) -> None:
        with self.assertRaises(ValueError):
            get_search_facets(self.database_path, "beneficio", field_scope="inventado")
        with self.assertRaises(ValueError):
            get_search_facets(self.database_path, "beneficio", max_values=501)
        with self.assertRaises(ValueError):
            get_search_facets(
                self.database_path, "beneficio", exact_chunk_budget=0
            )
        with self.assertRaises(ValueError):
            get_search_facets(
                self.database_path, "beneficio", bounded_chunk_limit=0
            )
        with self.assertRaises(ValueError):
            get_search_facets(
                self.database_path, "beneficio", exact_page_budget=0
            )
        with self.assertRaises(ValueError):
            get_search_facets(
                self.database_path, "beneficio", bounded_page_limit=0
            )

    def test_filter_values_are_parameters_not_sql(self) -> None:
        self._add("11", year=2021, section="SEMPB")

        summary = get_search_facets(
            self.database_path,
            "beneficio",
            filters={"sections": ["SEMPB' OR 1=1 --"]},
        )

        self.assertEqual(summary.total_documents, 0)
        with connect(self.database_path) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
                1,
            )


if __name__ == "__main__":
    unittest.main()
