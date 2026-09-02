from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from services.catalog import (
    CatalogRecord,
    catalog_id_for,
    load_catalog,
    manifest_rows,
    merge_catalog,
    parse_catalog_html,
    validate_discovery,
    write_catalog,
)
from services.models import DocumentMetadata


SOURCE_URL = (
    "https://www.invima.gov.co/productos-vigilados/medicamentos-y-productos-"
    "biologicos/sala-especializada-medicamentos-sintesis"
)
ALLOWED_HOSTS = ("www.invima.gov.co",)


class CatalogTests(unittest.TestCase):
    @staticmethod
    def _historical_records(per_year: int = 20) -> list[CatalogRecord]:
        records = []
        for year in range(2013, datetime.now().year + 1):
            for number in range(1, per_year + 1):
                records.append(
                    CatalogRecord(
                        catalog_id=catalog_id_for(year, str(number), "SEMPB", None),
                        title=f"Acta No {number:02d} de {year} SEMPB",
                        published_title=f"Acta No {number:02d} de {year} SEMPB",
                        url=(
                            "https://www.invima.gov.co/biblioteca/download/"
                            f"{year}-{number}"
                        ),
                        year=year,
                        acta_number=f"{number:02d}",
                        section="SEMPB",
                    )
                )
        return records

    def test_parses_years_parts_dates_context_and_duplicates(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "catalog_page.html"
        records = parse_catalog_html(
            fixture.read_text(encoding="utf-8"),
            SOURCE_URL,
            ALLOWED_HOSTS,
        )
        by_id = {record.catalog_id: record for record in records}

        first_2026 = by_id[catalog_id_for(2026, "08", "SEMPB", "Primera Parte")]
        second_2026 = by_id[catalog_id_for(2026, "08", "SEMPB", "Segunda Parte")]
        self.assertEqual(first_2026.publication_date, "2026-08-31")
        self.assertEqual(second_2026.page_occurrences, 2)

        inferred_first = by_id[
            catalog_id_for(2024, "02", "SEMNNIMB", "Primera Parte")
        ]
        self.assertEqual(inferred_first.part, "Primera Parte")
        self.assertEqual(
            by_id[catalog_id_for(2023, "01", "CONJUNTA", None)].section,
            "CONJUNTA",
        )
        self.assertEqual(
            by_id[catalog_id_for(2013, "01", "SEMPB", None)].year,
            2013,
        )
        self.assertIn(
            catalog_id_for(2017, "03", "SEMPB", "Primera Parte V1"),
            by_id,
        )
        self.assertFalse(any(record.acta_number == "99" for record in records))

    def test_merge_is_append_only_and_prefers_curated_url(self) -> None:
        curated_url = "https://www.invima.gov.co/biblioteca/download/curated"
        page_url = "https://www.invima.gov.co/biblioteca/page-entry"
        document = DocumentMetadata(
            title="Acta No 08 de 2026 SEMPB Primera Parte",
            url=curated_url,
            year=2026,
            acta_number="08",
            section="SEMPB",
            part="Primera Parte",
        )
        discovered = CatalogRecord(
            catalog_id=catalog_id_for(2026, "08", "SEMPB", "Primera Parte"),
            title=document.title,
            published_title=document.title,
            url=page_url,
            year=2026,
            acta_number="08",
            section="SEMPB",
            part="Primera Parte",
            source_page_url=SOURCE_URL,
        )
        old = CatalogRecord(
            catalog_id=catalog_id_for(2020, "01", "SEMNNIMB", None),
            title="Acta No 01 de 2020 SEMNNIMB",
            published_title="Acta No 01 de 2020 SEMNNIMB",
            url="https://www.invima.gov.co/biblioteca/download/old",
            year=2020,
            acta_number="01",
            section="SEMNNIMB",
            listing_status="listed",
        )

        merged = merge_catalog(
            [old],
            [discovered],
            [document],
            SOURCE_URL,
            observed_at="2026-09-01T00:00:00+00:00",
        )
        by_id = {record.catalog_id: record for record in merged}
        current = by_id[discovered.catalog_id]
        self.assertEqual(current.url, curated_url)
        self.assertIn(page_url, current.alternate_urls)
        self.assertEqual(current.listing_status, "listed")
        self.assertEqual(by_id[old.catalog_id].listing_status, "unlisted")

    def test_catalog_round_trip_and_manifest_url_deduplication(self) -> None:
        shared_url = "https://www.invima.gov.co/biblioteca/download/shared"
        records = [
            CatalogRecord(
                catalog_id=catalog_id_for(2026, number, "SEMPB", None),
                title=f"Acta No {number} de 2026 SEMPB",
                published_title=f"Acta No {number} de 2026 SEMPB",
                url=shared_url,
                year=2026,
                acta_number=number,
                section="SEMPB",
            )
            for number in ("01", "02")
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.csv"
            write_catalog(records, path)
            loaded = load_catalog(path)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(len(manifest_rows(loaded)), 1)

    def test_manifest_can_keep_older_records_outside_text_index(self) -> None:
        records = [
            CatalogRecord(
                catalog_id=catalog_id_for(year, "01", "SEMPB", None),
                title=f"Acta No 01 de {year} SEMPB",
                published_title=f"Acta No 01 de {year} SEMPB",
                url=f"https://www.invima.gov.co/biblioteca/download/{year}",
                year=year,
                acta_number="01",
                section="SEMPB",
            )
            for year in (2013, 2020)
        ]
        rows = manifest_rows(records, minimum_year=2020)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["year"], 2020)

    def test_discovery_rejects_a_missing_closed_year_even_above_minimum(self) -> None:
        records = [
            record for record in self._historical_records() if record.year != 2018
        ]
        self.assertGreaterEqual(len(records), 250)

        with self.assertRaisesRegex(ValueError, "2018"):
            validate_discovery(records)

    def test_discovery_rejects_a_large_per_year_collapse(self) -> None:
        previous = self._historical_records(per_year=25)
        current = [
            record
            for record in previous
            if record.year != 2020 or record.acta_number == "01"
        ]

        with self.assertRaisesRegex(ValueError, "2020"):
            validate_discovery(current, previous)


if __name__ == "__main__":
    unittest.main()
