#!/usr/bin/env python3
"""XMP metadata writing utilities for Project Kestrel.

Writes XMP sidecar files (.xmp) embedding star ratings, culling labels,
and analysis metadata (species, family, quality score) alongside image
files. Compatible with Adobe Lightroom, darktable, and Capture One.
"""

import os

# XMP namespace URIs
_KESTREL_NS = 'http://ns.projectkestrel.app/xmp/1.0/'
_NS_RDF = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'
_NS_XMP = 'http://ns.adobe.com/xap/1.0/'
_NS_DC = 'http://purl.org/dc/elements/1.1/'
_NS_LR = 'http://ns.adobe.com/lightroom/1.0/'

# Species/family values that indicate no meaningful detection
_EMPTY_LABELS = {'', 'unknown', 'no bird', 'n/a'}


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


def write_xmp_metadata(
    root_path: str,
    image_data,
    overwrite_external: bool = False,
    use_auto_labels: bool = False,
    fields: dict | None = None,
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

    Returns:
        { success, written, skipped_conflicts: [filenames], errors }
    """
    field_flags = _normalize_fields(fields)
    try:
        if not root_path or not os.path.isdir(root_path):
            return {'success': False, 'error': 'Invalid root path'}

        written = 0
        skipped_conflicts = []
        errors = []

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

        info(f'[metadata] write_xmp_metadata: written={written}, conflicts={len(skipped_conflicts)}, errors={len(errors)}')
        return {
            'success': True,
            'written': written,
            'skipped_conflicts': skipped_conflicts,
            'errors': errors,
        }

    except Exception as e:
        error(f'[metadata] write_xmp_metadata error: {e}')
        return {'success': False, 'error': str(e)}
