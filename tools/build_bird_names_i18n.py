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

  * analyzer/models/birds/bird_names.<tag>.json
        { "Scientific name": "translated name", ... } keyed by the catalog's
        scientific name. ``<tag>`` is a canonical BCP-47 tag (``de``,
        ``pt-PT``, ``zh-Hans``). The runtime joins these back onto the
        catalog's English names and aliases, so legacy AOS-labelled rows
        translate too. English is NOT emitted: it is the identity language
        (absent map == fallback to the stored English name).
  * analyzer/models/birds/bird_names_index.json
        Manifest for the Settings language dropdown: shipped languages with
        native names and coverage. Coverage = share of catalog species that
        have a translation in that language.

Join key: workbook scientific name (``IOC_<version>`` column) -> catalog
``scientific_name``; exact IOC English name match is the fallback. The
workbook's IOC release must equal the catalog's (``CATALOG_IOC_VERSION`` in
``analyzer/kestrel_analyzer/bird_catalog.py``) so both use one taxonomy; a
mismatch aborts unless ``--allow-version-mismatch`` is given. Languages
below ``--min-coverage`` are skipped (listed in the log) unless named in
``--include``.

Run (the workbook release must match the catalog, currently 15.1):
  python tools/build_bird_names_i18n.py \\
      --xlsx "/path/to/Multiling IOC 15.1_d.xlsx"
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

sys.path.insert(0, str(REPO_ROOT / 'analyzer'))
from kestrel_analyzer.bird_catalog import CATALOG_IOC_VERSION  # noqa: E402
from kestrel_analyzer.language_codes import is_valid_language_tag  # noqa: E402

_IOC_COLUMN_RE = re.compile(r'^IOC_(\d+\.\d+)$')

