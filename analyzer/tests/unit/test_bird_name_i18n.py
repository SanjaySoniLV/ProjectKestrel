"""Unit tests for bird-name i18n: converter, loaders, bridge methods, settings.

Covers the contracts the display-translation feature depends on:

  * ``tools/build_bird_names_i18n.py`` joins the Multiling workbook to the
    bundled catalog on scientific name (English-name fallback), emits alias
    keys, honors the coverage threshold + ``--include``, and writes a manifest.
  * ``kestrel_analyzer.bird_catalog`` translation loaders validate language
    codes before touching the filesystem and tolerate missing files.
  * The bridge methods reject junk codes without path escapes.
  * The ``language`` setting is sanitized to a strict code shape.
  * Every ``data-i18n*`` key used in ``visualizer.html`` exists in the
    English dictionary ``analyzer/i18n/en.json``.
"""

from pathlib import Path
import json
import re
import sys

import pytest

ANALYZER_DIR = Path(__file__).parent.parent.parent
REPO_ROOT = ANALYZER_DIR.parent
sys.path.insert(0, str(ANALYZER_DIR))
sys.path.insert(0, str(REPO_ROOT / 'tools'))

import settings_utils
from kestrel_analyzer import bird_catalog

pytestmark = pytest.mark.unit

# The converter tests need openpyxl + tools/ importable; the skip is applied
# per-fixture/per-test (not at module level) so the settings and dictionary
# consistency tests below always run.


# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------

def _write_catalog(tmp_path: Path) -> Path:
    catalog = tmp_path / 'birds_global.csv'
    catalog.write_text(
        "canonical_common_name,scientific_name,family_sci,family_common,order,regions,alpha_4,aliases,is_model_species\n"
        "Sand Martin,Riparia riparia,Ripariidae,Swallow sp.,Passeriformes,PAL,,Bank Swallow,1\n"
        "Common Ostrich,Struthio camelus,Struthionidae,Ostrich sp.,Struthioniformes,AF,,,0\n",
        encoding='utf-8')
    return catalog


def _write_workbook(tmp_path: Path) -> Path:
    openpyxl = pytest.importorskip("openpyxl", reason="converter tests need openpyxl")
    wb_path = tmp_path / 'multiling.xlsx'
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'List'
    ws.append(['seq', 'Order', 'Family', 'IOC_15.2', 'English',
               'German', 'Russian', 'French (Gaudin)', 'Malayalam'])
    # Row 1 joins on scientific name; the catalog canonical is the AOS name
    # ("Bank Swallow") with the IOC English ("Sand Martin") as its alias.
    ws.append([1, 'Passeriformes', 'Ripariidae', 'Riparia riparia', 'Sand Martin',
               'Uferschwalbe', 'Береговушка', 'Hirondelle de rivage', 'കരിങ്കോഴി'])
    # Row 2 has a scientific name the catalog doesn't know; the English-name
    # fallback join must still find it.
    ws.append([2, 'Struthioniformes', 'Struthionidae', 'Struthio camelus XXX', 'Common Ostrich',
               'Strauß', 'Африканский страус', 'Autruche d’Afrique', None])
    wb.save(wb_path)
    return wb_path


@pytest.fixture()
def built(tmp_path):
    """Run the converter on tiny fixtures; return (out_dir, index)."""
    bni = pytest.importorskip("build_bird_names_i18n", reason="tools/ must be importable")
    catalog = _write_catalog(tmp_path)
    wb_path = _write_workbook(tmp_path)
    out_dir = tmp_path / 'out'
    index = bni.build(wb_path, out_dir, catalog, min_coverage=0.75, include=set())
    return out_dir, index


# ----------------------------------------------------------------------------
# Converter
# ----------------------------------------------------------------------------

