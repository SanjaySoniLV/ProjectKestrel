#!/usr/bin/env python3
"""XMP metadata writing utilities for Project Kestrel.

Writes XMP sidecar files (.xmp) embedding star ratings, culling labels,
and analysis metadata (species, family, quality score) alongside image
files. Compatible with Adobe Lightroom, darktable, and Capture One.

For JPEG files, an optional ``embed_jpeg`` mode additionally writes the same
XMP fields *directly into the JPEG's APP1 XMP segment* (via exiv2/pyexiv2).
Adobe Lightroom only reads .xmp sidecars for RAW files — for JPEGs it expects
the XMP to live inside the file — so embedding is the only way ratings/labels
Kestrel writes become visible to Lightroom on a JPEG. This modifies the
original file in place (pixel data is left untouched; only the metadata
segment is rewritten), so it is strictly opt-in.
"""

import os

# XMP namespace URIs
_KESTREL_NS = 'http://ns.projectkestrel.app/xmp/1.0/'
_KESTREL_NS_PREFIX = 'kestrel'
_NS_RDF = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'
_NS_XMP = 'http://ns.adobe.com/xap/1.0/'
_NS_DC = 'http://purl.org/dc/elements/1.1/'
_NS_LR = 'http://ns.adobe.com/lightroom/1.0/'

# Species/family values that indicate no meaningful detection
_EMPTY_LABELS = {'', 'unknown', 'no bird', 'n/a'}

# File extensions treated as JPEG for the embed-in-original path.
_JPEG_EXTS = {'.jpg', '.jpeg'}


def _safe_sidecar_path(root: str, filename: str) -> str | None:
    """Resolve ``<root>/<filename>`` to an absolute path that is guaranteed to
    live directly inside ``root`` and return it, or ``None`` if ``filename``
    is unsafe.

    Policy (FINDING-02): ``filename`` MUST already be a bare basename. Any
    directory component (``../``, ``..\\``, absolute path, drive letter, UNC
    prefix, leading separator, NUL byte, Windows ``altsep``) causes the
    entry to be rejected outright — we deliberately do not silently reduce
    ``../../etc/evil`` to ``evil`` and write it into the root, because
    callers that pass non-bare names are either buggy or attacking, and in
    either case the user's data is better served by an error than by a
    surprise write.
    """
    if not isinstance(filename, str):
        return None
    name = filename.strip()
    if not name:
        return None
    # Control chars / NUL byte — reject before any further interpretation.
    if '\x00' in name:
        return None
    # Reject absolute paths, drive letters, and UNC prefixes up front.
    if os.path.isabs(name):
        return None
    # ``ntpath.splitdrive`` catches Windows drive prefixes like ``C:foo``
    # even on POSIX builds, because the attacker gets to choose the input.
    import ntpath as _nt
    drive, _rest = _nt.splitdrive(name)
    if drive:
        return None
    # Reject any path separator (POSIX ``/``, Windows ``\\``) and the
    # traversal pseudo-names. These are not legal characters in a sidecar
    # basename and their presence indicates the caller is trying to
    # redirect the write elsewhere.
    if '/' in name or '\\' in name:
        return None
    if name in ('.', '..'):
        return None
    # os.sep / os.altsep are already covered above but keep the belt-and-
    # suspenders check for future-proofing if someone ever adds new seps.
    if os.sep in name or (os.altsep and os.altsep in name):
        return None
    try:
        root_real = os.path.realpath(root)
        candidate = os.path.realpath(os.path.join(root_real, name))
    except (OSError, ValueError):
        return None
    try:
        if os.path.commonpath([candidate, root_real]) != root_real:
            return None
    except ValueError:
        # Different drives (Windows) — not under root.
        return None
    # Defence against ``realpath`` resolving a symlink back up out of root:
    # require the final component to match the name we validated.
    if os.path.basename(candidate) != name:
        return None
    return candidate

# Default field selection for XMP writes — every field on. The frontend can
# override individual flags via the `fields` parameter to write_xmp_metadata().
_DEFAULT_FIELDS = {
    'rating': True,    # xmp:Rating star rating (0–5)
    'label': True,     # xmp:Label color label (Green/Red for accept/reject)
    'species': True,   # kestrel:Species + dc:subject Species keyword
    'family': True,    # kestrel:Family + dc:subject Family keyword
    'quality': True,   # kestrel:QualityScore + Quality summary in description
}