# Workbook "List"-sheet column -> (language code, native name).
#
# Codes are canonical BCP-47 tags (``language[-Script][-REGION]``, checked by
# kestrel_analyzer.language_codes). Chinese is split by script, not region
# (zh-Hans / zh-Hant); "Portuguese (Lusophone)" is the general ``pt`` list
# and "Portuguese (Portuguese)" the European ``pt-PT`` one, so ``pt-BR``
# falls back to ``pt``. The Serbian column is written in Latin script.
# "French (Gaudin)" is an alternate French nomenclature, not a separate
# language: it is deliberately absent and logged as skipped.
LANGUAGE_COLUMNS: dict[str, tuple[str, str]] = {
    'Afrikaans':                 ('af', 'Afrikaans'),
    'Arabic':                    ('ar', 'العربية'),
    'Belarusian':                ('be', 'Беларуская'),
    'Bulgarian':                 ('bg', 'Български'),
    'Catalan':                   ('ca', 'Català'),
    'Chinese':                   ('zh-Hans', '简体中文'),
    'Chinese (Traditional)':     ('zh-Hant', '繁體中文'),
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
    'Portuguese (Portuguese)':   ('pt-PT', 'Português (Portugal)'),
    'Romanian':                  ('ro', 'Română'),
    'Russian':                   ('ru', 'Русский'),
    'Serbian':                   ('sr-Latn', 'Srpski (latinica)'),
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

    Returns ``(by_sci, by_english, total)``. ``by_sci`` maps a scientific
    name to itself (the set of catalog species); ``by_english`` maps every
    canonical common name and AOS alias (lowercased) to the catalog's
    scientific name, for the English-name fallback join.
    """
    by_sci: dict[str, str] = {}
    by_english: dict[str, str] = {}
    total = 0
    with open(catalog_path, encoding='utf-8', newline='') as f:
        for row in csv.DictReader(f):
            canonical = _norm(row.get('canonical_common_name'))
            sci = _norm(row.get('scientific_name'))
            if not canonical or not sci:
                continue
            total += 1
            aliases = tuple(a for a in (_norm(x) for x in (row.get('aliases') or '').split('|')) if a)
            by_sci.setdefault(sci, sci)
            by_english.setdefault(canonical.lower(), sci)
            for a in aliases:
                by_english.setdefault(a.lower(), sci)
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

    ioc_cols = [(i, m.group(1)) for i, h in enumerate(header) if (m := _IOC_COLUMN_RE.match(h))]
    if len(ioc_cols) != 1:
        sys.exit(f"expected exactly one IOC_<version> column, found {[header[i] for i, _ in ioc_cols]}")
    col_sci, ioc_version = ioc_cols[0]
    col_english = header.index('English')
    languages: dict[int, tuple[str, str]] = {}
    for i, h in enumerate(header):
        if h in LANGUAGE_COLUMNS:
            languages[i] = LANGUAGE_COLUMNS[h]
        elif h in SKIPPED_COLUMNS:
            print(f"Skipping column '{h}': {SKIPPED_COLUMNS[h]}")

    unknown = [h for h in header
               if h not in ('seq', 'Order', 'Family', 'English') and not _IOC_COLUMN_RE.match(h)
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
    return languages, rows, ioc_version


def build(xlsx_path: Path, out_dir: Path, catalog_path: Path,
          min_coverage: float, include: set[str],
          expected_ioc_version: str | None = CATALOG_IOC_VERSION) -> dict:
    print(f"Reading catalog from {catalog_path}")
    by_sci, by_english, total = load_catalog_index(catalog_path)
    print(f"  -> {total} species")

    print(f"Reading Multiling IOC from {xlsx_path}")
    languages, rows, ioc_version = parse_multiling(xlsx_path)
    print(f"  -> IOC {ioc_version}: {len(rows)} workbook rows, {len(languages)} language columns")
    if expected_ioc_version and ioc_version != expected_ioc_version:
        sys.exit(f"Multiling workbook is IOC {ioc_version} but the catalog is IOC "
                 f"{expected_ioc_version}. Use the matching workbook (or rebuild the "
                 f"catalog first); --allow-version-mismatch overrides.")
    bad_codes = [c for c, _ in languages.values() if not is_valid_language_tag(c)]
    if bad_codes:
        sys.exit(f"non-canonical language tags in LANGUAGE_COLUMNS: {bad_codes}")

    # translations[code][scientific_name] = translated name, keyed by the
    # catalog's scientific name (the workbook's English name is only a
    # fallback join for a row whose binomial the catalog spells differently).
    translations: dict[str, dict[str, str]] = {code: {} for code, _ in languages.values()}
    unmatched = 0
    for row in rows:
        sci = by_sci.get(row['sci']) if row['sci'] else None
        if sci is None and row['english']:
            sci = by_english.get(row['english'].lower())
        if sci is None:
            unmatched += 1
            continue
        for code, name in row['names'].items():
            translations[code].setdefault(sci, name)
    if unmatched:
        print(f"  workbook rows with no catalog match: {unmatched}")

    out_dir.mkdir(parents=True, exist_ok=True)
    emitted: dict[str, dict] = {}
    skipped: list[str] = []
    for i, (code, native) in languages.items():
        t = translations[code]
        coverage = len(t) / total if total else 0.0
        if coverage < min_coverage and code not in include:
            skipped.append(f"{code} ({native}): coverage {coverage:.1%} < {min_coverage:.0%}")
            continue
        out = out_dir / f'bird_names.{code}.json'
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(dict(sorted(t.items())), f,
                      ensure_ascii=False, indent=0)
            f.write('\n')
        emitted[code] = {'code': code, 'name_native': native,
                         'species_count': len(t), 'coverage': round(coverage, 4)}
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
        'generated_from': f'Multiling IOC {ioc_version}',
        'ioc_version': ioc_version,
        'keyed_by': 'scientific_name',
        'catalog_species': total,
        'languages': [emitted[c] for c in sorted(emitted)],
    }
    index_path = out_dir / 'bird_names_index.json'
    with open(index_path, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
        f.write('\n')
    print(f"Wrote manifest ({len(emitted)} languages) -> {index_path.name}")

    _update_notices(out_dir / 'NOTICES.md', ioc_version)
    return index


_NOTICES_BEGIN = '<!-- bird-names-i18n:begin -->'
_NOTICES_END = '<!-- bird-names-i18n:end -->'


def _update_notices(path: Path, ioc_version: str) -> None:
    """Insert/replace this script's attribution section in NOTICES.md.

    The section is delimited by marker comments so the script only owns its
    own block; the catalog builder rewrites the rest of the file wholesale.
    """
    major_minor = ioc_version
    section = (
        f"{_NOTICES_BEGIN}\n"
        f"## Multiling IOC (v{major_minor}) — bird-name translations\n\n"
        "Frank Gill, David Donsker & Pamela Rasmussen (Eds). "
        f"*IOC World Bird List* — Multiling IOC {major_minor}. "
        "https://www.worldbirdnames.org/new/ioc-lists/master-list-2/\n\n"
        "Licensed under "
        "[Creative Commons Attribution 3.0 Unported](https://creativecommons.org/licenses/by/3.0/).\n"
        "The translated common names in ``bird_names.<tag>.json`` are taken from\n"
        "this source, keyed by scientific name and joined to the catalog (same IOC\n"
        "release as ``birds_global.csv``). Names are reproduced unchanged; Kestrel\n"
        "only selects the languages it ships and drops species outside its catalog.\n"
        "Generated by ``tools/build_bird_names_i18n.py``.\n"
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
    p.add_argument('--allow-version-mismatch', action='store_true',
                   help='Accept a workbook whose IOC release differs from the catalog.')
    p.add_argument('--out-dir', type=Path, default=OUT_DIR,
                   help=argparse.SUPPRESS)
    p.add_argument('--catalog', type=Path, default=CATALOG_PATH,
                   help=argparse.SUPPRESS)
    args = p.parse_args()
    include = {c.strip() for c in args.include.split(',') if c.strip()}
    build(args.xlsx, args.out_dir, args.catalog, args.min_coverage, include,
          expected_ioc_version=None if args.allow_version_mismatch else CATALOG_IOC_VERSION)