class TestConverter:

    def test_join_scientific_and_alias_keys(self, built):
        out_dir, _ = built
        de = json.loads((out_dir / 'bird_names.de.json').read_text(encoding='utf-8'))
        # Canonical catalog name AND its AOS alias both resolve.
        assert de['Sand Martin'] == 'Uferschwalbe'
        assert de['Bank Swallow'] == 'Uferschwalbe'

    def test_english_fallback_join(self, built):
        out_dir, _ = built
        de = json.loads((out_dir / 'bird_names.de.json').read_text(encoding='utf-8'))
        assert de['Common Ostrich'] == 'Strauß'

    def test_empty_cells_dropped(self, tmp_path):
        bni = pytest.importorskip("build_bird_names_i18n", reason="tools/ must be importable")
        catalog = _write_catalog(tmp_path)
        wb_path = _write_workbook(tmp_path)
        out_dir = tmp_path / 'out'
        bni.build(wb_path, out_dir, catalog, min_coverage=0.75, include={'ml'})
        ml = json.loads((out_dir / 'bird_names.ml.json').read_text(encoding='utf-8'))
        # Row 2 has no Malayalam name: neither the canonical nor its alias
        # appears, while the translated row resolves under both English forms.
        assert 'Common Ostrich' not in ml
        assert ml['Sand Martin'] == 'കരിങ്കോഴി'
        assert ml['Bank Swallow'] == 'കരിങ്കോഴി'

    def test_threshold_skips_sparse_language(self, built):
        # Malayalam covers 1/2 species = 50% < 75% -> no file.
        out_dir, index = built
        assert not (out_dir / 'bird_names.ml.json').exists()
        assert all(l['code'] != 'ml' for l in index['languages'])

    def test_include_forces_sparse_language(self, tmp_path):
        bni = pytest.importorskip("build_bird_names_i18n", reason="tools/ must be importable")
        catalog = _write_catalog(tmp_path)
        wb_path = _write_workbook(tmp_path)
        out_dir = tmp_path / 'out'
        bni.build(wb_path, out_dir, catalog, min_coverage=0.75, include={'ml'})
        assert (out_dir / 'bird_names.ml.json').exists()

    def test_french_gaudin_not_shipped(self, built):
        out_dir, index = built
        assert not list(out_dir.glob('bird_names.fr*.json'))
        assert all(l['code'] != 'fr-gaudin' for l in index['languages'])

    def test_manifest(self, built):
        _out_dir, index = built
        assert index['catalog_species'] == 2
        codes = [l['code'] for l in index['languages']]
        assert 'de' in codes and 'ru' in codes
        de = next(l for l in index['languages'] if l['code'] == 'de')
        assert de['species_count'] == 2
        assert de['name_native'] == 'Deutsch'
        assert 0.0 <= de['coverage'] <= 1.0

    def test_stale_files_removed(self, tmp_path):
        bni = pytest.importorskip("build_bird_names_i18n", reason="tools/ must be importable")
        catalog = _write_catalog(tmp_path)
        wb_path = _write_workbook(tmp_path)
        out_dir = tmp_path / 'out'
        out_dir.mkdir()
        stale = out_dir / 'bird_names.xx.json'
        stale.write_text('{}', encoding='utf-8')
        bni.build(wb_path, out_dir, catalog, min_coverage=0.75, include=set())
        assert not stale.exists()


# ----------------------------------------------------------------------------
# bird_catalog loaders
# ----------------------------------------------------------------------------

@pytest.fixture()
def birds_dir(tmp_path, monkeypatch):
    d = tmp_path / 'birds'
    d.mkdir()
    (d / 'bird_names.de.json').write_text(
        json.dumps({'Sand Martin': 'Uferschwalbe'}), encoding='utf-8')
    (d / 'bird_names_index.json').write_text(json.dumps({
        'generated_from': 'fixture',
        'catalog_species': 2,
        'languages': [
            {'code': 'de', 'name_native': 'Deutsch', 'species_count': 1, 'coverage': 0.5},
        ],
    }), encoding='utf-8')
    monkeypatch.setattr(bird_catalog, '_name_languages_cache', None)
    bird_catalog._name_translations_cache.clear()
    return d