from settings_utils import debug, info, warn, error


def _xml_escape(text: str) -> str:
    """Escape special characters for XML attribute and text values."""
    return (
        text.replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
        .replace("'", '&apos;')
    )


def _is_meaningful(value: str) -> bool:
    """Return True if a string label is a real detection (not blank/unknown)."""
    return bool(value) and value.lower() not in _EMPTY_LABELS


def _normalize_fields(fields) -> dict:
    """Coerce a user-supplied fields dict to a complete bool-valued dict.

    Unknown keys are ignored; missing keys fall back to the default (True),
    so omitting `fields` entirely preserves the legacy "write everything"
    behaviour.
    """
    out = dict(_DEFAULT_FIELDS)
    if isinstance(fields, dict):
        for k, v in fields.items():
            if k in out:
                out[k] = bool(v)
    return out


def _build_xmp_packet(
    rating: int,
    label: str,
    cull_status: str,
    filename: str,
    species: str = '',
    family: str = '',
    quality_score: float = -1.0,
    fields: dict | None = None,
) -> str:
    """Build a complete XMP packet string with rating, label, and Kestrel metadata.

    The `fields` dict (see `_DEFAULT_FIELDS`) controls which sections appear
    in the packet. Disabled fields are omitted from xmp:* attributes,
    kestrel:* attributes, dc:description, and dc:subject keywords.

    Note: `kestrel:CullStatus` and `kestrel:SourceFile` are always written —
    they are bookkeeping needed to detect Kestrel-authored sidecars on the
    next write and are too small to bother gating.
    """
    f = _normalize_fields(fields)
    rating = max(0, min(5, rating))
    write_rating = f['rating']
    has_species = f['species'] and _is_meaningful(species)
    has_family = f['family'] and _is_meaningful(family)
    has_quality = f['quality'] and quality_score >= 0.0
    write_label = f['label'] and bool(label)

    lines = [
        '<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>',
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">',
        f'  <rdf:RDF xmlns:rdf="{_NS_RDF}">',
        '    <rdf:Description rdf:about=""',
        f'      xmlns:xmp="{_NS_XMP}"',
        f'      xmlns:dc="{_NS_DC}"',
        f'      xmlns:lr="{_NS_LR}"',
        f'      xmlns:kestrel="{_KESTREL_NS}"',
    ]
    if write_rating:
        lines.append(f'      xmp:Rating="{rating}"')
    if write_label:
        lines.append(f'      xmp:Label="{label}"')

    # Kestrel-specific attributes — CullStatus + SourceFile are always written
    # (used to identify Kestrel-authored sidecars on subsequent writes).
    lines.append(f'      kestrel:CullStatus="{_xml_escape(cull_status)}"')
    lines.append(f'      kestrel:SourceFile="{_xml_escape(filename)}"')
    if has_species:
        lines.append(f'      kestrel:Species="{_xml_escape(species)}"')
    if has_family:
        lines.append(f'      kestrel:Family="{_xml_escape(family)}"')
    if has_quality:
        lines.append(f'      kestrel:QualityScore="{quality_score:.4f}"')

    lines.append('    >')

    # dc:description — human-readable summary visible in Lightroom's metadata panel
    desc_parts = []
    if has_species:
        desc_parts.append(f'Species: {species}')
    if has_family:
        desc_parts.append(f'Family: {family}')
    if has_quality:
        desc_parts.append(f'Quality: {quality_score:.3f}')
    if write_rating:
        desc_parts.append(f'Rating: {"*" * rating}')

    if desc_parts:
        description = ' | '.join(desc_parts)
        lines += [
            '      <dc:description>',
            '        <rdf:Alt>',
            f'          <rdf:li xml:lang="x-default">{_xml_escape(description)}</rdf:li>',
            '        </rdf:Alt>',
            '      </dc:description>',
        ]

    # dc:subject — hierarchical keywords for Lightroom keyword panel
    subject_lines = []
    if write_rating:
        subject_lines.append(f'          <rdf:li>Kestrel|Rating|{rating} Star</rdf:li>')
    if has_species:
        subject_lines.append(f'          <rdf:li>Kestrel|Species|{_xml_escape(species)}</rdf:li>')
    if has_family:
        subject_lines.append(f'          <rdf:li>Kestrel|Family|{_xml_escape(family)}</rdf:li>')
    if subject_lines:
        lines += [
            '      <dc:subject>',
            '        <rdf:Bag>',
            *subject_lines,
            '        </rdf:Bag>',
            '      </dc:subject>',
        ]

    lines += [
        '    </rdf:Description>',
        '  </rdf:RDF>',
        '</x:xmpmeta>',
        '<?xpacket end="w"?>',
    ]

    return '\n'.join(lines)


