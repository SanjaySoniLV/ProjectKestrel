"""Unit tests for bird-name i18n: converter, loaders, bridge methods, settings.

Covers the contracts the display-translation feature depends on:

  * ``tools/build_bird_names_i18n.py`` joins the Multiling workbook to the
    bundled catalog on scientific name (English-name fallback), keys the
    output by scientific name, refuses a workbook from a different IOC
    release than the catalog, honors the coverage threshold + ``--include``,
    and writes a manifest.
  * ``kestrel_analyzer.bird_catalog`` translation loaders validate language
    tags before touching the filesystem, join scientific names back onto the
    catalog's English names + aliases, walk the BCP-47 fallback chain, and
    tolerate missing files.
  * The bridge methods reject junk codes without path escapes.
  * The ``language`` / ``bird_name_language`` settings are canonical BCP-47.

The UI-string dictionary tests live in ``test_ui_i18n.py``.
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


def _write_workbook(tmp_path: Path, ioc_version: str = bird_catalog.CATALOG_IOC_VERSION) -> Path:
    openpyxl = pytest.importorskip("openpyxl", reason="converter tests need openpyxl")
    wb_path = tmp_path / 'multiling.xlsx'
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'List'
    ws.append(['seq', 'Order', 'Family', f'IOC_{ioc_version}', 'English',
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

    def test_keyed_by_scientific_name(self, built):
        out_dir, _ = built
        de = json.loads((out_dir / 'bird_names.de.json').read_text(encoding='utf-8'))
        assert de['Riparia riparia'] == 'Uferschwalbe'
        # No English keys: aliases are resolved at load time, not stored.
        assert 'Sand Martin' not in de and 'Bank Swallow' not in de

    def test_english_fallback_join(self, built):
        # Row 2's binomial is unknown to the catalog; the English-name join
        # still files it under the catalog's scientific name.
        out_dir, _ = built
        de = json.loads((out_dir / 'bird_names.de.json').read_text(encoding='utf-8'))
        assert de['Struthio camelus'] == 'Strauß'

    def test_empty_cells_dropped(self, tmp_path):
        bni = pytest.importorskip("build_bird_names_i18n", reason="tools/ must be importable")
        catalog = _write_catalog(tmp_path)
        wb_path = _write_workbook(tmp_path)
        out_dir = tmp_path / 'out'
        bni.build(wb_path, out_dir, catalog, min_coverage=0.75, include={'ml'})
        ml = json.loads((out_dir / 'bird_names.ml.json').read_text(encoding='utf-8'))
        assert 'Struthio camelus' not in ml
        assert ml['Riparia riparia'] == 'കരിങ്കോഴി'

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
        assert index['ioc_version'] == bird_catalog.CATALOG_IOC_VERSION
        assert index['keyed_by'] == 'scientific_name'
        codes = [l['code'] for l in index['languages']]
        assert 'de' in codes and 'ru' in codes
        de = next(l for l in index['languages'] if l['code'] == 'de')
        assert de['species_count'] == 2
        assert de['name_native'] == 'Deutsch'
        assert 0.0 <= de['coverage'] <= 1.0

    def test_notices_attribution_written(self, built):
        out_dir, _ = built
        notices = (out_dir / 'NOTICES.md').read_text(encoding='utf-8')
        assert '<!-- bird-names-i18n:begin -->' in notices
        assert 'creativecommons.org/licenses/by/3.0' in notices
        assert f'Multiling IOC {bird_catalog.CATALOG_IOC_VERSION}' in notices

    def test_version_mismatch_refused(self, tmp_path):
        bni = pytest.importorskip("build_bird_names_i18n", reason="tools/ must be importable")
        catalog = _write_catalog(tmp_path)
        wb_path = _write_workbook(tmp_path, ioc_version='99.9')
        with pytest.raises(SystemExit):
            bni.build(wb_path, tmp_path / 'out', catalog, min_coverage=0.75, include=set())
        # Explicit override still works.
        index = bni.build(wb_path, tmp_path / 'out', catalog, min_coverage=0.75,
                          include=set(), expected_ioc_version=None)
        assert index['ioc_version'] == '99.9'

    def test_language_columns_are_canonical_bcp47(self):
        bni = pytest.importorskip("build_bird_names_i18n", reason="tools/ must be importable")
        from kestrel_analyzer.language_codes import is_valid_language_tag
        bad = [c for c, _ in bni.LANGUAGE_COLUMNS.values() if not is_valid_language_tag(c)]
        assert not bad

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
        json.dumps({'Riparia riparia': 'Uferschwalbe'}), encoding='utf-8')
    (d / 'bird_names.pt.json').write_text(
        json.dumps({'Riparia riparia': 'andorinha-das-barreiras',
                    'Struthio camelus': 'avestruz'}), encoding='utf-8')
    (d / 'bird_names.pt-PT.json').write_text(
        json.dumps({'Riparia riparia': 'andorinha-das-barreiras-pt'}), encoding='utf-8')
    (d / 'bird_names_index.json').write_text(json.dumps({
        'generated_from': 'fixture',
        'catalog_species': 2,
        'languages': [
            {'code': 'de', 'name_native': 'Deutsch', 'species_count': 1, 'coverage': 0.5},
            {'code': 'not a tag', 'name_native': 'x', 'species_count': 1, 'coverage': 0.5},
        ],
    }), encoding='utf-8')
    monkeypatch.setattr(bird_catalog, '_name_languages_cache', None)
    bird_catalog._name_translations_cache.clear()
    bird_catalog._sci_translations_cache.clear()
    return d


_FIXTURE_RECORDS = [
    bird_catalog.BirdRecord(
        canonical_common_name='Sand Martin', scientific_name='Riparia riparia',
        family_sci='Hirundinidae', family_common='Swallows', order='Passeriformes',
        regions=frozenset({'PAL'}), alpha_4='', aliases=('Bank Swallow',), is_model_species=True),
    bird_catalog.BirdRecord(
        canonical_common_name='Common Ostrich', scientific_name='Struthio camelus',
        family_sci='Struthionidae', family_common='Ostriches', order='Struthioniformes',
        regions=frozenset({'AF'}), alpha_4='', aliases=(), is_model_species=False),
]


class TestLoaders:

    def test_by_sci_round_trip(self, birds_dir):
        assert bird_catalog.load_name_translations_by_sci('de', base_dir=birds_dir) == {
            'Riparia riparia': 'Uferschwalbe'}

    def test_english_view_includes_aliases(self, birds_dir):
        m = bird_catalog.load_name_translations('de', base_dir=birds_dir, catalog=_FIXTURE_RECORDS)
        assert m == {'Sand Martin': 'Uferschwalbe', 'Bank Swallow': 'Uferschwalbe'}
        # Cached: same object on second call.
        assert bird_catalog.load_name_translations(
            'de', base_dir=birds_dir, catalog=_FIXTURE_RECORDS) is m

    def test_regional_tag_falls_back_to_base_language(self, birds_dir):
        # No pt-BR file: pt-BR -> pt.
        m = bird_catalog.load_name_translations('pt-BR', base_dir=birds_dir, catalog=_FIXTURE_RECORDS)
        assert m['Sand Martin'] == 'andorinha-das-barreiras'
        assert m['Common Ostrich'] == 'avestruz'

    def test_more_specific_file_wins_and_base_fills_gaps(self, birds_dir):
        m = bird_catalog.load_name_translations('pt-PT', base_dir=birds_dir, catalog=_FIXTURE_RECORDS)
        assert m['Sand Martin'] == 'andorinha-das-barreiras-pt'
        assert m['Common Ostrich'] == 'avestruz'

    def test_invalid_code_never_touches_fs(self, birds_dir):
        assert bird_catalog.load_name_translations('../escape', base_dir=birds_dir) == {}
        assert bird_catalog.load_name_translations('DE', base_dir=birds_dir) == {}
        assert bird_catalog.load_name_translations('d', base_dir=birds_dir) == {}

    def test_missing_file_yields_empty(self, birds_dir):
        assert bird_catalog.load_name_translations('fr', base_dir=birds_dir,
                                                   catalog=_FIXTURE_RECORDS) == {}

    def test_available_languages(self, birds_dir):
        langs = bird_catalog.available_name_languages(base_dir=birds_dir)
        assert [l['code'] for l in langs] == ['de']
        assert langs[0]['name_native'] == 'Deutsch'

    def test_available_languages_tolerates_missing_manifest(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bird_catalog, '_name_languages_cache', None)
        assert bird_catalog.available_name_languages(base_dir=tmp_path) == []

    @pytest.mark.parametrize('code,ok', [
        ('en', True), ('ru', True), ('pt-PT', True), ('zh-Hant', True), ('pt-BR', True),
        ('es-419', True), ('sr-Latn', True), ('zh-Hant-TW', True),
        ('EN', False), ('pt-pt', False), ('e', False), ('english', False),
        ('pt-br-x', False), ('', False), ('../x', False),
    ])
    def test_code_shape(self, code, ok):
        assert bird_catalog.is_valid_language_code(code) is ok


class TestShippedBirdNameData:
    """The committed bird_names.<tag>.json files and their manifest."""

    BIRDS = ANALYZER_DIR / 'models' / 'birds'

    def test_manifest_matches_catalog_release(self):
        idx = json.loads((self.BIRDS / 'bird_names_index.json').read_text(encoding='utf-8'))
        assert idx['ioc_version'] == bird_catalog.CATALOG_IOC_VERSION
        assert idx['keyed_by'] == 'scientific_name'

    def test_files_match_manifest(self):
        idx = json.loads((self.BIRDS / 'bird_names_index.json').read_text(encoding='utf-8'))
        codes = {l['code'] for l in idx['languages']}
        files = {p.name[len('bird_names.'):-len('.json')] for p in self.BIRDS.glob('bird_names.*.json')}
        assert codes == files
        assert all(bird_catalog.is_valid_language_code(c) for c in codes)

    def test_keys_are_catalog_scientific_names(self):
        sci = {r.scientific_name for r in bird_catalog.load_catalog()}
        for p in self.BIRDS.glob('bird_names.*.json'):
            data = json.loads(p.read_text(encoding='utf-8'))
            stray = [k for k in data if k not in sci]
            assert not stray, f"{p.name}: keys not in catalog: {stray[:5]}"

    def test_notices_credit_multiling(self):
        notices = (self.BIRDS / 'NOTICES.md').read_text(encoding='utf-8')
        assert f'Multiling IOC {bird_catalog.CATALOG_IOC_VERSION}' in notices
        block = notices.split('<!-- bird-names-i18n:begin -->')[1]
        assert 'creativecommons.org/licenses/by/3.0' in block


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
        assert res['success'] is True and res['lang'] == 'en' and res['map'] == {}

    def test_real_repo_translation_by_english_name(self):
        import api_bridge
        api = api_bridge.Api()
        res = api.get_bird_name_translations('de')
        assert res['success'] is True
        assert res['map'].get('Common Ostrich') == 'Strauß'

    def test_case_is_canonicalized_and_region_falls_back(self):
        import api_bridge
        api = api_bridge.Api()
        res = api.get_bird_name_translations('pt-br')
        assert res['success'] is True and res['lang'] == 'pt-BR'
        assert res['map'], 'pt-BR should fall back to the pt bird-name file'

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
        ('ru', 'ru'), ('pt-pt', 'pt-PT'), ('pt-BR', 'pt-BR'), ('zh-hans', 'zh-Hans'),
        ('zh_Hant', 'zh-Hant'), ('es-419', 'es-419'),
        ('EN', 'en'), ('de-DE', 'de-DE'), ('x', 'en'), ('', 'en'),
        (123, 'en'), (None, 'en'), ('../x', 'en'), ('en-US-x-private', 'en'),
    ])
    def test_sanitize(self, raw, expected):
        out = settings_utils._sanitize_settings_payload({'language': raw})
        assert out['language'] == expected

    @pytest.mark.parametrize('raw,expected', [
        ('de', 'de'), ('fr', 'fr'), ('pt-pt', 'pt-PT'), ('zh-Hant', 'zh-Hant'),
        ('EN', 'en'), ('x', 'en'), ('', 'en'),
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
