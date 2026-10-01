"""Read-only XLSX hypothesis import. No database, network, or workbook writes."""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
import unicodedata
import zipfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def key(value):
    # Matching normalization only; original text is always preserved.
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    return " ".join(re.findall(r"\w+", "".join(c for c in value if not unicodedata.combining(c))))


ALIASES = {
    "ordinal": {"", "numero", "nro", "orden"},
    "artist": {"artista", "artist", "interprete"},
    "title": {"track", "titulo", "cancion", "title"},
    "album": {"album", "disco"},
    "version": {"version", "recording version"},
    "lyrics": {"letra", "letras", "lyrics", "lyric", "texto"},
    "job_id": {"job id", "job_id", "genly id"},
    "source": {"fuente", "source"},
    "url": {"url", "link", "enlace"},
    "retrieved_at": {"fecha de recuperacion", "retrieved at", "retrieved_at"},
}
MISSING = {"no encontrada", "no encontrado", "no disponible", "not found", "n a", "sin letra"}


def availability(value):
    if value is None or not str(value).strip():
        return "empty"
    if key(value) in MISSING:
        return "not_found"
    if not isinstance(value, str) or value.startswith("#REF!"):
        return "invalid"
    # A source label plus URL is a pointer, not a lyric. Never fetch it here.
    if re.fullmatch(r"\s*(?:\[[^\]\n]+\]\s*)?https?://\S+\s*", value):
        return "pointer_only"
    return "present"


