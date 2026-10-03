// ── Reviewed scene tags → per-image species/family ──────────────────────────
//
// When a user corrects a scene's species and marks it Reviewed, the correction
// is stored on the SCENE, in kestrel_scenedata.json as
// `scenes[id].user_tags = { species, families, finalized: true }`. Nothing
// writes it back onto the per-image `species` / `family` columns of
// kestrel_database.csv — those stay as the model predicted.
//
// Everywhere the app DISPLAYS a reviewed scene it uses the reviewed tags in
// place of the per-image predictions (the display rule in js/scenes.js). The
// metadata writer did not: it built its payload straight off the row columns,
// so a corrected species showed as corrected in Kestrel while the file's
// keywords and description were written with the original prediction.
//
// This module is the one place that maps a reviewed scene back onto a single
// image. It is loaded by both the main window and the standalone Culling
// Assistant, and deliberately has no dependencies.
(function (global) {
  'use strict';

  function _clean(value) {
    return String(value == null ? '' : value).trim();
  }

  // The reviewed tag list for an image's scene, or null when the scene has not
  // been reviewed. `scenes` is the scenedata `scenes` map; `sceneId` is the
  // row's scene_count.
  function reviewedTagsForScene(scenes, sceneId) {
    if (!scenes || typeof scenes !== 'object') return null;
    const entry = scenes[String(sceneId)];
    if (!entry || typeof entry !== 'object') return null;
    const tags = entry.user_tags;
    if (!tags || typeof tags !== 'object' || tags.finalized !== true) return null;
    return tags;
  }

  // Resolve one tag (species or family) for a single image.
  //
  // Only the unambiguous cases are resolved:
  //   * the row's own value is already among the reviewed tags → keep it; the
  //     prediction and the review agree.
  //   * the review names exactly one tag → that is the answer for every image
  //     in the scene. This is the case a user hits when they fix a misidentified
  //     bird and tick Reviewed.
  //
  // A scene reviewed with SEVERAL species is left alone: which of them belongs
  // to this particular image is not recorded anywhere, so overriding the
  // per-image prediction would be a guess, and a guess is worse than the
  // prediction it replaced.
  function resolveTag(rowValue, reviewedList) {
    const current = _clean(rowValue);
    if (!Array.isArray(reviewedList)) return current;
    const reviewed = reviewedList.map(_clean).filter(Boolean);
    if (!reviewed.length) return current;
    const lowered = current.toLowerCase();
    if (current && reviewed.some((t) => t.toLowerCase() === lowered)) return current;
    return reviewed.length === 1 ? reviewed[0] : current;
  }

  // Convenience: {species, family} for a row, given the scenedata scenes map.
  function resolveRowTags(scenes, row) {
    const species = row ? row.species : '';
    const family = row ? row.family : '';
    const tags = reviewedTagsForScene(scenes, row ? row.scene_count : '');
    if (!tags) return { species: _clean(species), family: _clean(family) };
    return {
      species: resolveTag(species, tags.species),
      family: resolveTag(family, tags.families),
    };
  }

  global.KestrelReviewedTags = {
    reviewedTagsForScene,
    resolveTag,
    resolveRowTags,
  };
})(typeof window !== 'undefined' ? window : globalThis);
