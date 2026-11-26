// Analyze dialog: time estimates and the per-folder plan.
//
// Pure functions only (no DOM, no settings reads), so they run under Node in
// tests/unit/test_analyze_estimate.py. analyze-dialog.js, event-wiring.js and
// cloud-compute.js call them through window.KestrelAnalyzeEstimate.
(function () {
  // Baseline seconds per photo on a fresh install. Once the queue worker has
  // recorded MIN_SAMPLED_FOR_LOCAL photos for a mode (perf_samples_gpu /
  // perf_samples_cpu in settings.json), the measured rate replaces these.
  const LOCAL_BASELINE_GPU = 4.0;
  const LOCAL_BASELINE_CPU = 10.0;
  const MIN_IMAGES_TO_ESTIMATE = 50;
  const MIN_SAMPLED_FOR_LOCAL = 100;

  /** Seconds per photo on this computer: { rate, source: 'local'|'baseline', sampledImgs }. */
  function localRate(samples, useGpu) {
    let imgs = 0, secs = 0;
    for (const s of Array.isArray(samples) ? samples : []) {
      const i = Number(s && s.imgs), t = Number(s && s.secs);
      if (i > 0 && t > 0) { imgs += i; secs += t; }
    }
    if (imgs >= MIN_SAMPLED_FOR_LOCAL) return { rate: secs / imgs, source: 'local', sampledImgs: imgs };
    return { rate: useGpu ? LOCAL_BASELINE_GPU : LOCAL_BASELINE_CPU, source: 'baseline', sampledImgs: imgs };
  }

  // Cloud Compute model. Nothing is analyzed until dispatchThreshold photos
  // (or the whole job) are uploaded; one container then starts after a cold
  // start, and another is spawned whenever the uploaded-but-unanalyzed backlog
  // reaches scaleThreshold, up to maxContainers, with a cooldown between spawns.
  // The worker serves these numbers with the upload speed test result; the
  // values here are used only when it doesn't.
  const CLOUD_FALLBACK_PIPELINE = {
    containerImagesPerSec: 1.0,
    maxContainers: 3,
    dispatchThreshold: 100,
    scaleThreshold: 200,
    spawnCooldownSeconds: 20,
    containerStartupSeconds: 22,
  };
  // The simulation models the ramp, so the margin only covers jitter.
  const CLOUD_MARGIN = 1.05;

  /** Served pipeline numbers merged field by field over the fallbacks; only finite positives are taken. */
  function cloudParams(served) {
    const out = Object.assign({}, CLOUD_FALLBACK_PIPELINE);
    if (served && typeof served === 'object') {
      for (const key of Object.keys(CLOUD_FALLBACK_PIPELINE)) {
        const v = Number(served[key]);
        if (Number.isFinite(v) && v > 0) out[key] = v;
      }
    }
    return out;
  }

  /** Seconds until the last photo is analyzed. uploadRate is photos/sec; Infinity means already uploaded. */
  function simulateCloudJob(totalImages, uploadRate, params) {
    const p = params || cloudParams(null);
    const n = Math.max(0, Math.floor(totalImages));
    if (n === 0) return 0;
    const instant = !(uploadRate > 0) || !Number.isFinite(uploadRate);
    // About 2,000 steps whatever the job size: fine enough for the spawn
    // cooldown on small jobs, coarse enough not to stall the UI on big ones.
    const horizon = instant
      ? n / (p.maxContainers * p.containerImagesPerSec)
      : n / uploadRate + n / p.containerImagesPerSec;
    const dt = Math.min(60, Math.max(0.25, (horizon + p.containerStartupSeconds) / 2000));
    const firstDispatchAt = Math.min(n, p.dispatchThreshold);
    let t = 0, analyzed = 0, spawned = 0, lastSpawnAt = -Infinity;
    const readyAt = [];
    for (let step = 0; step < 200000 && analyzed < n; step++) {
      const uploaded = instant ? n : Math.min(n, uploadRate * t);
      const backlog = uploaded - analyzed;
      if (spawned < p.maxContainers && (t - lastSpawnAt) >= p.spawnCooldownSeconds) {
        const trigger = spawned === 0 ? uploaded >= firstDispatchAt : backlog >= p.scaleThreshold;
        if (trigger) { readyAt.push(t + p.containerStartupSeconds); spawned++; lastSpawnAt = t; }
      }
      let active = 0;
      for (const r of readyAt) if (r <= t) active++;
      if (active > 0) {
        const capacity = active * p.containerImagesPerSec * dt;
        analyzed = Math.min(n, analyzed + Math.min(capacity, Math.max(0, backlog)));
      }
      t += dt;
    }
    return t;
  }

  /**
   * Cloud estimate for totalImages given the last speed test result (or null).
   * Returns { secs, analysisSecs, uploadSecs, regime, hasTest }; secs and regime
   * are null until a usable speed test has run. regime is 'upload' | 'balanced' | 'compute'.
   */
  function cloudEstimate(totalImages, speedTest) {
    const p = cloudParams(speedTest && speedTest.pipeline);
    const analysisSecs = simulateCloudJob(totalImages, Infinity, p);
    const none = { secs: null, analysisSecs, uploadSecs: null, regime: null };
    if (!speedTest || !(speedTest.mbps > 0) || !(speedTest.samples_uploaded > 0)) return { ...none, hasTest: false };
    const avgBytes = Number(speedTest.total_bytes || 0) / Number(speedTest.samples_uploaded);
    if (!(avgBytes > 0)) return { ...none, hasTest: true };
    const uploadRate = Number(speedTest.mbps) / (avgBytes / 1048576);
    const uploadSecs = totalImages / uploadRate;
    const secs = simulateCloudJob(totalImages, uploadRate, p) * CLOUD_MARGIN;
    // The dead band keeps the label from flickering when the two are close.
    let regime = 'balanced';
    if (analysisSecs > 0) {
      const ratio = uploadSecs / analysisSecs;
      if (ratio > 1.15) regime = 'upload';
      else if (ratio < 0.87) regime = 'compute';
    }
    return { secs, analysisSecs, uploadSecs, regime, hasTest: true };
  }

  /**
   * What Start will do with one checked folder.
   *   f: { total, processed, errored, outdated, upgradeMode: null|'upgrade'|'reanalyze' }
   *   opts: { reanalyzeUnlocked, retryErrored }
   * Returns { kind, reason, images, retriedErrored, skippedErrored }:
   *   kind 'upgrade'   in-place upgrade to the current analysis version (runs locally, adds no images)
   *        'reanalyze' erase .kestrel and analyze every photo; reason 'chosen' | 'outdated' | 'unlocked'
   *        'skip'      already fully analyzed; nothing happens
   *        'analyze'   analyze the photos not done yet
   */
  function planFolder(f, opts) {
    const total = Number(f.total) || 0;
    const processed = Number(f.processed) || 0;
    const errored = Number(f.errored) || 0;
    const o = opts || {};
    const base = { reason: null, images: 0, retriedErrored: 0, skippedErrored: 0 };
    if (f.upgradeMode === 'upgrade') return { ...base, kind: 'upgrade' };
    if (f.upgradeMode === 'reanalyze') return { ...base, kind: 'reanalyze', reason: 'chosen', images: total };
    if (f.outdated) return { ...base, kind: 'reanalyze', reason: 'outdated', images: total };
    if (total > 0 && processed >= total && errored === 0) {
      return o.reanalyzeUnlocked ? { ...base, kind: 'reanalyze', reason: 'unlocked', images: total } : { ...base, kind: 'skip' };
    }
    if (errored > 0 && o.retryErrored) return { ...base, kind: 'analyze', images: errored, retriedErrored: errored };
    if (errored > 0) return { ...base, kind: 'analyze', images: Math.max(0, total - processed - errored), skippedErrored: errored };
    return { ...base, kind: 'analyze', images: Math.max(0, total - processed) };
  }

  window.KestrelAnalyzeEstimate = {
    MIN_IMAGES_TO_ESTIMATE, MIN_SAMPLED_FOR_LOCAL,
    localRate, cloudParams, simulateCloudJob, cloudEstimate, planFolder,
  };
})();