class TestLoaders:

    def test_round_trip(self, birds_dir):
        m = bird_catalog.load_name_translations('de', base_dir=birds_dir)
        assert m == {'Sand Martin': 'Uferschwalbe'}
        # Cached: same object on second call.
        assert bird_catalog.load_name_translations('de', base_dir=birds_dir) is m

    def test_invalid_code_never_touches_fs(self, birds_dir):
        assert bird_catalog.load_name_translations('../escape', base_dir=birds_dir) == {}
        assert bird_catalog.load_name_translations('DE', base_dir=birds_dir) == {}
        assert bird_catalog.load_name_translations('d', base_dir=birds_dir) == {}

    def test_missing_file_yields_empty(self, birds_dir):
        assert bird_catalog.load_name_translations('fr', base_dir=birds_dir) == {}

    def test_available_languages(self, birds_dir):
        langs = bird_catalog.available_name_languages(base_dir=birds_dir)
        assert [l['code'] for l in langs] == ['de']
        assert langs[0]['name_native'] == 'Deutsch'

    def test_available_languages_tolerates_missing_manifest(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bird_catalog, '_name_languages_cache', None)
        assert bird_catalog.available_name_languages(base_dir=tmp_path) == []

    @pytest.mark.parametrize('code,ok', [
        ('en', True), ('ru', True), ('pt-pt', True), ('zh-tw', True),
        ('EN', False), ('e', False), ('eng', False), ('pt-br-x', False), ('', False),
    ])
    def test_code_shape(self, code, ok):
        assert bird_catalog.is_valid_language_code(code) is ok


# ----------------------------------------------------------------------------
# Bridge methods
# ----------------------------------------------------------------------------

class TestBridge:

    def test_junk_code_rejected(self):
        sys.path.insert(0, str(ANALYZER_DIR))
        import api_bridge
        api = api_bridge.Api()
        res = api.get_bird_name_translations('../../etc/passwd')
        assert res['success'] is False
        assert res['map'] == {}

    def test_english_is_identity(self):
        import api_bridge
        api = api_bridge.Api()
        res = api.get_bird_name_translations('en')
        assert res['success'] is True and res['lang'] == 'en'

    def test_real_repo_languages(self):
        import api_bridge
        api = api_bridge.Api()
        res = api.get_bird_name_languages()
        assert res['success'] is True
        codes = [l['code'] for l in res['languages']]
        assert 'ru' in codes and 'en' not in codes


# ----------------------------------------------------------------------------
# Settings sanitizer
# ----------------------------------------------------------------------------

class TestLanguageSetting:

    @pytest.mark.parametrize('raw,expected', [
        ('ru', 'ru'), ('pt-pt', 'pt-pt'), ('zh-tw', 'zh-tw'),
        ('EN', 'en'), ('de-DE', 'en'), ('x', 'en'), ('', 'en'),
        (123, 'en'), (None, 'en'), ('../x', 'en'),
    ])
    def test_sanitize(self, raw, expected):
        out = settings_utils._sanitize_settings_payload({'language': raw})
        assert out['language'] == expected

    @pytest.mark.parametrize('raw,expected', [
        ('de', 'de'), ('fr', 'fr'), ('pt-pt', 'pt-pt'),
        ('EN', 'en'), ('de-DE', 'en'), ('x', 'en'), ('', 'en'),
        (123, 'en'), (None, 'en'), ('../x', 'en'),
    ])
    def test_sanitize_bird_name_language(self, raw, expected):
        out = settings_utils._sanitize_settings_payload({'bird_name_language': raw})
        assert out['bird_name_language'] == expected

    def test_absent_key_not_added(self):
        assert 'language' not in settings_utils._sanitize_settings_payload({})
        assert 'bird_name_language' not in settings_utils._sanitize_settings_payload({})

    def test_settings_are_independent(self):
        out = settings_utils._sanitize_settings_payload({'language': 'de', 'bird_name_language': 'fr'})
        assert out['language'] == 'de'
        assert out['bird_name_language'] == 'fr'


# ----------------------------------------------------------------------------
# Dictionary / markup consistency
# ----------------------------------------------------------------------------

_I18N_ATTRS = ('data-i18n', 'data-i18n-title', 'data-i18n-aria',
               'data-i18n-placeholder', 'data-i18n-html', 'data-i18n-data-tip')

# UI languages shipped as analyzer/i18n/<lang>.json. Kept in sync with
# UI_LANGUAGES in analyzer/js/i18n.js.
SHIPPED_UI_LANGS = ('ru', 'de', 'fr')

_PLACEHOLDER_RE = re.compile(r'\{(\w+)\}')
_TAG_RE = re.compile(r'<[^>]+>')
_ID_ATTR_RE = re.compile(r'id="([^"]+)"')
# t('key') / t("key") with a string literal — must not be preceded by a word
# char or dot so member calls like ``foo.t('x')`` are not matched.
_T_CALL_RE = re.compile(r'(?<![.\w$])t\(\s*[\'"]([a-z][a-zA-Z0-9_.]*)[\'"]')


