"""Write star ratings and analysis metadata to XMP sidecar files for Lightroom compatibility.

Lightroom reads XMP sidecar files (.xmp) placed alongside RAW files, and embedded
XMP in JPEG files. This module writes the xmp:Rating tag (1-5 stars) along with
custom Kestrel metadata (species, quality score, detail level) as Dublin Core
description fields that Lightroom can display.
"""

import logging
import os
from typing import Optional
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom

logger = logging.getLogger(__name__)

# XMP namespace URIs
NS_X = "adobe:ns:meta/"
NS_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
NS_XMP = "http://ns.adobe.com/xap/1.0/"
NS_DC = "http://purl.org/dc/elements/1.1/"
NS_XMP_RIGHTS = "http://ns.adobe.com/xap/1.0/rights/"
NS_LR = "http://ns.adobe.com/lightroom/1.0/"
NS_KESTREL = "http://ns.projectkestrel.com/1.0/"


def _build_xmp_packet(
    rating: int,
    label: str = "",
    species: str = "",
    family: str = "",
    quality_score: float = -1.0,
    detail_score: float = -1.0,
    sharpness_score: float = -1.0,
    camera_model: str = "",
) -> str:
    """Build a complete XMP packet with rating and Kestrel metadata."""
    # Use string building for precise XMP format that Adobe tools expect
    lines = []
    lines.append('<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>')
    lines.append('<x:xmpmeta xmlns:x="adobe:ns:meta/">')
    lines.append(f'  <rdf:RDF xmlns:rdf="{NS_RDF}">')
    lines.append(f'    <rdf:Description')
    lines.append(f'      xmlns:xmp="{NS_XMP}"')
    lines.append(f'      xmlns:dc="{NS_DC}"')
    lines.append(f'      xmlns:lr="{NS_LR}"')
    lines.append(f'      xmlns:kestrel="{NS_KESTREL}"')

    # Star rating (1-5, 0 means unrated) - this is what Lightroom reads
    lines.append(f'      xmp:Rating="{max(0, min(5, rating))}"')

    # Color label for Lightroom (maps rating to colour for visual sorting)
    if label:
        lines.append(f'      xmp:Label="{label}"')

    # Kestrel-specific metadata as custom XMP properties
    if species and species != "Unknown":
        lines.append(f'      kestrel:Species="{_xml_escape(species)}"')
    if family and family != "Unknown":
        lines.append(f'      kestrel:Family="{_xml_escape(family)}"')
    if quality_score >= 0:
        lines.append(f'      kestrel:QualityScore="{quality_score:.4f}"')
    if detail_score >= 0:
        lines.append(f'      kestrel:DetailScore="{detail_score:.4f}"')
    if sharpness_score >= 0:
        lines.append(f'      kestrel:SharpnessScore="{sharpness_score:.4f}"')

    lines.append('    >')

    # Dublin Core description - visible in Lightroom's metadata panel
    desc_parts = []
    if species and species != "Unknown":
        desc_parts.append(f"Species: {species}")
    if family and family != "Unknown":
        desc_parts.append(f"Family: {family}")
    if quality_score >= 0:
        desc_parts.append(f"Quality: {quality_score:.3f}")
    if detail_score >= 0:
        desc_parts.append(f"Detail: {detail_score:.3f}")
    if sharpness_score >= 0:
        desc_parts.append(f"Sharpness: {sharpness_score:.3f}")
    desc_parts.append(f"Rating: {'*' * rating}")

    if desc_parts:
        description = " | ".join(desc_parts)
        lines.append('      <dc:description>')
        lines.append('        <rdf:Alt>')
        lines.append(f'          <rdf:li xml:lang="x-default">{_xml_escape(description)}</rdf:li>')
        lines.append('        </rdf:Alt>')
        lines.append('      </dc:description>')

    # Hierarchical keywords for Lightroom keyword panel
    lines.append('      <dc:subject>')
    lines.append('        <rdf:Bag>')
    lines.append(f'          <rdf:li>Kestrel|Rating|{rating} Star</rdf:li>')
    if species and species != "Unknown":
        lines.append(f'          <rdf:li>Kestrel|Species|{_xml_escape(species)}</rdf:li>')
    if family and family != "Unknown":
        lines.append(f'          <rdf:li>Kestrel|Family|{_xml_escape(family)}</rdf:li>')
    if camera_model:
        lines.append(f'          <rdf:li>Kestrel|Camera|{_xml_escape(camera_model)}</rdf:li>')
    lines.append('        </rdf:Bag>')
    lines.append('      </dc:subject>')

    lines.append('    </rdf:Description>')
    lines.append('  </rdf:RDF>')
    lines.append('</x:xmpmeta>')
    lines.append('<?xpacket end="w"?>')

    return '\n'.join(lines)


def _xml_escape(text: str) -> str:
    """Escape special characters for XML attribute/text values."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _rating_to_label(rating: int) -> str:
    """Map star rating to Lightroom colour label for visual sorting."""
    labels = {
        5: "Green",   # Top-tier keepers
        4: "Yellow",  # Good shots
        3: "",        # Average - no label
        2: "Orange",  # Below average
        1: "Red",     # Poor quality
        0: "",        # Unrated
    }
    return labels.get(rating, "")


def write_xmp_sidecar(
    image_path: str,
    rating: int,
    species: str = "",
    family: str = "",
    quality_score: float = -1.0,
    detail_score: float = -1.0,
    sharpness_score: float = -1.0,
    camera_model: str = "",
) -> Optional[str]:
    """Write an XMP sidecar file alongside the source image.

    For RAW files (NEF, CR2, etc.), Lightroom expects a .xmp sidecar file
    with the same base name. For JPEGs, Lightroom also reads .xmp sidecars.

    Returns the path to the written XMP file, or None on failure.
    """
    try:
        base, _ = os.path.splitext(image_path)
        xmp_path = base + ".xmp"
        label = _rating_to_label(rating)

        xmp_content = _build_xmp_packet(
            rating=rating,
            label=label,
            species=species,
            family=family,
            quality_score=quality_score,
            detail_score=detail_score,
            sharpness_score=sharpness_score,
            camera_model=camera_model,
        )

        with open(xmp_path, "w", encoding="utf-8") as f:
            f.write(xmp_content)

        logger.info("XMP sidecar written: %s (rating=%d)", xmp_path, rating)
        return xmp_path
    except Exception as e:
        logger.error("Failed to write XMP sidecar for %s: %s", image_path, e)
        return None
