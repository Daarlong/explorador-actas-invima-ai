from __future__ import annotations

import csv
import hashlib
import json
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from services.manifest import is_allowed_url
from services.models import DocumentMetadata


CATALOG_FIELDS = (
    "catalog_id",
    "title",
    "published_title",
    "url",
    "year",
    "acta_number",
    "section",
    "part",
    "publication_date",
    "publication_date_raw",
    "source_type",
    "source_page_url",
    "listing_status",
    "first_seen_at",
    "last_seen_at",
    "page_occurrences",
    "alternate_urls",
)

CATALOG_PARSER_VERSION = "2"

MANIFEST_FIELDS = (
    "title",
    "url",
    "year",
    "acta_number",
    "section",
    "part",
    "source_type",
    "publication_date",
    "source_page_url",
    "catalog_id",
    "listing_status",
)

PART_NAMES = (
    "Decimonovena",
    "Decimoctava",
    "Decimoséptima",
    "Decimoseptima",
    "Decimosexta",
    "Decimoquinta",
    "Decimocuarta",
    "Decimotercera",
    "Duodécima",
    "Duodecima",
    "Undécima",
    "Undecima",
    "Décima",
    "Decima",
    "Novena",
    "Octava",
    "Séptima",
    "Septima",
    "Sexta",
    "Quinta",
    "Cuarta",
    "Tercera",
    "Segunda",
    "Primera",
)

ACTA_PATTERN = re.compile(
    r"\b(?:Publicar\s+)?Acta(?:\s+Conjunta)?\s+"
    r"(?:(?:No\.?|N[.º°]?)\s*)?(\d+)",
    flags=re.IGNORECASE,
)
YEAR_PATTERN = re.compile(r"\bde\s+(20\d{2})\b", flags=re.IGNORECASE)
PART_PATTERN = re.compile(
    rf"\b(?P<name>{'|'.join(PART_NAMES)})\s*"
    r"(?:(?P<version>V\d+)\s*)?Parte(?:\s+(?P<suffix>[AB]))?\b",
    flags=re.IGNORECASE,
)
PUBLICATION_PATTERN = re.compile(
    r"Fecha\s+de\s+Publicaci[oó]n\s*:?\s*"
    r"(?P<date>\d{1,2}[/-]\d{1,2}[/-]\d{4})",
    flags=re.IGNORECASE,
)


def _clean(value: str | None) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


def _plain(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char)).lower()


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", _plain(value)).strip("-")
    return result or "completa"


def _normalize_part(value: str) -> str | None:
    match = PART_PATTERN.search(value)
    if not match:
        return None
    name = _plain(match.group("name")).title()
    accents = {
        "Septima": "Séptima",
        "Decima": "Décima",
        "Undecima": "Undécima",
        "Duodecima": "Duodécima",
        "Decimoseptima": "Decimoséptima",
    }
    name = accents.get(name, name)
    part = f"{name} Parte"
    if match.group("version"):
        part += f" {match.group('version').upper()}"
    if match.group("suffix"):
        part += f" {match.group('suffix').upper()}"
    return part


def _publication_date(value: str) -> tuple[str, str]:
    match = PUBLICATION_PATTERN.search(value)
    if not match:
        return "", ""
    raw = match.group("date")
    first, second, year = (int(item) for item in re.split(r"[/-]", raw))
    if first > 12:
        day, month = first, second
    elif second > 12:
        month, day = first, second
    else:
        day, month = first, second
    try:
        return date(year, month, day).isoformat(), raw
    except ValueError:
        return "", raw


def _section_from_title(title: str, context: str | None, year: int) -> str:
    upper = title.upper()
    acronyms = [
        acronym
        for acronym in ("SEMNNIMB", "SEMPB", "SEMSQB", "SEMDMRDI", "SEDMRDI")
        if re.search(rf"\b{acronym}\b", upper)
    ]
    if re.search(r"\bSEM\b", upper):
        acronyms.append("SEM")
    if "CONJUNTA" in upper or context == "CONJUNTA":
        return "CONJUNTA" + (f" {'-'.join(dict.fromkeys(acronyms))}" if acronyms else "")
    if acronyms:
        return acronyms[0]
    # Las actas antiguas sin sigla pertenecen al bloque histórico SEMPB.
    return "SEMPB" if year <= 2017 else (context or "SEMPB")


def catalog_id_for(
    year: int,
    acta_number: str,
    section: str,
    part: str | None,
) -> str:
    return f"{year}-{_slug(section)}-{acta_number.zfill(2)}-{_slug(part or 'completa')}"


