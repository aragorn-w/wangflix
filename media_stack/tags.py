"""Matroska global-tag manipulation via mkvpropedit + mkvextract.

The pipeline writes two idempotency tags into MKV files:
  - CONSOLIDATED_SUBS=v<PIPELINE_VERSION>  (consolidate-subs.py)
  - NORMALIZED_AUDIO=v<audio_version>      (normalize-audio.py)

Reading them back via ffprobe gates re-processing.  This module
encapsulates the two writers because the WHOLE-replacement semantics
of mkvpropedit's `--tags` are subtle — set_global_tag() has to read
existing tags, merge in the new one, and write back; otherwise
setting NORMALIZED_AUDIO would wipe CONSOLIDATED_SUBS and vice versa.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


def set_global_tag(path: Path, name: str, value: str) -> bool:
    """Set `<name>=<value>` as a global Matroska tag on `path`,
    preserving any other existing global tags.

    mkvpropedit's `--tags global:FILE` REPLACES the entire global tag
    block.  So we mkvextract the existing tags first, drop any prior
    entry for `name`, append our new row, write the merged XML back.

    Returns True on success.  Failure is non-fatal for the calling
    pipeline (idempotency falls back to the state file or to the next
    sweep tick).
    """
    # 1. Extract current global tags.
    r = subprocess.run(["mkvextract", str(path), "tags", "-"],
                       capture_output=True, text=True, timeout=60)
    existing_xml = r.stdout if r.returncode == 0 else ""

    # 2. Build merged XML.  Drop any prior `name` entries from the
    #    global-target (TargetTypeValue=50, or absent) tag block;
    #    keep everything else.
    rows: list[str] = []
    if existing_xml.strip():
        for tag_match in re.finditer(r"<Tag>(.*?)</Tag>", existing_xml, re.DOTALL):
            tag_body = tag_match.group(1)
            tt = re.search(r"<TargetTypeValue>(\d+)</TargetTypeValue>", tag_body)
            # Only process tags targeted at the file as a whole.
            if tt and tt.group(1) != "50":
                continue
            for sm in re.finditer(r"<Simple>.*?</Simple>", tag_body, re.DOTALL):
                block = sm.group(0)
                existing_name = re.search(r"<Name>([^<]+)</Name>", block)
                if existing_name and existing_name.group(1).upper() == name.upper():
                    continue  # drop stale version
                rows.append(block)

    rows.append(
        "<Simple>\n"
        f"      <Name>{name}</Name>\n"
        f"      <String>{value}</String>\n"
        "    </Simple>"
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Tags>\n'
        '  <Tag>\n'
        '    <Targets><TargetTypeValue>50</TargetTypeValue></Targets>\n'
        '    ' + "\n    ".join(rows) + "\n"
        '  </Tag>\n'
        '</Tags>\n'
    )
    xml_path = path.with_suffix(path.suffix + ".tags.xml")
    xml_path.write_text(xml)
    try:
        r = subprocess.run(
            ["mkvpropedit", str(path), "--tags", f"global:{xml_path}"],
            capture_output=True, text=True, timeout=60,
        )
        return r.returncode == 0
    except Exception:
        return False
    finally:
        try:
            xml_path.unlink()
        except FileNotFoundError:
            pass


def set_consolidated_tag(path: Path, version: int) -> bool:
    """Convenience: stamp `CONSOLIDATED_SUBS=v<version>`."""
    return set_global_tag(path, "CONSOLIDATED_SUBS", f"v{version}")


def set_normalized_tag(path: Path, version: int) -> bool:
    """Convenience: stamp `NORMALIZED_AUDIO=v<version>`."""
    return set_global_tag(path, "NORMALIZED_AUDIO", f"v{version}")
