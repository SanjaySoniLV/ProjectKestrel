"""Split a smart-search query into species/family filters and a free-text remainder.

"heron in a marsh" -> filter family *Heron/Egret/Bittern sp.* + SigLIP query
"in a marsh"; "dove pair" -> *Pigeon/Dove sp.* + "pair"; "Cooper's hawks
perched" -> species *Cooper's Hawk* + "perched".

Matching is a greedy longest-n-gram pass (up to 6 words) over:

* the IOC bird catalog: canonical common names, their aliases, scientific
  names, and family names (each ``/``/``,``/``&`` part of a display family
  such as "Hawk/Eagle/Kite sp." on its own, plus the scientific family);
* the BioCLIP wildlife labels (non-bird animals);
* folder-aware "group" words: a single word that is the last word of a
  species tagged in the folder ("robin" -> American Robin when the folder has
  one; "deer" -> White-Tailed Deer).

Normalisation folds case, hyphens, apostrophes and plurals ("hawks",
"geese", "flies"); words of 5+ letters tolerate one typo (two at 10+).
When a phrase names several things, the one tagged in the folder wins, then
one in the folder's region, then an exact over a fuzzy match.

Everything here is pure Python over the catalog; nothing loads a model.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

MAX_NGRAM = 6

# Words never treated as (part of) a filter on their own.
STOPWORDS = frozenset("""
a an the of in on at by with and or to from for into onto over under near its it is are
photo photos picture image images shot bird birds animal animals wildlife some two three
pair flock group one single sharp blurry blur focus focused flying flight perched perching
eating feeding sitting standing walking swimming wading looking close closeup up portrait
""".split())

# Real catalog names that are far more often ordinary words in a query.
AMBIGUOUS = frozenset({"rail", "swift", "tit", "shag", "booby", "sora", "ruff", "post"})

_APOS = re.compile(r"[’'`]")
_NONWORD = re.compile(r"[^0-9a-z ]+")


def normalize(text: str) -> str:
    t = _APOS.sub("", str(text or "").lower())
    t = t.replace("-", " ").replace("/", " ")
    t = _NONWORD.sub(" ", t)
    return " ".join(t.split())


_IRREGULAR = {"geese": "goose", "mice": "mouse", "teeth": "tooth", "feet": "foot", "oxen": "ox",
              "wolves": "wolf", "calves": "calf", "halves": "half", "leaves": "leaf",
              "children": "child", "people": "person", "fishes": "fish", "octopi": "octopus"}


def singular(tok: str) -> str:
    if tok in _IRREGULAR:
        return _IRREGULAR[tok]
    if len(tok) <= 3:
        return tok
    if tok.endswith("ies") and len(tok) > 4:
        return tok[:-3] + "y"
    if tok.endswith("sses"):
        return tok[:-2]
    if tok.endswith(("ches", "shes", "xes", "zes")):
        return tok[:-2]
    if tok.endswith("s") and not tok.endswith(("ss", "us", "is")):
        return tok[:-1]
    return tok


def key_of(text: str) -> str:
    return " ".join(singular(t) for t in normalize(text).split())


def _lev_within(a: str, b: str, k: int) -> bool:
    """Levenshtein(a, b) <= k (banded, early exit)."""
    if abs(len(a) - len(b)) > k:
        return False
    if a == b:
        return True
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        lo = max(1, i - k)
        hi = min(len(b), i + k)
        if lo > 1:
            cur[lo - 1] = k + 1
        best = cur[lo - 1] if lo > 1 else i
        for j in range(lo, hi + 1):
            cost = 0 if ca == b[j - 1] else 1
            v = min(prev[j] + 1, (cur[j - 1] if j - 1 >= lo - 1 else k + 1) + 1, prev[j - 1] + cost)
            cur[j] = v
            best = min(best, v)
        for j in range(hi + 1, len(b) + 1):
            cur[j] = k + 1
        if best > k:
            return False
        prev = cur
    return prev[len(b)] <= k


def _typo_budget(key: str) -> int:
    n = len(key.replace(" ", ""))
    if n >= 10:
        return 2
    if n >= 5:
        return 1
    return 0


@dataclass(frozen=True)
class Target:
    kind: str           # "species" | "family" | "wildlife" | "group"
    label: str          # the tag value stored in the folder (canonical name / family display name)
    scientific: str = ""
    regions: frozenset = frozenset()
    model_species: bool = False


@dataclass
class Match:
    kind: str
    label: str
    text: str                    # the query words it consumed (as typed)
    start: int
    end: int                     # token span [start, end)
    scientific: str = ""
    fuzzy: bool = False
    labels: Tuple[str, ...] = ()  # for "group": every folder tag it stands for

    def to_dict(self) -> dict:
        return {"kind": self.kind, "label": self.label, "text": self.text, "scientific": self.scientific,
                "fuzzy": self.fuzzy, "labels": list(self.labels or (self.label,))}


@dataclass
class ParsedQuery:
    query: str
    remainder: str
    matches: List[Match] = field(default_factory=list)

    @property
    def has_filter(self) -> bool:
        return bool(self.matches)


class QueryIndex:
    """Lookup tables over the catalog + wildlife labels (built once, thread-safe to share)."""

    def __init__(self, catalog=None, wildlife_labels: Optional[Sequence[dict]] = None):
        if catalog is None:
            from .bird_catalog import get_catalog
            catalog = get_catalog()
        if wildlife_labels is None:
            wildlife_labels = _load_wildlife_labels()
        self.by_key: Dict[str, List[Target]] = {}
        fam_regions: Dict[str, Set[str]] = {}
        fam_sci: Dict[str, str] = {}
        for r in catalog.records:
            t = Target("species", r.canonical_common_name, r.scientific_name, frozenset(r.regions),
                       bool(getattr(r, "is_model_species", False)))
            for name in (r.canonical_common_name, *tuple(r.aliases or ()), r.scientific_name):
                self._add(name, t)
            if r.family_common:
                fam_regions.setdefault(r.family_common, set()).update(r.regions)
                if r.family_sci:
                    fam_sci.setdefault(r.family_common, r.family_sci)
        for fam, regs in fam_regions.items():
            t = Target("family", fam, fam_sci.get(fam, ""), frozenset(regs))
            for part in family_parts(fam):
                self._add(part, t)
            if fam_sci.get(fam):
                self._add(fam_sci[fam], t)
        for lab in wildlife_labels or ():
            common = str(lab.get("common_name") or "").strip()
            if not common or common.endswith((" species", " family")):
                continue
            from .ml.bioclip_species import format_display_name
            t = Target("wildlife", format_display_name(common), str(lab.get("scientific_name") or ""))
            self._add(common, t)
            if lab.get("scientific_name"):
                self._add(str(lab["scientific_name"]), t)
        # Fuzzy candidates bucketed by (word count, length).
        self._buckets: Dict[Tuple[int, int], List[str]] = {}
        for k in self.by_key:
            self._buckets.setdefault((k.count(" ") + 1, len(k)), []).append(k)

    def _add(self, name: str, target: Target) -> None:
        k = key_of(name)
        if not k or k in STOPWORDS:
            return
        lst = self.by_key.setdefault(k, [])
        if target not in lst:
            lst.append(target)

    def lookup(self, key: str, allow_fuzzy: bool = True) -> Tuple[List[Target], bool]:
        hit = self.by_key.get(key)
        if hit:
            return hit, False
        budget = _typo_budget(key) if allow_fuzzy else 0
        if budget <= 0:
            return [], False
        words = key.count(" ") + 1
        found: List[Target] = []
        for L in range(len(key) - budget, len(key) + budget + 1):
            for cand in self._buckets.get((words, L), ()):
                if cand[0] != key[0] and budget < 2:
                    continue  # one-typo matches must agree on the first letter
                if _lev_within(key, cand, budget):
                    for t in self.by_key[cand]:
                        if t not in found:
                            found.append(t)
        return found, bool(found)


def family_parts(family_display: str) -> List[str]:
    """'Hawk/Eagle/Kite sp.' -> ['hawk', 'eagle', 'kite']; 'Cassowaries, Emu' -> [...]."""
    s = re.sub(r"\bsp\.?$", "", str(family_display or "").strip()).strip()
    s = re.sub(r"(&\s*)?\ballies\b", "", s, flags=re.I)
    parts = re.split(r"[/,&]| and ", s)
    out = [p.strip() for p in parts if p.strip()]
    whole = s.strip()
    if whole and whole not in out:
        out.append(whole)
    return out


def _load_wildlife_labels() -> List[dict]:
    try:
        from .config import BIOCLIP_MODELS_DIR
        with open(BIOCLIP_MODELS_DIR / "wildlife_index.json", encoding="utf-8") as f:
            return list(json.load(f).get("labels") or [])
    except Exception:
        return []


_shared_index: Optional[QueryIndex] = None
_shared_lock = threading.Lock()


def shared_index() -> QueryIndex:
    global _shared_index
    with _shared_lock:
        if _shared_index is None:
            _shared_index = QueryIndex()
        return _shared_index


def parse_query(
    query: str,
    index: Optional[QueryIndex] = None,
    folder_species: Iterable[str] = (),
    folder_families: Iterable[str] = (),
    regions: Optional[Iterable[str]] = None,
    disabled_terms: Iterable[str] = (),
) -> ParsedQuery:
    """Parse ``query``. ``folder_species``/``folder_families`` are the tags present in
    the folder (preferred when ambiguous, and the source of "group" words);
    ``regions`` the folder's species regions; ``disabled_terms`` are query
    phrases the user un-chipped (never matched again, kept in the remainder)."""
    index = index or shared_index()
    raw_tokens = str(query or "").split()
    tokens = [normalize(t) for t in raw_tokens]
    keys = [" ".join(singular(w) for w in t.split()) for t in tokens]
    disabled = {key_of(t) for t in disabled_terms or () if str(t).strip()}
    present_sp = {normalize(s): s for s in folder_species or () if s}
    present_fam = {normalize(f): f for f in folder_families or () if f}
    region_set = frozenset(r for r in (regions or ()) if r and str(r).lower() != "global")

    def rank(t: Target, fuzzy: bool) -> tuple:
        present = normalize(t.label) in (present_fam if t.kind == "family" else present_sp)
        in_region = (not region_set) or ("Worldwide" in t.regions) or bool(region_set & t.regions) \
            or t.kind == "wildlife"
        return (present, in_region, not fuzzy, t.kind != "wildlife", t.model_species)

    used = [False] * len(tokens)
    n_tok = len(tokens)
    # Words of an un-chipped phrase stay plain text: no match may touch them.
    blocked = [False] * n_tok
    for dk in disabled:
        dw = dk.split()
        for i in range(0, n_tok - len(dw) + 1):
            if keys[i:i + len(dw)] == dw:
                for j in range(i, i + len(dw)):
                    blocked[j] = True
    matches: List[Match] = []
    for n in range(min(MAX_NGRAM, n_tok), 0, -1):
        for i in range(0, n_tok - n + 1):
            if any(used[i:i + n]) or any(blocked[i:i + n]) or not all(keys[i:i + n]):
                continue
            k = " ".join(keys[i:i + n])
            if not k or k in disabled:
                continue
            if n == 1 and (k in STOPWORDS or k in AMBIGUOUS or len(k) < 3):
                continue
            if n > 1 and all(w in STOPWORDS for w in k.split()):
                continue
            targets, fuzzy = index.lookup(k, allow_fuzzy=(n > 1 or len(k) >= 5))
            if not targets:
                continue
            best = max(targets, key=lambda t: rank(t, fuzzy))
            matches.append(Match(best.kind, best.label, " ".join(raw_tokens[i:i + n]), i, i + n,
                                 best.scientific, fuzzy))
            for j in range(i, i + n):
                used[j] = True
    # Folder-aware group words: "robin" -> the folder's *Robin species.
    if present_sp:
        last_word: Dict[str, List[str]] = {}
        for norm, label in present_sp.items():
            words = norm.split()
            if words:
                last_word.setdefault(singular(words[-1]), []).append(label)
        for i, k in enumerate(keys):
            if used[i] or blocked[i] or not k or k in STOPWORDS or len(k) < 3 or " " in k:
                continue
            labels = last_word.get(k)
            if labels:
                matches.append(Match("group", raw_tokens[i], raw_tokens[i], i, i + 1, "", False,
                                     tuple(sorted(labels))))
                used[i] = True
    matches.sort(key=lambda m: m.start)
    rem_words = [t for t, u in zip(raw_tokens, used) if not u]
    while rem_words and normalize(rem_words[-1]) in _TRAILING:
        rem_words.pop()
    if all(normalize(w) in FILLER for w in rem_words):
        rem_words = []
    return ParsedQuery(query=" ".join(raw_tokens), remainder=" ".join(rem_words), matches=matches)


# Words that carry no visual meaning on their own once a filter took the subject.
FILLER = frozenset("a an the of in on at photo photos picture pictures image images shot shots "
                   "bird birds animal animals some".split())
_TRAILING = frozenset("a an the of and or with".split())


def semantic_text(parsed: ParsedQuery) -> str:
    """The text sent to SigLIP (wrapped later as ``"a photo of {text}."``).

    Empty when nothing visual remains after the filters (filter-only search,
    sorted by quality). Otherwise the remainder -- but when a filter matched,
    the whole query as typed: "dove pair" reads better to SigLIP than "pair",
    "heron in a marsh" better than "in a marsh", and the filter already
    restricts the candidates to doves / herons.
    """
    rem = parsed.remainder.strip()
    if not rem:
        return ""
    if parsed.matches:
        return parsed.query.strip()
    return rem


@dataclass(frozen=True)
class QueryRoute:
    """How the single search box handles a query (see :func:`classify_query`)."""
    kind: str                      # "empty" | "filter" | "semantic"
    labels: Tuple[str, ...] = ()   # filter: the species/family tag values to match


def classify_query(
    query: str,
    index: Optional[QueryIndex] = None,
    folder_species: Iterable[str] = (),
    folder_families: Iterable[str] = (),
) -> QueryRoute:
    """Decide whether a search-box query is a plain species/family filter.

    ``"filter"``: after the catalog match (common/scientific names, families,
    wildlife labels, folder "group" words; plurals and small typos tolerated)
    every word of the trimmed query is part of a match, apart from filler such
    as "birds" or a trailing "and". "Spoonbill", "roseate spoonbills" and
    "spoonbil" qualify. ``labels`` are the tag values to filter by.

    ``"semantic"``: anything with words left over ("spoonbill wading",
    "bird in flight at sunset") or no catalog match at all. ``"empty"``: blank.
    """
    q = " ".join(str(query or "").split())
    if not q:
        return QueryRoute("empty")
    parsed = parse_query(q, index=index, folder_species=folder_species, folder_families=folder_families)
    if parsed.matches and not parsed.remainder.strip():
        labels: List[str] = []
        for m in parsed.matches:
            for lab in (m.labels or (m.label,)):
                if lab and lab not in labels:
                    labels.append(lab)
        return QueryRoute("filter", tuple(labels))
    return QueryRoute("semantic")