@dataclass(frozen=True)
class CatalogRecord:
    catalog_id: str
    title: str
    published_title: str
    url: str
    year: int
    acta_number: str
    section: str
    part: str | None = None
    publication_date: str = ""
    publication_date_raw: str = ""
    source_type: str = "official"
    source_page_url: str = ""
    listing_status: str = "listed"
    first_seen_at: str = ""
    last_seen_at: str = ""
    page_occurrences: int = 1
    alternate_urls: tuple[str, ...] = ()

    def as_csv_dict(self) -> dict[str, str | int]:
        values = asdict(self)
        values["part"] = self.part or ""
        values["alternate_urls"] = "|".join(self.alternate_urls)
        return values


@dataclass(frozen=True)
class _ActaEvent:
    text: str
    href: str
    year_hint: int | None
    context: str | None


class _CatalogHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.catalog_started = False
        self.current_year: int | None = None
        self.current_context: str | None = None
        self.anchor_href: str | None = None
        self.anchor_parts: list[str] = []
        self.ignored_depth = 0
        self.events: list[_ActaEvent] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag in {"script", "style", "noscript"}:
            self.ignored_depth += 1
            return
        if self.ignored_depth:
            return
        if tag == "a":
            self.anchor_href = dict(attrs).get("href") or ""
            self.anchor_parts = []

    def _update_context(self, text: str) -> None:
        plain = _plain(text)
        if "pronunciamientos salas conjuntas" in plain:
            self.current_context = "CONJUNTA"
        elif "actas y agendas" in plain:
            self.catalog_started = True
            self.current_context = None

    def _consume_text(self, text: str, href: str = "") -> None:
        clean = _clean(text)
        if not clean or len(clean) > 600:
            return
        self._update_context(clean)
        if not self.catalog_started:
            return
        if re.fullmatch(r"20\d{2}", clean):
            self.current_year = int(clean)
            return
        if ACTA_PATTERN.search(clean):
            self.events.append(
                _ActaEvent(clean, href, self.current_year, self.current_context)
            )

    def handle_data(self, data: str) -> None:
        if self.ignored_depth:
            return
        if self.anchor_href is not None:
            self.anchor_parts.append(data)
        else:
            self._consume_text(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self.ignored_depth:
            self.ignored_depth -= 1
            return
        if self.ignored_depth:
            return
        if tag == "a" and self.anchor_href is not None:
            self._consume_text(" ".join(self.anchor_parts), self.anchor_href)
            self.anchor_href = None
            self.anchor_parts = []


def _record_from_event(
    event: _ActaEvent,
    source_page_url: str,
    allowed_hosts: tuple[str, ...],
) -> CatalogRecord | None:
    acta_match = ACTA_PATTERN.search(event.text)
    if not acta_match:
        return None
    year_match = YEAR_PATTERN.search(event.text)
    year = int(year_match.group(1)) if year_match else event.year_hint
    if year is None or not 2010 <= year <= datetime.now().year + 1:
        return None
    number = acta_match.group(1).zfill(2)
    part = _normalize_part(event.text)
    section = _section_from_title(event.text, event.context, year)
    publication_date, publication_date_raw = _publication_date(event.text)
    published_title = PUBLICATION_PATTERN.split(event.text, maxsplit=1)[0]
    published_title = re.sub(
        r"^Publicar\s+", "", _clean(published_title).strip(" -"), flags=re.IGNORECASE
    )
    standard_title = f"Acta No {number} de {year} {section}"
    if part:
        standard_title += f" {part}"
    absolute_url = urljoin(source_page_url, event.href) if event.href else ""
    if absolute_url and not is_allowed_url(absolute_url, allowed_hosts):
        absolute_url = ""
    return CatalogRecord(
        catalog_id=catalog_id_for(year, number, section, part),
        title=standard_title,
        published_title=published_title,
        url=absolute_url,
        year=year,
        acta_number=number,
        section=section,
        part=part,
        publication_date=publication_date,
        publication_date_raw=publication_date_raw,
        source_page_url=source_page_url,
    )


def _infer_implicit_first_parts(records: list[CatalogRecord]) -> list[CatalogRecord]:
    grouped: dict[tuple[int, str, str], list[CatalogRecord]] = {}
    for record in records:
        grouped.setdefault(
            (record.year, record.section, record.acta_number), []
        ).append(record)

    updated: list[CatalogRecord] = []
    for group in grouped.values():
        has_explicit_parts = any(record.part for record in group)
        for record in group:
            if has_explicit_parts and not record.part:
                part = "Primera Parte"
                updated.append(
                    replace(
                        record,
                        part=part,
                        title=f"Acta No {record.acta_number} de {record.year} "
                        f"{record.section} {part}",
                        catalog_id=catalog_id_for(
                            record.year,
                            record.acta_number,
                            record.section,
                            part,
                        ),
                    )
                )
            else:
                updated.append(record)
    return updated


def parse_catalog_html(
    html: str,
    source_page_url: str,
    allowed_hosts: tuple[str, ...],
) -> list[CatalogRecord]:
    parser = _CatalogHtmlParser()
    parser.feed(html)
    parsed = [
        record
        for event in parser.events
        if (record := _record_from_event(event, source_page_url, allowed_hosts))
    ]
    parsed = _infer_implicit_first_parts(parsed)

    groups: dict[str, list[CatalogRecord]] = {}
    for record in parsed:
        groups.setdefault(record.catalog_id, []).append(record)

    deduplicated: list[CatalogRecord] = []
    for catalog_id, group in groups.items():
        primary = next((item for item in group if item.url), group[0])
        urls = list(dict.fromkeys(item.url for item in group if item.url))
        deduplicated.append(
            replace(
                primary,
                catalog_id=catalog_id,
                page_occurrences=len(group),
                alternate_urls=tuple(url for url in urls if url != primary.url),
            )
        )
    return sorted(deduplicated, key=_catalog_sort_key)


def fetch_catalog_html(
    source_page_url: str,
    allowed_hosts: tuple[str, ...],
    *,
    max_bytes: int = 12 * 1024 * 1024,
    timeout: int = 60,
) -> str:
    if not is_allowed_url(source_page_url, allowed_hosts):
        raise ValueError(f"Dominio de catálogo no permitido: {source_page_url}")
    for attempt in range(3):
        request = Request(
            source_page_url,
            headers={
                "User-Agent": "Explorador-Actas-INVIMA/1.2",
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310
                resolved_url = str(response.geturl())
                if not is_allowed_url(resolved_url, allowed_hosts):
                    raise ValueError(
                        "El catálogo redirigió a un dominio no permitido: "
                        f"{resolved_url}"
                    )
                payload = response.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise ValueError("La página del catálogo supera el tamaño permitido")
            return payload.decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("No fue posible consultar el catálogo")


def load_catalog(path: Path) -> list[CatalogRecord]:
    if not path.exists():
        return []
    records: list[CatalogRecord] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "catalog_id" not in reader.fieldnames:
            raise ValueError("El catálogo no contiene la columna catalog_id")
        for row in reader:
            if not (row.get("catalog_id") or "").strip():
                continue
            records.append(
                CatalogRecord(
                    catalog_id=(row.get("catalog_id") or "").strip(),
                    title=(row.get("title") or "").strip(),
                    published_title=(row.get("published_title") or "").strip(),
                    url=(row.get("url") or "").strip(),
                    year=int(row["year"]),
                    acta_number=(row.get("acta_number") or "").strip().zfill(2),
                    section=(row.get("section") or "SEMPB").strip(),
                    part=(row.get("part") or "").strip() or None,
                    publication_date=(row.get("publication_date") or "").strip(),
                    publication_date_raw=(
                        row.get("publication_date_raw") or ""
                    ).strip(),
                    source_type=(row.get("source_type") or "official").strip(),
                    source_page_url=(row.get("source_page_url") or "").strip(),
                    listing_status=(row.get("listing_status") or "listed").strip(),
                    first_seen_at=(row.get("first_seen_at") or "").strip(),
                    last_seen_at=(row.get("last_seen_at") or "").strip(),
                    page_occurrences=int(row.get("page_occurrences") or 1),
                    alternate_urls=tuple(
                        value
                        for value in (row.get("alternate_urls") or "").split("|")
                        if value
                    ),
                )
            )
    return records


def _record_from_document(
    document: DocumentMetadata,
    source_page_url: str,
) -> CatalogRecord | None:
    if document.year is None or not document.acta_number:
        return None
    section = document.section or "SEMPB"
    return CatalogRecord(
        catalog_id=catalog_id_for(
            document.year,
            document.acta_number,
            section,
            document.part,
        ),
        title=document.title,
        published_title=document.title,
        url=document.url,
        year=document.year,
        acta_number=document.acta_number.zfill(2),
        section=section,
        part=document.part,
        source_type=document.source_type,
        source_page_url=source_page_url,
        listing_status="known",
    )


def merge_catalog(
    existing: list[CatalogRecord],
    discovered: list[CatalogRecord],
    manifest_documents: list[DocumentMetadata],
    source_page_url: str,
    *,
    observed_at: str | None = None,
    source_checked: bool = True,
) -> list[CatalogRecord]:
    observed_at = observed_at or datetime.now(timezone.utc).isoformat()
    merged = {record.catalog_id: record for record in existing}

    for document in manifest_documents:
        seeded = _record_from_document(document, source_page_url)
        if not seeded:
            continue
        current = merged.get(seeded.catalog_id)
        if current:
            alternatives = tuple(
                dict.fromkeys(
                    [*current.alternate_urls]
                    + ([seeded.url] if seeded.url and seeded.url != current.url else [])
                )
            )
            merged[seeded.catalog_id] = replace(
                current,
                alternate_urls=alternatives,
            )
        else:
            merged[seeded.catalog_id] = seeded

    discovered_ids = {record.catalog_id for record in discovered}
    if source_checked:
        for catalog_id, current in list(merged.items()):
            if catalog_id not in discovered_ids and current.listing_status in {
                "listed",
                "listed_without_url",
                "known",
            }:
                merged[catalog_id] = replace(current, listing_status="unlisted")

    for found in discovered:
        current = merged.get(found.catalog_id)
        if not current:
            merged[found.catalog_id] = replace(
                found,
                first_seen_at=observed_at,
                last_seen_at=observed_at,
            )
            continue

        primary_url = current.url or found.url
        alternatives = list(current.alternate_urls)
        for candidate in (found.url, *found.alternate_urls):
            if candidate and candidate != primary_url and candidate not in alternatives:
                alternatives.append(candidate)
        merged[found.catalog_id] = replace(
            found,
            title=current.title or found.title,
            url=primary_url,
            source_type=current.source_type if current.url else found.source_type,
            first_seen_at=current.first_seen_at or observed_at,
            last_seen_at=observed_at,
            listing_status="listed" if found.url else "listed_without_url",
            alternate_urls=tuple(alternatives),
        )

    return sorted(merged.values(), key=_catalog_sort_key)


def _part_sort_key(part: str | None) -> tuple[int, str]:
    if not part:
        return (0, "")
    plain = _plain(part)
    for index, name in enumerate(reversed(PART_NAMES), start=1):
        if _plain(name) in plain:
            return (index, plain)
    return (999, plain)


def _catalog_sort_key(record: CatalogRecord) -> tuple:
    return (
        record.year,
        record.section,
        int(record.acta_number),
        _part_sort_key(record.part),
        record.title,
    )


def write_catalog(records: list[CatalogRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CATALOG_FIELDS)
        writer.writeheader()
        writer.writerows(record.as_csv_dict() for record in records)
    temporary.replace(path)


def catalog_content_signature(records: list[CatalogRecord]) -> tuple[tuple, ...]:
    """Compara cambios documentales sin convertir cada revisión en un commit."""
    return tuple(
        (
            record.catalog_id,
            record.title,
            record.published_title,
            record.url,
            record.year,
            record.acta_number,
            record.section,
            record.part,
            record.publication_date,
            record.publication_date_raw,
            record.source_type,
            record.source_page_url,
            record.listing_status,
            record.page_occurrences,
            record.alternate_urls,
        )
        for record in sorted(records, key=_catalog_sort_key)
    )


def manifest_rows(
    records: list[CatalogRecord],
    *,
    minimum_year: int | None = None,
) -> list[dict[str, str | int]]:
    rows: list[dict[str, str | int]] = []
    used_urls: set[str] = set()
    for record in sorted(records, key=_catalog_sort_key):
        if minimum_year is not None and record.year < minimum_year:
            continue
        if not record.url or record.url in used_urls:
            continue
        used_urls.add(record.url)
        rows.append(
            {
                "title": record.title,
                "url": record.url,
                "year": record.year,
                "acta_number": record.acta_number,
                "section": record.section,
                "part": record.part or "",
                "source_type": record.source_type,
                "publication_date": record.publication_date,
                "source_page_url": record.source_page_url,
                "catalog_id": record.catalog_id,
                "listing_status": record.listing_status,
            }
        )
    return rows


def write_manifest_from_catalog(
    records: list[CatalogRecord],
    path: Path,
    *,
    minimum_year: int | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerows(manifest_rows(records, minimum_year=minimum_year))
    temporary.replace(path)


def validate_discovery(
    records: list[CatalogRecord],
    previous_records: list[CatalogRecord] | None = None,
) -> None:
    """Cierra de forma segura ante extracciones parciales de la página fuente."""

    years = {record.year for record in records}
    if len(records) < 250:
        raise ValueError(
            "La página produjo menos de 250 actas; se conserva el catálogo anterior"
        )
    if not years or min(years) > 2013:
        raise ValueError("La página no produjo el histórico esperado desde 2013")
    if max(years) < datetime.now().year - 1:
        raise ValueError("La página no produjo las actas de los años recientes")
    expected_closed_years = set(range(2013, datetime.now().year))
    missing_closed_years = sorted(expected_closed_years - years)
    if missing_closed_years:
        raise ValueError(
            "La página no produjo uno o más años cerrados del histórico: "
            + ", ".join(str(year) for year in missing_closed_years)
        )

    prior = [
        record
        for record in (previous_records or [])
        if record.listing_status.startswith("listed")
    ]
    if len(prior) >= 250:
        minimum_global = max(250, int(len(prior) * 0.75))
        if len(records) < minimum_global:
            raise ValueError(
                "La extracción cayó de forma anómala frente al último catálogo "
                f"válido ({len(records)} frente a {len(prior)} registros)."
            )
        prior_by_year: dict[int, int] = {}
        current_by_year: dict[int, int] = {}
        for record in prior:
            prior_by_year[record.year] = prior_by_year.get(record.year, 0) + 1
        for record in records:
            current_by_year[record.year] = current_by_year.get(record.year, 0) + 1
        collapsed = [
            year
            for year, count in prior_by_year.items()
            if year < datetime.now().year
            and count >= 3
            and current_by_year.get(year, 0) < max(1, int(count * 0.6))
        ]
        if collapsed:
            raise ValueError(
                "La extracción perdió una proporción anómala de publicaciones "
                "en los años: " + ", ".join(str(year) for year in sorted(collapsed))
            )


def build_source_snapshot(
    discovered: list[CatalogRecord],
    source_page_url: str,
    source_html: str,
) -> dict:
    """Crea evidencia compacta y reproducible de la última consulta válida."""

    years: dict[str, int] = {}
    for record in discovered:
        key = str(record.year)
        years[key] = years.get(key, 0) + 1
    return {
        "status": "valid",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_page_url": source_page_url,
        "parser_version": CATALOG_PARSER_VERSION,
        "html_bytes": len(source_html.encode("utf-8")),
        "html_sha256": hashlib.sha256(source_html.encode("utf-8")).hexdigest(),
        "discovered_records": len(discovered),
        "records_without_url": sum(not record.url for record in discovered),
        "years": dict(sorted(years.items())),
        "catalog_ids": sorted(record.catalog_id for record in discovered),
    }


def write_source_snapshot(snapshot: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def build_catalog_report(
    records: list[CatalogRecord],
    discovered: list[CatalogRecord],
    manifest_count: int,
    source_page_url: str,
    *,
    source_checked: bool = True,
) -> dict:
    current = [record for record in records if record.listing_status.startswith("listed")]
    unlisted = [record for record in records if record.listing_status == "unlisted"]
    unverified = [record for record in records if record.listing_status == "known"]
    missing_url = [record for record in current if not record.url]
    year_counts: dict[str, int] = {}
    section_counts: dict[str, int] = {}
    for record in records:
        year_counts[str(record.year)] = year_counts.get(str(record.year), 0) + 1
        section_counts[record.section] = section_counts.get(record.section, 0) + 1

    possible_gaps: list[dict[str, object]] = []
    groups: dict[tuple[int, str], set[int]] = {}
    for record in current:
        if record.section.startswith("CONJUNTA"):
            continue
        groups.setdefault((record.year, record.section), set()).add(
            int(record.acta_number)
        )
    for (year, section), numbers in sorted(groups.items()):
        if not numbers:
            continue
        missing = sorted(set(range(1, max(numbers) + 1)) - numbers)
        if missing:
            possible_gaps.append(
                {"year": year, "section": section, "acta_numbers": missing}
            )

    url_to_ids: dict[str, list[str]] = {}
    for record in records:
        if record.url:
            url_to_ids.setdefault(record.url, []).append(record.catalog_id)
    shared_urls = [
        {"url": url, "catalog_ids": ids}
        for url, ids in url_to_ids.items()
        if len(ids) > 1
    ]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_page_url": source_page_url,
        "source_checked": source_checked,
        "discovered_records": len(discovered),
        "catalog_records": len(records),
        "currently_listed": len(current),
        "unlisted_records": len(unlisted),
        "unverified_records": len(unverified),
        "records_without_url": len(missing_url),
        "manifest_documents": manifest_count,
        "duplicate_page_occurrences": sum(
            max(record.page_occurrences - 1, 0) for record in discovered
        ),
        "shared_url_conflicts": shared_urls,
        "possible_number_gaps": possible_gaps,
        "years": dict(sorted(year_counts.items())),
        "sections": dict(sorted(section_counts.items())),
        "status": "warning" if missing_url or shared_urls else "ok",
    }


def write_catalog_report(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_catalog_report(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None