def _is_kestrel_xmp(path: str) -> bool:
    """Return True if the XMP file at ``path`` was written by Kestrel."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read(4096)  # namespace declarations are near the top
        return _KESTREL_NS in content
    except Exception:
        return False


def _is_jpeg(filename: str) -> bool:
    """Return True if ``filename`` has a JPEG extension (case-insensitive)."""
    return os.path.splitext(filename)[1].lower() in _JPEG_EXTS


# pyexiv2 is a heavyweight native dependency (it bundles the exiv2 C++
# library). Import it lazily so the sidecar path — and the whole module —
# keeps working in environments where it is not installed. The import result
# is cached: a successful module, or the exception that explains why it is
# unavailable.
_pyexiv2_mod = None
_pyexiv2_err: Exception | None = None
_kestrel_ns_registered = False


def _load_pyexiv2():
    """Return the imported ``pyexiv2`` module, or raise a clear RuntimeError."""
    global _pyexiv2_mod, _pyexiv2_err, _kestrel_ns_registered
    if _pyexiv2_mod is not None:
        return _pyexiv2_mod
    if _pyexiv2_err is not None:
        raise RuntimeError(f'pyexiv2 unavailable: {_pyexiv2_err}')
    try:
        import pyexiv2  # type: ignore
    except Exception as e:  # pragma: no cover - import environment specific
        _pyexiv2_err = e
        raise RuntimeError(f'pyexiv2 unavailable: {e}')
    _pyexiv2_mod = pyexiv2
    # Register the Kestrel XMP namespace once so ``kestrel:*`` properties
    # serialise with our prefix. Safe to attempt once; ignore if the runtime
    # already knows it.
    if not _kestrel_ns_registered:
        try:
            pyexiv2.registerNs(_KESTREL_NS, _KESTREL_NS_PREFIX)
        except Exception:
            pass
        _kestrel_ns_registered = True
    return pyexiv2


def _build_embed_xmp_dict(
    rating: int,
    label: str,
    cull_status: str,
    filename: str,
    species: str,
    family: str,
    quality_score: float,
    fields: dict,
) -> dict:
    """Build the ``{Xmp.* : value}`` dict written into a JPEG's XMP segment.

    Mirrors the field-gating of :func:`_build_xmp_packet` exactly so the
    embedded metadata matches the sidecar Kestrel would write for the same
    image. Only the selected fields are returned; ``kestrel:CullStatus`` and
    ``kestrel:SourceFile`` are always included as the Kestrel-authored marker.
    """
    f = _normalize_fields(fields)
    rating = max(0, min(5, rating))
    write_rating = f['rating']
    has_species = f['species'] and _is_meaningful(species)
    has_family = f['family'] and _is_meaningful(family)
    has_quality = f['quality'] and quality_score >= 0.0
    write_label = f['label'] and bool(label)

    xd: dict = {}
    if write_rating:
        xd['Xmp.xmp.Rating'] = str(rating)
    if write_label:
        xd['Xmp.xmp.Label'] = label
    xd['Xmp.kestrel.CullStatus'] = cull_status
    xd['Xmp.kestrel.SourceFile'] = filename
    if has_species:
        xd['Xmp.kestrel.Species'] = species
    if has_family:
        xd['Xmp.kestrel.Family'] = family
    if has_quality:
        xd['Xmp.kestrel.QualityScore'] = f'{quality_score:.4f}'

    subject = []
    if write_rating:
        subject.append(f'Kestrel|Rating|{rating} Star')
    if has_species:
        subject.append(f'Kestrel|Species|{species}')
    if has_family:
        subject.append(f'Kestrel|Family|{family}')
    if subject:
        xd['Xmp.dc.subject'] = subject

    desc_parts = []
    if has_species:
        desc_parts.append(f'Species: {species}')
    if has_family:
        desc_parts.append(f'Family: {family}')
    if has_quality:
        desc_parts.append(f'Quality: {quality_score:.3f}')
    if write_rating:
        desc_parts.append(f'Rating: {"*" * rating}')
    if desc_parts:
        # pyexiv2 renders this as a LangAlt (dc:description is a lang-alt).
        xd['Xmp.dc.description'] = f'lang="x-default" {" | ".join(desc_parts)}'

    return xd


def _embed_xmp_in_jpeg(
    image_path: str,
    rating: int,
    label: str,
    cull_status: str,
    filename: str,
    species: str,
    family: str,
    quality_score: float,
    fields: dict,
) -> None:
    """Embed Kestrel XMP fields directly into a JPEG's APP1 XMP segment.

    Uses exiv2 (via pyexiv2), which rewrites *only* the metadata segment and
    leaves the compressed image data byte-for-byte identical. Existing XMP in
    the file is preserved (merge, not clobber) — only the Kestrel-owned
    properties are set/updated. Raises on any failure so the caller can record
    it per-file without aborting the batch.
    """
    pyexiv2 = _load_pyexiv2()
    xd = _build_embed_xmp_dict(
        rating, label, cull_status, filename, species, family, quality_score, fields
    )
    img = pyexiv2.Image(image_path)
    try:
        img.modify_xmp(xd)
    finally:
        img.close()


def write_xmp_metadata(
    root_path: str,
    image_data,
    overwrite_external: bool = False,
    use_auto_labels: bool = False,
    fields: dict | None = None,
    embed_jpeg: bool = False,
):
    """Write XMP sidecar files for each image, embedding star rating, culling
    label, and analysis metadata (species, family, quality score).

    Each entry in ``image_data`` is expected to be a dict with:
        filename       – bare filename (e.g. "IMG_0001.jpg")
        rating         – integer 0-5
        culled         – "accept" or "reject"
        culled_origin  – "auto", "manual", or "verified" (optional)
        species        – detected species name (optional)
        family         – detected family name (optional)
        quality        – raw quality score float 0.0–1.0 (optional)

    XMP sidecar files are written as ``<basename>.xmp`` alongside the
    original in ``root_path``.

    Safety rules:
      - If a ``.xmp`` file already exists and was written by Kestrel
        (detected by the presence of the Kestrel namespace URI), it is
        safe to overwrite and will always be updated.
      - If a ``.xmp`` file already exists but was written by external
        software (Lightroom, darktable, Capture One, etc.) AND
        ``overwrite_external`` is False, the file is skipped and its
        filename is added to ``skipped_conflicts`` in the response so the
        caller can ask the user for confirmation.
      - If ``overwrite_external`` is True, external XMP files are also
        overwritten.

    Args:
        root_path: Path to images.
        image_data: List of dicts.
        overwrite_external: Whether to overwrite non-Kestrel XMPs.
        use_auto_labels: If True, write Red/Green color labels for AI-generated ('auto') culls.
                         Labels are always written for user culls ('manual' and 'verified').
        fields: Optional dict selecting which fields to write. Recognised keys
                are ``rating``, ``label``, ``species``, ``family``, ``quality``;
                each value is coerced to bool. Missing keys default to True so
                callers that omit the argument keep legacy "write everything"
                behaviour.
        embed_jpeg: If True, additionally embed the same XMP fields directly
                into each JPEG's own XMP segment (in place, via exiv2). This
                modifies the original JPEG files and is what makes Kestrel's
                ratings visible to Adobe Lightroom, which ignores .xmp sidecars
                for JPEGs. Non-JPEG files are unaffected. Pixel data is left
                untouched. Off by default.

    Returns:
        { success, written, skipped_conflicts: [filenames], errors,
          embedded, embed_errors: [strings] }

        ``written`` counts sidecar files; ``embedded`` counts JPEGs whose
        in-file XMP was updated. ``embed_errors`` holds per-file embed
        failures (e.g. pyexiv2 unavailable) — these never abort the sidecar
        write.
    """
    field_flags = _normalize_fields(fields)
    try:
        if not root_path or not os.path.isdir(root_path):
            return {'success': False, 'error': 'Invalid root path'}

        written = 0
        skipped_conflicts = []
        errors = []
        embedded = 0
        embed_errors = []

        for entry in (image_data or []):
            try:
                filename = str(entry.get('filename', '')).strip()
                if not filename:
                    errors.append('(blank filename): skipped')
                    continue

                rating = int(entry.get('rating', 0) or 0)
                rating = max(0, min(5, rating))

                cull_status = str(entry.get('culled', '')).lower()
                origin = str(entry.get('culled_origin', '')).lower()
                
                label = ''
                if use_auto_labels or origin in ('manual', 'verified'):
                    if cull_status == 'accept':
                        label = 'Green'
                    elif cull_status == 'reject':
                        label = 'Red'

                species = str(entry.get('species', '') or '').strip()
                family = str(entry.get('family', '') or '').strip()

                quality_raw = entry.get('quality', None)
                try:
                    quality_score = float(quality_raw) if quality_raw is not None else -1.0
                except (TypeError, ValueError):
                    quality_score = -1.0

                # Jail sidecar writes to ``root_path``. A crafted filename
                # with traversal segments (``../sensitive``) or an absolute
                # path would otherwise let the caller write .xmp files
                # anywhere on disk. See FINDING-02.
                resolved_image = _safe_sidecar_path(root_path, filename)
                if resolved_image is None:
                    errors.append(f'{filename}: rejected (unsafe filename)')
                    warn(f'[metadata][security] write_xmp_metadata rejected unsafe filename: {filename!r}')
                    continue
                base, _ext = os.path.splitext(resolved_image)
                xmp_path = base + '.xmp'
                xmp_filename = os.path.basename(xmp_path)

                # Embed XMP directly into JPEG originals when requested. This is
                # independent of the sidecar write below — it merges into the
                # file's own XMP segment (Lightroom ignores .xmp sidecars for
                # JPEGs) and only runs when the real image file is present.
                # Failures are recorded per-file and never abort the batch or
                # the sidecar write.
                if embed_jpeg and _is_jpeg(filename) and os.path.isfile(resolved_image):
                    try:
                        _embed_xmp_in_jpeg(
                            resolved_image,
                            rating=rating,
                            label=label,
                            cull_status=cull_status,
                            filename=filename,
                            species=species,
                            family=family,
                            quality_score=quality_score,
                            fields=field_flags,
                        )
                        embedded += 1
                        info(f'[metadata] embed_jpeg: embedded XMP into {filename}')
                    except Exception as embed_err:
                        embed_errors.append(f'{filename}: {embed_err}')
                        warn(f'[metadata] embed_jpeg: failed for {filename}: {embed_err}')

                # Safety check: if XMP already exists, verify origin
                if os.path.exists(xmp_path):
                    if not _is_kestrel_xmp(xmp_path):
                        if not overwrite_external:
                            skipped_conflicts.append(xmp_filename)
                            warn(f'[metadata] write_xmp: skipping external XMP {xmp_path}')
                            continue
                        else:
                            info(f'[metadata] write_xmp: overwriting external XMP {xmp_path} (user confirmed)')

                xmp_content = _build_xmp_packet(
                    rating=rating,
                    label=label,
                    cull_status=cull_status,
                    filename=filename,
                    species=species,
                    family=family,
                    quality_score=quality_score,
                    fields=field_flags,
                )

                with open(xmp_path, 'w', encoding='utf-8') as f:
                    f.write(xmp_content)

                written += 1
                info(f'[metadata] write_xmp: wrote {xmp_path}')

            except Exception as entry_err:
                errors.append(f'{entry.get("filename", "?")}: {entry_err}')

        info(
            f'[metadata] write_xmp_metadata: written={written}, '
            f'conflicts={len(skipped_conflicts)}, errors={len(errors)}, '
            f'embedded={embedded}, embed_errors={len(embed_errors)}'
        )
        return {
            'success': True,
            'written': written,
            'skipped_conflicts': skipped_conflicts,
            'errors': errors,
            'embedded': embedded,
            'embed_errors': embed_errors,
        }

    except Exception as e:
        error(f'[metadata] write_xmp_metadata error: {e}')
        return {'success': False, 'error': str(e)}
