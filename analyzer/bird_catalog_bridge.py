"""Bird catalog methods of the JS bridge: regions, species and family search.

``api_bridge.Api`` inherits ``BirdCatalogBridgeMixin``, so the frontend calls
the same method names as before. The catalog itself is
``kestrel_analyzer.bird_catalog``.
"""
from __future__ import annotations

from settings_utils import error


class BirdCatalogBridgeMixin:
    """Region metadata and fuzzy search over the bundled bird catalog."""

    def get_species_family_map(self):
        """Return a {species_display_name: family_display_name} mapping for
        every species in the bundled IOC world catalog (~11,250 rows).

        The species classifier (BioCLIP) can name any of these, so the
        frontend's species→family auto-link must cover all of them, not just
        the legacy CNN's 498 North-American labels. Family names are the
        catalog's ``family_common`` display names ("Thrush sp."), the same
        strings the classifier writes to the ``family`` column. Cached on the
        bridge instance.
        """
        cached = getattr(self, '_species_family_map_cache', None)
        if cached is not None:
            return cached
        try:
            cat = self._get_bird_catalog()
            if cat is None:
                raise RuntimeError('catalog unavailable')
            mapping: dict[str, str] = {}
            for rec in cat.records:
                if rec.canonical_common_name and rec.family_common:
                    mapping[rec.canonical_common_name] = rec.family_common
            result = {'success': True, 'map': mapping}
        except Exception as e:
            error(f'[API] get_species_family_map error: {e}')
            result = {'success': False, 'error': str(e), 'map': {}}
        self._species_family_map_cache = result
        return result

    def _get_bird_catalog(self):
        """Process-wide catalog singleton, cached on the bridge instance.

        Returns ``None`` on load failure so callers can degrade gracefully
        rather than 500-ing the entire combobox.
        """
        cached = getattr(self, '_bird_catalog_cache', None)
        if cached is not None:
            return cached
        try:
            try:
                from kestrel_analyzer.bird_catalog import get_catalog
            except ImportError:
                from analyzer.kestrel_analyzer.bird_catalog import get_catalog
            cached = get_catalog()
        except Exception as e:
            error(f'[API] bird catalog load error: {e}')
            cached = None
        self._bird_catalog_cache = cached
        return cached

    def _sanitize_species_regions(self, value):
        """Per-folder species geofence from the frontend → list or ``None``.

        ``None`` (or nothing valid) means "use the ``bird_regions`` setting";
        the worker resolves that just before the folder runs.
        """
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            return None
        vals = [v.strip() for v in value if isinstance(v, str) and v.strip()]
        if not vals:
            return None
        try:
            try:
                from kestrel_analyzer.bird_catalog import ALLOWED_REGION_CODES, GLOBAL_REGION
            except ImportError:
                from analyzer.kestrel_analyzer.bird_catalog import ALLOWED_REGION_CODES, GLOBAL_REGION
        except Exception:
            return None
        if any(v.lower() == GLOBAL_REGION for v in vals):
            return [GLOBAL_REGION]
        picked = [c for c in ALLOWED_REGION_CODES if c in set(vals)]
        return picked or None

    def get_bird_catalog_meta(self):
        """Return UI metadata: region labels + default selection + catalog size.

        Used by the frontend to populate the region picker in settings without
        downloading the full catalog. Cached separately from the records.
        """
        try:
            try:
                from kestrel_analyzer.bird_catalog import (
                    ALLOWED_REGION_CODES, REGION_LABELS, DEFAULT_REGION_SELECTION,
                )
            except ImportError:
                from analyzer.kestrel_analyzer.bird_catalog import (
                    ALLOWED_REGION_CODES, REGION_LABELS, DEFAULT_REGION_SELECTION,
                )
            cat = self._get_bird_catalog()
            return {
                'success': True,
                'regions': [
                    {'code': code, 'label': REGION_LABELS.get(code, code)}
                    for code in ALLOWED_REGION_CODES
                ],
                'default_regions': list(DEFAULT_REGION_SELECTION),
                'total_species': len(cat) if cat is not None else 0,
                # Species per region (geofence size shown in the analyze
                # dialog's region picker). Worldwide species count everywhere.
                'region_counts': {
                    code: len(cat.filter([code])) for code in ALLOWED_REGION_CODES
                } if cat is not None else {},
            }
        except Exception as e:
            error(f'[API] get_bird_catalog_meta error: {e}')
            return {'success': False, 'error': str(e),
                    'regions': [], 'default_regions': ['NA'], 'total_species': 0}

    def search_birds(self, query='', regions=None, limit=20):
        """Region-filtered fuzzy search across the global bird catalog.

        Parameters mirror the JS combobox: ``query`` is the text the user has
        typed (may be empty to seed the dropdown), ``regions`` is the list of
        biogeographic codes currently selected in Settings, and ``limit`` caps
        the number of returned records.
        """
        try:
            try:
                from kestrel_analyzer.bird_catalog import record_to_dict
            except ImportError:
                from analyzer.kestrel_analyzer.bird_catalog import record_to_dict
            cat = self._get_bird_catalog()
            if cat is None:
                return {'success': False, 'error': 'catalog unavailable', 'results': []}
            q = '' if query is None else str(query)
            sel = regions if isinstance(regions, (list, tuple)) else ['NA']
            try:
                n = int(limit)
            except (TypeError, ValueError):
                n = 20
            n = max(1, min(100, n))
            results = cat.search(q, sel, limit=n)
            return {'success': True, 'results': [record_to_dict(r) for r in results]}
        except Exception as e:
            error(f'[API] search_birds error: {e}')
            return {'success': False, 'error': str(e), 'results': []}

    def search_families(self, query='', regions=None, limit=20):
        """Region-filtered fuzzy search across unique bird families."""
        try:
            try:
                from kestrel_analyzer.bird_catalog import family_entry_to_dict
            except ImportError:
                from analyzer.kestrel_analyzer.bird_catalog import family_entry_to_dict
            cat = self._get_bird_catalog()
            if cat is None:
                return {'success': False, 'error': 'catalog unavailable', 'results': []}
            q = '' if query is None else str(query)
            sel = regions if isinstance(regions, (list, tuple)) else ['NA']
            try:
                n = int(limit)
            except (TypeError, ValueError):
                n = 20
            n = max(1, min(100, n))
            results = cat.search_families(q, sel, limit=n)
            return {'success': True, 'results': [family_entry_to_dict(f) for f in results]}
        except Exception as e:
            error(f'[API] search_families error: {e}')
            return {'success': False, 'error': str(e), 'results': []}

    def lookup_birds(self, names=None):
        """Resolve a list of canonical names (already-applied pills) to records.

        Returns a ``{ canonical_name: record_dict }`` map so the frontend can
        render the scientific-name subtext for pills it inherited from a
        previous session (where the catalog wasn't yet loaded into memory).
        Unknown names are simply omitted from the response.
        """
        try:
            try:
                from kestrel_analyzer.bird_catalog import record_to_dict
            except ImportError:
                from analyzer.kestrel_analyzer.bird_catalog import record_to_dict
            cat = self._get_bird_catalog()
            if cat is None:
                return {'success': False, 'error': 'catalog unavailable', 'map': {}}
            out: dict = {}
            if isinstance(names, (list, tuple)):
                for raw in names:
                    if not isinstance(raw, str):
                        continue
                    rec = cat.lookup(raw)
                    if rec is not None:
                        out[rec.canonical_common_name] = record_to_dict(rec)
            return {'success': True, 'map': out}
        except Exception as e:
            error(f'[API] lookup_birds error: {e}')
            return {'success': False, 'error': str(e), 'map': {}}

    def get_family_sci_map(self):
        """Return the catalog's full ``family_common -> family_sci`` map.

        Hydrated once on the JS side at startup so family-tier pills can
        resolve their italicised scientific-family subtext directly,
        without depending on a sibling species record being present in
        the same scene.
        """
        try:
            cat = self._get_bird_catalog()
            if cat is None:
                return {'success': False, 'error': 'catalog unavailable', 'map': {}}
            return {'success': True, 'map': cat.family_sci_map}
        except Exception as e:
            error(f'[API] get_family_sci_map error: {e}')
            return {'success': False, 'error': str(e), 'map': {}}
