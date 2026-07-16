"""
European-standard DXF annotation entry.

Reuses placement/layout helpers from dxf_annotator but must NOT share the
GB annotate() entry — call annotate_eu only for AB/AC/AP/AT/AX assemblies.
Output goes to annotated/eu/; GB annotations go to annotated/gb/.
"""
from __future__ import annotations

import os
import re
import time
from collections import defaultdict

import ezdxf

import dxf_annotator as _gb
from weld_extractor import is_eu_comp, extract_comp_id

FOLDER = os.path.dirname(os.path.abspath(__file__))
ANNOTATED_EU_DIR = os.path.join(FOLDER, "annotated", "eu")


def annotate_eu(results, dxf_paths=None, out_dir=None):
    """
    EU-only annotation entry.
    Rejects any non-EU component in results. Writes to annotated/eu/.
    """
    out_dir = out_dir or ANNOTATED_EU_DIR
    os.makedirs(out_dir, exist_ok=True)

    bad = sorted({r.get('component', '') for r in results if not is_eu_comp(r.get('component', ''))})
    if bad:
        raise ValueError(f"annotate_eu refused non-EU components: {bad}")

    by_comp = defaultdict(list)
    for r in results:
        by_comp[r['component']].append(r)

    if dxf_paths is None:
        import glob
        dxf_paths = sorted([
            f for f in glob.glob(os.path.join(FOLDER, "*.dxf"))
            if '(2)' not in f and re.search(r'(AB|AC|AP|AT|AX)\d{4}', os.path.basename(f), re.I)
        ])

    all_sampled_labels = []
    _t_all0 = time.perf_counter()

    for dxf_path in dxf_paths:
        comp = extract_comp_id(dxf_path)
        if not is_eu_comp(comp):
            print(f"  SKIP (not EU): {os.path.basename(dxf_path)}")
            continue
        comp_full = os.path.splitext(os.path.basename(dxf_path))[0].rsplit('_', 1)[0]

        if comp not in by_comp:
            print(f"  SKIP {comp_full}: no weld data")
            continue

        # Prefer welds extracted from this exact DXF (avoid AB0002_00+_01 merge).
        _base = os.path.basename(dxf_path)
        _all = by_comp[comp]
        if any(r.get('source_dxf') for r in _all):
            comp_welds = [r for r in _all if r.get('source_dxf') == _base]
        else:
            comp_welds = _all
        if not comp_welds:
            print(f"  SKIP {comp_full}: no weld data for {_base}")
            continue
        print(f"\n  [EU] Annotating {comp_full} ({len(comp_welds)} welds) → {_base}")

        try:
            doc = ezdxf.readfile(dxf_path)
        except Exception as e:
            print(f"    ERROR reading {dxf_path}: {e}")
            continue

        try:
            if hasattr(_gb._search_placement, '_fb_seen'):
                _gb._search_placement._fb_seen.clear()
            _t0 = time.perf_counter()
            sampled_labels = _gb._annotate_one(doc, comp_welds)
            print(f"    annotate wall: {time.perf_counter() - _t0:.1f}s")
            all_sampled_labels.extend(sampled_labels)
            out_path = os.path.join(out_dir, os.path.basename(dxf_path))
            for _retry in range(3):
                try:
                    doc.saveas(out_path)
                    break
                except OSError:
                    if _retry < 2:
                        time.sleep(0.5)
                    else:
                        raise

            try:
                hv = doc.header.hdrvars
                cx, cy, _ = hv['$VIEWCTR'].value if '$VIEWCTR' in hv else (0, 0, 0)
                view_size = hv['$VIEWSIZE'].value if '$VIEWSIZE' in hv else 1
                _gb._patch_header_viewctr(out_path, cx, cy, view_size)
            except Exception:
                pass

            print(f"    Saved → {out_path}")
        except Exception as e:
            import traceback
            print(f"    ERROR annotating {comp}: {e}")
            traceback.print_exc()

    print(f"  [EU] All annotate wall: {time.perf_counter() - _t_all0:.1f}s")
    return all_sampled_labels


if __name__ == '__main__':
    print("dxf_annotator_eu.py — use annotate_eu(results, dxf_paths) from run_pipeline")
