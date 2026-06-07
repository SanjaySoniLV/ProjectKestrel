"""Per-perch manifest schema mirroring `Perch Worker/src/perches/manifest_types.ts`.

The Worker authors the manifest at presign + commit time. The desktop builds
the same shape locally as the body of the presign request; the Worker echoes
it back as ``intendedManifest`` which the desktop re-sends at commit.

Hierarchy: Perch → Scenes → Exports → Crops. Crops nest under their parent
export, not top-level. ``r2_key`` is optional on assets — present only for
legacy entries that don't follow the standard ``p/{perchId}/{assetId}``
derivation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


CURRENT_MANIFEST_VERSION = 1


@dataclass
class PerchManifestBbox:
    x_min_norm: float
    y_min_norm: float
    x_max_norm: float
    y_max_norm: float


@dataclass
class PerchManifestCrop:
    asset_id: str
    filename: str
    content_type: str
    byte_length: int
    quality: Optional[float]
    exposure_correction: Optional[float]
    bbox: PerchManifestBbox
    r2_key: Optional[str] = None


@dataclass
class PerchManifestExport:
    asset_id: str
    filename: str
    content_type: str
    byte_length: int
    capture_time_ms: Optional[int]
    crops: List[PerchManifestCrop] = field(default_factory=list)
    r2_key: Optional[str] = None


@dataclass
class PerchManifestScene:
    scene_id: str
    kestrel_scene_id: Optional[str]
    capture_time_ms: Optional[int]
    species_list: Optional[List[str]]
    family_list: Optional[List[str]]
    user_tags_finalized: bool
    exports: List[PerchManifestExport] = field(default_factory=list)


@dataclass
class PerchManifest:
    manifest_version: int
    perch_id: str
    owner_id: str
    created_at_ms: int
    updated_at_ms: int
    scenes: List[PerchManifestScene] = field(default_factory=list)
