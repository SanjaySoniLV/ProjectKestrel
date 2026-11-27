// Cull mode's rules, with no DOM and no app globals, so they can be tested
// under node. js/cull-mode.js feeds it plain photo records:
//
//   { key, q, manual, fav, time }
//     key     unique id (the file name)
//     q       Quality 0..1, or null when no bird was found
//     manual  'accept' | 'reject' when the user decided it themselves, else null
//     fav     true for a favorite
//     time    capture time in ms (NaN when unknown)
//
// Starting picks: in each scene the photos Kestrel decides (the "pool": not
// decided by the user, bird found) are sorted favorites first, then sharpest
// first. Those at or above the threshold are accepted, then the scene is
// topped up to "accept at least N" (counting the user's own accepts).
// Favorites are always among the accepts. A scene's cut, once the user drags
// it, replaces the threshold for that scene. Photos with no bird found follow
// the no-bird rule.
(function () {
  /** Star-band floors used when the rating profile can't be read. */
  const DEFAULT_EDGES = { two: 0.15, three: 0.40, four: 0.60, five: 0.85 };

  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const byQ = (a, b) => (b.q ?? -1) - (a.q ?? -1);
  const byFavThenQ = (a, b) => (b.fav ? 1 : 0) - (a.fav ? 1 : 0) || byQ(a, b);
  const byTime = (a, b) => {
    const ta = isNum(a.time) ? a.time : Infinity;
    const tb = isNum(b.time) ? b.time : Infinity;
    return ta !== tb ? ta - tb : String(a.key).localeCompare(String(b.key), undefined, { numeric: true });
  };

  function edgesList(edges) {
    const e = edges || DEFAULT_EDGES;
    return [
      { star: 2, at: Number(e.two) },
      { star: 3, at: Number(e.three) },
      { star: 4, at: Number(e.four) },
      { star: 5, at: Number(e.five) },
    ].filter(x => isNum(x.at));
  }

  /** Star band (1-5) a Quality sits in. */
  function starFor(q, edges) {
    let star = 1;
    for (const e of edgesList(edges)) if (q >= e.at) star = e.star;
    return star;
  }

  /** The star edge nearest to ``v`` (for Shift-snapping the threshold). */
  function snapToEdge(v, edges) {
    let best = v, dist = Infinity;
    for (const e of edgesList(edges)) {
      const d = Math.abs(e.at - v);
      if (d < dist) { dist = d; best = e.at; }
    }
    return best;
  }

  /** Pass N+1's default threshold: the next star edge above pass N's. */
  function nextThreshold(thr, edges) {
    const up = edgesList(edges).map(e => e.at).filter(a => a > thr + 1e-6);
    return up.length ? Math.min(...up) : Math.min(0.95, thr + 0.15);
  }

  function pool(photos) {
    return photos.filter(p => !p.manual && p.q != null).sort(byFavThenQ);
  }

  /** How many of the scene's pool photos start accepted. */
  function acceptCount(photos, rules, cut) {
    const pl = pool(photos);
    const favs = pl.filter(p => p.fav).length;
    if (cut != null) return Math.max(favs, Math.min(cut, pl.length));
    let n = pl.filter(p => p.fav || p.q >= rules.threshold).length;
    const userAccepts = photos.filter(p => p.manual === 'accept').length;
    const min = Math.max(0, rules.min_per_scene ?? 1);
    if (n + userAccepts < min) n = Math.min(pl.length, min - userAccepts);
    return n;
  }

  /**
   * Plan one scene: Kestrel's decision for every photo the user hasn't
   * decided, the strip order, and where the divider sits.
   *
   * Order 'quality': [your accepts][Kestrel's accepts] | [Kestrel's rejects]
   * [your rejects][no bird found]. Order 'time': capture order, no divider.
   * ``divider`` is the index of the first photo right of the cut, or null
   * when the scene has nothing for Kestrel to decide.
   */
  function planScene(photos, rules, cut, order) {
    const pl = pool(photos);
    const k = acceptCount(photos, rules, cut);
    const decisions = new Map();
    pl.forEach((p, i) => decisions.set(p.key, i < k ? 'accept' : 'reject'));
    const noBird = photos.filter(p => !p.manual && p.q == null);
    for (const p of noBird) decisions.set(p.key, rules.no_bird === 'accept' ? 'accept' : 'reject');

    const userA = photos.filter(p => p.manual === 'accept').sort(byQ);
    const userR = photos.filter(p => p.manual === 'reject').sort(byQ);
    let list, divider = null;
    if (order === 'time') {
      list = photos.slice().sort(byTime);
    } else {
      const left = [...userA, ...pl.slice(0, k)];
      list = [...left, ...pl.slice(k), ...userR, ...noBird];
      if (pl.length) divider = left.length;
    }
    return { decisions, order: list.map(p => p.key), divider, accepted: k, poolSize: pl.length };
  }

  /** Cut value for the pool photo keys left of a drop point, in pool order. */
  function cutFromLeftKeys(photos, leftKeys) {
    const pl = pool(photos);
    const left = new Set(leftKeys);
    let k = 0;
    pl.forEach((p, i) => { if (left.has(p.key)) k = Math.max(k, i + 1); });
    return k;
  }

  /** 20-bin histogram of Quality over photos with a bird. */
  function histogram(qs, bins = 20) {
    const out = new Array(bins).fill(0);
    for (const q of qs) {
      if (!isNum(q) || q < 0) continue;
      out[Math.min(bins - 1, Math.floor(q * bins))] += 1;
    }
    return out;
  }

  /**
   * Thumbnail width for a strip of ``n`` photos in ``width`` pixels: as big
   * as fits the whole scene in up to three rows, but never below ``minW``
   * (then the strip scrolls).
   */
  function stripLayout(n, width, minW, maxW, gap) {
    const rows = n <= 7 ? 1 : n <= 16 ? 2 : 3;
    const per = Math.max(1, Math.ceil(n / rows));
    let w = Math.min(maxW, Math.floor((width - (per - 1) * gap) / per));
    const scroll = w < minW;
    if (scroll) w = minW;
    return { w, rows, scroll };
  }

  window.KestrelCullLogic = {
    DEFAULT_EDGES, starFor, snapToEdge, nextThreshold, acceptCount,
    planScene, cutFromLeftKeys, histogram, stripLayout, edgesList,
  };
})();