def _used_markup_keys():
    html = (ANALYZER_DIR / 'visualizer.html').read_text(encoding='utf-8')
    used = set()
    for attr in _I18N_ATTRS:
        used |= set(re.findall(attr + r'="([^"]+)"', html))
    return used


def _used_t_call_keys():
    used = set()
    for js in (ANALYZER_DIR / 'js').glob('*.js'):
        if js.name == 'i18n.js':
            continue
        src = js.read_text(encoding='utf-8')
        used |= set(_T_CALL_RE.findall(src))
    return used


class TestDictionaryConsistency:

    def test_all_markup_keys_exist_in_en_dict(self):
        en = json.loads((ANALYZER_DIR / 'i18n' / 'en.json').read_text(encoding='utf-8'))
        missing = sorted(k for k in _used_markup_keys() if k not in en)
        assert not missing, f"data-i18n keys missing from i18n/en.json: {missing}"

    def test_all_t_call_keys_exist_in_en_dict(self):
        en = json.loads((ANALYZER_DIR / 'i18n' / 'en.json').read_text(encoding='utf-8'))
        missing = sorted(k for k in _used_t_call_keys() if k not in en)
        assert not missing, f"t('...') keys missing from i18n/en.json: {missing}"

    def test_no_orphan_keys_in_en_dict(self):
        en = json.loads((ANALYZER_DIR / 'i18n' / 'en.json').read_text(encoding='utf-8'))
        used = _used_markup_keys() | _used_t_call_keys()
        orphan = sorted(k for k in en if k not in used)
        assert not orphan, f"en.json keys not used anywhere: {orphan}"


class TestShippedUiDictionaries:

    @pytest.fixture(scope='class')
    def en_dict(self):
        return json.loads((ANALYZER_DIR / 'i18n' / 'en.json').read_text(encoding='utf-8'))

    @pytest.mark.parametrize('lang', SHIPPED_UI_LANGS)
    def test_matches_en_key_set(self, lang, en_dict):
        d = json.loads((ANALYZER_DIR / 'i18n' / f'{lang}.json').read_text(encoding='utf-8'))
        assert set(d) == set(en_dict), (
            f"{lang}.json key drift: missing={sorted(set(en_dict) - set(d))[:8]} "
            f"extra={sorted(set(d) - set(en_dict))[:8]}"
        )

    @pytest.mark.parametrize('lang', SHIPPED_UI_LANGS)
    def test_values_nonempty_strings(self, lang):
        d = json.loads((ANALYZER_DIR / 'i18n' / f'{lang}.json').read_text(encoding='utf-8'))
        bad = sorted(k for k, v in d.items() if not isinstance(v, str) or not v.strip())
        assert not bad, f"{lang}.json empty/non-string values: {bad[:8]}"

    @pytest.mark.parametrize('lang', SHIPPED_UI_LANGS)
    def test_placeholders_match_en(self, lang, en_dict):
        d = json.loads((ANALYZER_DIR / 'i18n' / f'{lang}.json').read_text(encoding='utf-8'))
        bad = sorted(k for k in d
                     if sorted(_PLACEHOLDER_RE.findall(en_dict[k])) != sorted(_PLACEHOLDER_RE.findall(d[k])))
        assert not bad, f"{lang}.json placeholder drift: {bad[:8]}"

    @pytest.mark.parametrize('lang', SHIPPED_UI_LANGS)
    def test_markup_matches_en(self, lang, en_dict):
        # Tag/id sets must match exactly; ORDER may differ where grammar
        # forces it (e.g. "the <code>.kestrel/</code> folder of <b id=...>"
        # vs "le dossier <b id=...> et son répertoire <code>...").
        d = json.loads((ANALYZER_DIR / 'i18n' / f'{lang}.json').read_text(encoding='utf-8'))
        bad = []
        for k in d:
            if sorted(_TAG_RE.findall(en_dict[k])) != sorted(_TAG_RE.findall(d[k])):
                bad.append(k)
            elif sorted(_ID_ATTR_RE.findall(en_dict[k])) != sorted(_ID_ATTR_RE.findall(d[k])):
                bad.append(k)
        assert not bad, f"{lang}.json markup drift (tags/ids): {bad[:8]}"
