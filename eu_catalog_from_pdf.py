# -*- coding: utf-8 -*-
"""
Build eu_sections.json from 欧标型钢规格 PDF (ArcelorMittal-style tables).

Reads Dimensions pages (h, b, tw, tf) via pdfplumber/pymupdf — no OCR needed.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

FOLDER = Path(__file__).resolve().parent
EU_SECTIONS_DEFAULT = FOLDER / "eu_sections.json"

# Viewer pages known to hold Dimensions (1-based). Extra pages scanned as fallback.
_DEFAULT_DIM_PAGES = (18, 20, 22, 24, 26, 28, 46, 48)

# name G h b tw tf  — European decimal comma
_DIM_ROW = re.compile(
    r"(HE|IPE|IPN|UPN|UPE)\s+(\d+)\s*([ABCM]{0,2}s?|\*)?\s+"
    r"([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,.]+)\s+([\d,.]+)",
    re.I,
)


def _find_default_pdf():
    for p in sorted(FOLDER.glob("*.pdf")):
        name = p.name
        if name.startswith("361") or "annotated" in str(p).lower():
            continue
        # Prefer Chinese filename containing 欧标 / 型钢
        if "型钢" in name or "欧标" in name or "HE" in name.upper():
            return p
    # Fallback: largest non-361 pdf in folder
    cands = [p for p in FOLDER.glob("*.pdf")
             if not p.name.startswith("361") and "annotated" not in str(p).lower()]
    if not cands:
        return None
    return max(cands, key=lambda x: x.stat().st_size)


def _to_float(s: str) -> float:
    return float(s.replace(",", "."))


def _catalog_key(family: str, num: str, series: str | None) -> str:
    family = family.upper()
    series = (series or "").rstrip("sS*").upper()
    if family == "HE":
        # HE 300 A -> HE300A ; HE 300 AA -> HE300AA
        return f"HE{num}{series or 'A'}"
    # IPE 300 / UPN 100 / UPE 120 / IPN 100
    return f"{family}{num}{series}" if series else f"{family}{num}"


def _bom_aliases(catalog_name: str) -> dict:
    """HEA300 ↔ HE300A style aliases."""
    aliases = {catalog_name: catalog_name}
    m = re.match(r"^HE(\d+)([ABM])$", catalog_name)
    if m:
        num, ser = m.group(1), m.group(2)
        aliases[f"HE{ser}{num}"] = catalog_name  # HEA300
        aliases[f"HE{num}{ser}"] = catalog_name
    m = re.match(r"^HE(\d+)AA$", catalog_name)
    if m:
        aliases[f"HEAA{m.group(1)}"] = catalog_name
    # IPE300 / UPN100 already match BOM style
    return aliases


def extract_sections_from_pdf(pdf_path, pages=None) -> dict:
    """
    Return {catalog_name: {depth, flange_w, web_t, flange_t, weight_kg_m}}.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)

    try:
        import fitz
    except ImportError as e:
        raise ImportError("pymupdf required: pip install pymupdf") from e

    doc = fitz.open(str(pdf_path))
    page_list = list(pages) if pages else list(_DEFAULT_DIM_PAGES)
    # Also auto-detect other Dimensions pages
    for i in range(doc.page_count):
        if (i + 1) in page_list:
            continue
        t = doc.load_page(i).get_text("text") or ""
        if "Abmessungen" in t and "tw" in t and re.search(r"(HE|IPE|UPN|UPE|IPN)\s+\d+", t):
            page_list.append(i + 1)

    by_catalog = {}
    for page_no in sorted(set(page_list)):
        if page_no < 1 or page_no > doc.page_count:
            continue
        text = doc.load_page(page_no - 1).get_text("text") or ""
        flat = text.replace("\n", " ")
        for m in _DIM_ROW.finditer(flat):
            family, num, series = m.group(1), m.group(2), m.group(3)
            # Skip footnote-only / malformed (h must be plausible)
            try:
                g = _to_float(m.group(4))
                h = _to_float(m.group(5))
                b = _to_float(m.group(6))
                tw = _to_float(m.group(7))
                tf = _to_float(m.group(8))
            except ValueError:
                continue
            if h < 40 or h > 1200 or b < 20 or b > 1200:
                continue
            if tw < 1 or tw > 80 or tf < 1 or tf > 120:
                continue
            key = _catalog_key(family, num, series)
            # Prefer first hit; dimension pages are authoritative
            if key in by_catalog:
                continue
            by_catalog[key] = {
                "depth": h,
                "flange_w": b,
                "web_t": tw,
                "flange_t": tf,
                "weight_kg_m": g,
            }

    doc.close()
    return by_catalog


def build_catalog_document(by_catalog: dict, pdf_name: str) -> dict:
    aliases = {}
    for name in by_catalog:
        aliases.update(_bom_aliases(name))
    return {
        "_meta": {
            "note": "Generated from European steel PDF Dimensions tables (h/b/tw/tf)",
            "source_pdf": pdf_name,
            "section_count": len(by_catalog),
        },
        "by_catalog": {
            k: {
                "depth": v["depth"],
                "flange_w": v["flange_w"],
                "web_t": v["web_t"],
                "flange_t": v["flange_t"],
            }
            for k, v in sorted(by_catalog.items(), key=lambda kv: kv[0])
        },
        "aliases": dict(sorted(aliases.items())),
    }


def refresh_eu_catalog(pdf_path=None, out_path=None) -> dict:
    """
    Parse PDF and write eu_sections.json. Returns the document written.
    Clears in-memory catalog cache used by weld_extractor_eu.
    """
    pdf_path = Path(pdf_path) if pdf_path else _find_default_pdf()
    if pdf_path is None:
        raise FileNotFoundError("No European steel PDF found in project folder")
    out_path = Path(out_path) if out_path else EU_SECTIONS_DEFAULT

    by_catalog = extract_sections_from_pdf(pdf_path)
    if not by_catalog:
        raise RuntimeError(f"No section dimensions extracted from {pdf_path}")

    doc = build_catalog_document(by_catalog, pdf_path.name)
    out_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

    # Clear caches
    try:
        import weld_extractor_eu as weu
        weu._EU_CATALOG_CACHE.clear()
    except Exception:
        pass

    return doc


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Refresh eu_sections.json from PDF")
    ap.add_argument("--pdf", default=None, help="Path to 欧标型钢规格 PDF")
    ap.add_argument("--out", default=str(EU_SECTIONS_DEFAULT))
    args = ap.parse_args()
    doc = refresh_eu_catalog(args.pdf, args.out)
    print(f"Wrote {args.out}: {doc['_meta']['section_count']} sections "
          f"from {doc['_meta']['source_pdf']}")
    for key in ("HE200A", "HE300A", "HE300B", "IPE300", "UPN100", "UPN120"):
        dims = doc["by_catalog"].get(key)
        print(f"  {key}: {dims}")
