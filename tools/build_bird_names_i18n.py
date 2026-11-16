"""Build per-language bird-name translation maps from the Multiling IOC XLSX.

This is a *one-time* data-preparation script. Its outputs are committed into
the repository under ``analyzer/models/birds/`` and bundled with the app. The
runtime only reads the emitted JSON; it never parses the workbook.

Input (downloaded out-of-band, path supplied via ``--xlsx``):

  * Multiling IOC — IOC World Bird List multilingual species names (XLSX)
        Frank Gill, David Donsker & Pamela Rasmussen (Eds), licensed CC-BY 3.0
        https://www.worldbirdnames.org/new/ioc-lists/master-list-2/
        (the workbook's ``List`` sheet: one row per species, with the
        scientific name, the IOC English name, and one column per language)

Inputs read from the repository (committed):

  * analyzer/models/birds/birds_global.csv  -- master catalog (canonical
        common names the classifier writes and the UI displays, plus the
        AOS alias names kept for legacy rows)

Outputs (committed):

  * analyzer/models/birds/bird_names.<lang>.json
        { "canonical_common_name": "translated name", ... } -- includes the
        catalog's AOS alias names as extra keys so legacy rows translate too.
        English is NOT emitted: it is the identity language (absent map ==
        fallback to the stored English name).
  * analyzer/models/birds/bird_names_index.json
        Manifest for the Settings language dropdown: shipped languages with
        native names and coverage. Coverage = share of catalog species that
        have a translation in that language.

Join key: workbook scientific name (``IOC_15.2`` column) -> catalog
``scientific_name``; exact IOC English name match is the fallback. Languages
below ``--min-coverage`` are skipped (listed in the log) unless named in
``--include`` -- Russian and English are guaranteed by policy, not by luck.

Run:
  python tools/build_bird_names_i18n.py \\
      --xlsx "/tmp/Multiling IOC 15.2.xlsx"
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = REPO_ROOT / 'analyzer' / 'models'
OUT_DIR = MODELS_DIR / 'birds'
CATALOG_PATH = OUT_DIR / 'birds_global.csv'

# Workbook "List"-sheet column -> (language code, native name).
#
# Codes are lowercase BCP-47-ish slugs; the settings sanitizer accepts
# exactly ^[a-z]{2}(-[a-z]{2})?$, so every code added here must stay in that
# shape. "French (Gaudin)" is an alternate French nomenclature, not a
# separate language: it is deliberately absent and logged as skipped.
LANGUAGE_COLUMNS: dict[str, tuple[str, str]] = {
    'Afrikaans':                 ('af', 'Afrikaans'),
    'Arabic':                    ('ar', 'العربية'),
    'Belarusian':                ('be', 'Беларуская'),
    'Bulgarian':                 ('bg', 'Български'),
    'Catalan':                   ('ca', 'Català'),
    'Chinese':                   ('zh', '中文'),
    'Chinese (Traditional)':     ('zh-tw', '繁體中文'),
    'Croatian':                  ('hr', 'Hrvatski'),
    'Czech':                     ('cs', 'Čeština'),
    'Danish':                    ('da', 'Dansk'),
    'Dutch':                     ('nl', 'Nederlands'),
    'Esperanto':                 ('eo', 'Esperanto'),
    'Estonian':                  ('et', 'Eesti'),
    'Finnish':                   ('fi', 'Suomi'),
    'French':                    ('fr', 'Français'),
    'German':                    ('de', 'Deutsch'),
    'Greek':                     ('el', 'Ελληνικά'),
    'Hebrew':                    ('he', 'עברית'),
    'Hungarian':                 ('hu', 'Magyar'),
    'Icelandic':                 ('is', 'Íslenska'),
    'Indonesian':                ('id', 'Bahasa Indonesia'),
    'Italian':                   ('it', 'Italiano'),
    'Japanese':                  ('ja', '日本語'),
    'Korean':                    ('ko', '한국어'),
    'Latvian':                   ('lv', 'Latviešu'),
    'Lithuanian':                ('lt', 'Lietuvių'),
    'Macedonian':                ('mk', 'Македонски'),
    'Malayalam':                 ('ml', 'മലയാളം'),
    'Northern Sami':             ('se', 'Davvisámegiella'),
    'Norwegian':                 ('no', 'Norsk'),
    'Persian':                   ('fa', 'فارسی'),
    'Polish':                    ('pl', 'Polski'),
    'Portuguese (Lusophone)':    ('pt', 'Português'),
    'Portuguese (Portuguese)':   ('pt-pt', 'Português europeu'),
    'Romanian':                  ('ro', 'Română'),
    'Russian':                   ('ru', 'Русский'),
    'Serbian':                   ('sr', 'Српски'),
    'Slovak':                    ('sk', 'Slovenčina'),
    'Slovenian':                 ('sl', 'Slovenščina'),
    'Spanish':                   ('es', 'Español'),
    'Swedish':                   ('sv', 'Svenska'),
    'Thai':                      ('th', 'ไทย'),
    'Turkish':                   ('tr', 'Türkçe'),
    'Ukrainian':                 ('uk', 'Українська'),
}

# Columns present in the workbook but intentionally not shipped.
SKIPPED_COLUMNS = {'French (Gaudin)': 'alternate French nomenclature, not a language'}

_WS_RE = re.compile(r'\s+')


def _norm(s) -> str:
    """Collapse all whitespace runs (incl. non-breaking spaces) to single spaces."""
    if s is None:
        return ''
    return _WS_RE.sub(' ', str(s)).strip()


def load_catalog_index(catalog_path: Path):
    """Index the bundled catalog by scientific name and by every English name.

    Returns ``(by_sci, by_english, total)`` where ``by_english`` covers both
    canonical common names and AOS alias names (lowercased keys).
    """
    by_sci: dict[str, tuple[str, tuple[str, ...]]] = {}
    by_english: dict[str, tuple[str, tuple[str, ...]]] = {}
    total = 0
    with open(catalog_path, encoding='utf-8', newline='') as f:
        for row in csv.DictReader(f):
            canonical = _norm(row.get('canonical_common_name'))
            if not canonical:
                continue
            total += 1
            aliases = tuple(a for a in (_norm(x) for x in (row.get('aliases') or '').split('|')) if a)
            entry = (canonical, aliases)
            sci = _norm(row.get('scientific_name'))
            if sci:
                by_sci.setdefault(sci, entry)
            by_english.setdefault(canonical.lower(), entry)
            for a in aliases:
                by_english.setdefault(a.lower(), entry)
    return by_sci, by_english, total


def parse_multiling(xlsx_path: Path):
    """Parse the workbook's ``List`` sheet.

    Returns ``(languages, rows)``: the column->language mapping actually
    present, and per-species ``{sci, english, names: {code: name}}`` dicts.
    """
    try:
        from openpyxl import load_workbook
    except ImportError:
        sys.exit("openpyxl required to parse the Multiling IOC XLSX: pip install openpyxl")

    wb = load_workbook(str(xlsx_path), read_only=True, data_only=True)
    ws = wb['List']
    rows_iter = ws.iter_rows(values_only=True)
    header = [_norm(h) for h in next(rows_iter)]

    col_sci = header.index('IOC_15.2')
    col_english = header.index('English')
    languages: dict[int, tuple[str, str]] = {}
    for i, h in enumerate(header):
        if h in LANGUAGE_COLUMNS:
            languages[i] = LANGUAGE_COLUMNS[h]
        elif h in SKIPPED_COLUMNS:
            print(f"Skipping column '{h}': {SKIPPED_COLUMNS[h]}")

    unknown = [h for h in header
               if h not in ('seq', 'Order', 'Family', 'IOC_15.2', 'English')
               and h not in LANGUAGE_COLUMNS and h not in SKIPPED_COLUMNS]
    if unknown:
        print(f"WARNING: unmapped workbook columns ignored: {', '.join(unknown)}")

    rows: list[dict] = []
    for r in rows_iter:
        sci = _norm(r[col_sci] if col_sci < len(r) else None)
        english = _norm(r[col_english] if col_english < len(r) else None)
        if not sci and not english:
            continue
        names = {}
        for i, (code, _native) in languages.items():
            name = _norm(r[i] if i < len(r) else None)
            if name:
                names[code] = name
        rows.append({'sci': sci, 'english': english, 'names': names})
    return languages, rows


def build(xlsx_path: Path, out_dir: Path, catalog_path: Path,
          min_coverage: float, include: set[str]) -> dict:
    print(f"Reading catalog from {catalog_path}")
    by_sci, by_english, total = load_catalog_index(catalog_path)
    print(f"  -> {total} species")

    print(f"Reading Multiling IOC from {xlsx_path}")
    languages, rows = parse_multiling(xlsx_path)
    print(f"  -> {len(rows)} workbook rows, {len(languages)} language columns")

    # translations[code][english_name] = translated name. Keys are every
    # English form the app can display a species under: canonical common name
    # plus catalog aliases (legacy AOS names on old rows). Coverage is tracked
    # per species (not per key) so aliases don't inflate it past 100%.
    translations: dict[str, dict[str, str]] = {code: {} for code, _ in languages.values()}
    covered: dict[str, set[str]] = {code: set() for code, _ in languages.values()}
    unmatched = 0
    for row in rows:
        entry = by_sci.get(row['sci']) if row['sci'] else None
        if entry is None and row['english']:
            entry = by_english.get(row['english'].lower())
        if entry is None:
            unmatched += 1
            continue
        canonical, aliases = entry
        for code, name in row['names'].items():
            t = translations[code]
            t.setdefault(canonical, name)
            covered[code].add(canonical)
            for a in aliases:
                t.setdefault(a, name)
    if unmatched:
        print(f"  workbook rows with no catalog match: {unmatched} "
              f"(IOC version drift; English fallback covers most)")

    out_dir.mkdir(parents=True, exist_ok=True)
    emitted: dict[str, dict] = {}
    skipped: list[str] = []
    for i, (code, native) in languages.items():
        t = translations[code]
        coverage = len(covered[code]) / total if total else 0.0
        if coverage < min_coverage and code not in include:
            skipped.append(f"{code} ({native}): coverage {coverage:.1%} < {min_coverage:.0%}")
            continue
        out = out_dir / f'bird_names.{code}.json'
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(dict(sorted(t.items(), key=lambda kv: kv[0].lower())), f,
                      ensure_ascii=False, indent=0)
            f.write('\n')
        emitted[code] = {'code': code, 'name_native': native,
                         'species_count': len(covered[code]), 'coverage': round(coverage, 4)}
        print(f"Wrote {len(t):6d} names ({coverage:6.1%} of species) -> {out.name}")

    for s in skipped:
        print(f"Skipped {s}")

    # Remove stale language files from an earlier run that are no longer
    # emitted (language dropped or coverage threshold raised).
    keep = {f'bird_names.{c}.json' for c in emitted}
    for stale in sorted(out_dir.glob('bird_names.*.json')):
        if stale.name not in keep:
            stale.unlink()
            print(f"Removed stale {stale.name}")

    index = {
        'generated_from': 'Multiling IOC 15.2',
        'catalog_species': total,
        'languages': [emitted[c] for c in sorted(emitted)],
    }
    index_path = out_dir / 'bird_names_index.json'
    with open(index_path, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
        f.write('\n')
    print(f"Wrote manifest ({len(emitted)} languages) -> {index_path.name}")

    _update_notices(out_dir / 'NOTICES.md')
    return index


_NOTICES_BEGIN = '<!-- bird-names-i18n:begin -->'
_NOTICES_END = '<!-- bird-names-i18n:end -->'


def _update_notices(path: Path) -> None:
    """Insert/replace this script's attribution section in NOTICES.md.

    The section is delimited by marker comments so the script only owns its
    own block; the catalog builder rewrites the rest of the file wholesale.
    """
    section = (
        f"{_NOTICES_BEGIN}\n"
        "## IOC Multiling — bird-name translations\n\n"
        "Frank Gill, David Donsker & Pamela Rasmussen (Eds). 2026. "
        "*IOC World Bird List* — Multiling IOC 15.2. "
        "https://www.worldbirdnames.org/new/ioc-lists/master-list-2/\n\n"
        "Licensed under "
        "[Creative Commons Attribution 3.0 Unported](https://creativecommons.org/licenses/by/3.0/).\n"
        "Translated common names in ``bird_names.<lang>.json`` are derived from this\n"
        "source (joined to the catalog on scientific name). Generated by\n"
        "``tools/build_bird_names_i18n.py``.\n"
        f"{_NOTICES_END}\n"
    )
    existing = path.read_text(encoding='utf-8') if path.exists() else ''
    pattern = re.compile(
        re.escape(_NOTICES_BEGIN) + r'.*?' + re.escape(_NOTICES_END) + r'\n?',
        re.S)
    if pattern.search(existing):
        updated = pattern.sub(section, existing)
    else:
        updated = existing.rstrip('\n') + '\n\n' + section
    path.write_text(updated, encoding='utf-8')
    print(f"Wrote attribution -> {path.name}")


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--xlsx', type=Path, required=True,
                   help='Path to the Multiling IOC workbook (downloaded out-of-band).')
    p.add_argument('--min-coverage', type=float, default=0.5,
                   help='Minimum share of catalog species a language must cover '
                        'to be emitted (default 0.5).')
    p.add_argument('--include', default='',
                   help='Comma-separated language codes to emit even when below '
                        'the coverage threshold (e.g. "ru,de").')
    p.add_argument('--out-dir', type=Path, default=OUT_DIR,
                   help=argparse.SUPPRESS)
    p.add_argument('--catalog', type=Path, default=CATALOG_PATH,
                   help=argparse.SUPPRESS)
    args = p.parse_args()
    include = {c.strip().lower() for c in args.include.split(',') if c.strip()}
    build(args.xlsx, args.out_dir, args.catalog, args.min_coverage, include)