def import_workbook(path: str | Path, month="Agosto"):
    path = Path(path)
    workbook_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    rows, schemas, excluded = [], [], []
    with zipfile.ZipFile(path) as z:
        if sum(i.file_size for i in z.infolist()) > 100_000_000:
            raise ValueError("Workbook exceeds uncompressed import budget")
        def xml(name):
            data = z.read(name)
            if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
                raise ValueError("XML entities are not supported")
            return ET.fromstring(data)
        strings = []
        if "xl/sharedStrings.xml" in z.namelist():
            strings = ["".join(t.text or "" for t in si.findall(".//s:t", NS))
                       for si in xml("xl/sharedStrings.xml").findall("s:si", NS)]
        relationships = {r.attrib["Id"]: r.attrib["Target"]
                         for r in xml("xl/_rels/workbook.xml.rels")}
        for sheet in xml("xl/workbook.xml").findall("s:sheets/s:sheet", NS):
            name = sheet.attrib["name"]
            if not ("lyrics" in key(name).split() and key(month) in key(name).split()) or "art tracks" in key(name):
                excluded.append(name)
                continue
            target = relationships[sheet.attrib[f"{{{REL}}}id"]]
            member = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
            root = xml(member)
            cells_by_row = []
            for row in root.findall("s:sheetData/s:row", NS):
                cells = {}
                for cell in row.findall("s:c", NS):
                    coordinate = cell.attrib["r"]
                    column = re.sub(r"\d", "", coordinate)
                    raw = cell.find("s:v", NS)
                    value = raw.text if raw is not None else None
                    if cell.attrib.get("t") == "s" and value is not None:
                        value = strings[int(value)]
                    elif cell.attrib.get("t") == "inlineStr":
                        value = "".join(t.text or "" for t in cell.findall(".//s:t", NS))
                    if cell.find("s:f", NS) is not None:
                        # Never silently treat cached formula output as sourced lyrics.
                        value = None
                    if value is not None:
                        cells[column] = value
                cells_by_row.append((int(row.attrib["r"]), cells))
            header = None
            for number, cells in cells_by_row:
                mapping = {field: col for field, aliases in ALIASES.items()
                           for col, value in cells.items() if (key(value) in aliases and
                               (field != "ordinal" or str(value).strip() == "#" or key(value) != ""))}
                if "artist" in mapping and "title" in mapping:
                    header = (number, cells, mapping)
                    break
            if header is None:
                raise ValueError(f"No artist/title headers in {name}")
            number, labels, mapping = header
            data_rows = [(n, c) for n, c in cells_by_row if n > number and
                         c.get(mapping["artist"]) and c.get(mapping["title"])]
            inferred = False
            if "lyrics" not in mapping:
                scores = Counter()
                for _, cells in data_rows:
                    for col, value in cells.items():
                        if col not in labels and ("\n" in value or availability(value) == "not_found"):
                            scores[col] += 1
                if len(scores) != 1:
                    raise ValueError(f"Ambiguous missing lyrics header in {name}: {sorted(scores)}")
                mapping["lyrics"] = next(iter(scores))
                inferred = True
            schemas.append({"sheet": name, "header_row": number, "columns": mapping,
                            "lyrics_header_inferred": inferred,
                            "inference": "unique unlabelled multiline/missing-marker column" if inferred else None})
            # External worksheet hyperlinks are pointers only. Never fetch their text.
            rel_member = posixpath.dirname(member) + "/_rels/" + posixpath.basename(member) + ".rels"
            links = {}
            if rel_member in z.namelist():
                rels = {r.attrib["Id"]: r.attrib for r in xml(rel_member)}
                for link in root.findall("s:hyperlinks/s:hyperlink", NS):
                    relation = rels.get(link.attrib.get(f"{{{REL}}}id"), {})
                    if relation.get("Type", "").endswith("/hyperlink"):
                        links[link.attrib["ref"]] = relation.get("Target")
            for row_number, cells in data_rows:
                fields = {field: cells.get(col) for field, col in mapping.items()}
                original = fields.pop("lyrics", None)
                state = availability(original)
                pointer = fields.get("url") or links.get(f"{mapping['lyrics']}{row_number}")
                if state == 'pointer_only' and not pointer:
                    pointer = re.search(r'https?://\S+', original).group(0)
                row = {**fields, "workbook": path.name, "workbook_sha256": workbook_hash,
                       "sheet": name, "row": row_number, "lyrics_cell": f"{mapping['lyrics']}{row_number}",
                       "availability": state, "original_cell_text": original,
                       "lyrics": original if state == "present" else None, "url": pointer,
                       "provenance": "external_identified" if pointer or fields.get("source") else "unverified",
                       "recording_correspondence": "unknown"}
                row["content_sha256"] = digest(row)
                rows.append(row)
    if not schemas:
        raise ValueError(f"No lyrics sheet for {month}")
    ordinals = []
    for row in rows:
        try:
            ordinal = float(row.get("ordinal") or "nan")
            if ordinal.is_integer() and ordinal > 0:
                ordinals.append(int(ordinal))
        except (ValueError, OverflowError):
            pass
    return {"schema": "shadow-reference-import-v1", "workbook_sha256": workbook_hash,
            "sheets": schemas, "excluded_sheets": excluded, "rows": rows,
            "ordinal_gaps_through_300": sorted(set(range(1, 301)) - set(ordinals)) if ordinals else None,
            "duplicate_ordinals": [n for n, count in Counter(ordinals).items() if count > 1],
            "availability_counts": dict(Counter(r["availability"] for r in rows))}


def associate(manifest, jobs):
    """Only exact, unique artist+title candidates. Unknown album/version stays unknown."""
    for row in manifest["rows"]:
        candidates = []
        for job in jobs:
            if row.get("job_id"):
                matches = row["job_id"] == job["job_id"]
            else:
                matches = (key(row["artist"]) == key(job.get("artist")) and
                           key(row["title"]) == key(job.get("title")))
                for field in ("album", "version"):
                    if row.get(field) and job.get(field):
                        matches = matches and key(row[field]) == key(job[field])
            if matches:
                candidates.append(job["job_id"])
        row["candidate_job_ids"] = candidates
        row["association"] = "unique_metadata_candidate" if len(candidates) == 1 else "ambiguous" if candidates else "unmatched"
        row["matched_job_id"] = candidates[0] if len(candidates) == 1 else None
    # Duplicate spreadsheet candidates are not silently collapsed.
    counts = Counter(r["matched_job_id"] for r in manifest["rows"] if r["matched_job_id"])
    for row in manifest["rows"]:
        if counts.get(row["matched_job_id"], 0) > 1:
            row["association"] = "ambiguous_duplicate_rows"
            row["matched_job_id"] = None
    manifest["association_counts"] = dict(Counter(r["association"] for r in manifest["rows"]))
    return manifest
