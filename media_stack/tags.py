"""Idempotency tags in MKV global tags: CONSOLIDATED_SUBS=v<n> and NORMALIZED_AUDIO=v<n>."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


def set_global_tag(path: Path, name: str, value: str) -> bool:
    """Set `<name>=<value>` as a global tag, preserving the others (mkvpropedit `--tags`
    replaces the whole block). Failure is non-fatal: idempotency falls back to the state file."""
    r = subprocess.run(["mkvextract", str(path), "tags", "-"],
                       capture_output=True, text=True, timeout=60)
    existing_xml = r.stdout if r.returncode == 0 else ""

    # Keep global-target (TargetTypeValue=50 or absent) entries except a prior `name`.
    rows: list[str] = []
    if existing_xml.strip():
        for tag_match in re.finditer(r"<Tag>(.*?)</Tag>", existing_xml, re.DOTALL):
            tag_body = tag_match.group(1)
            tt = re.search(r"<TargetTypeValue>(\d+)</TargetTypeValue>", tag_body)
            if tt and tt.group(1) != "50":
                continue
            for sm in re.finditer(r"<Simple>.*?</Simple>", tag_body, re.DOTALL):
                block = sm.group(0)
                existing_name = re.search(r"<Name>([^<]+)</Name>", block)
                if existing_name and existing_name.group(1).upper() == name.upper():
                    continue
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
        xml_path.unlink(missing_ok=True)


def set_consolidated_tag(path: Path, version: int) -> bool:
    """Stamp CONSOLIDATED_SUBS."""
    return set_global_tag(path, "CONSOLIDATED_SUBS", f"v{version}")


def set_normalized_tag(path: Path, version: int) -> bool:
    """Stamp NORMALIZED_AUDIO."""
    return set_global_tag(path, "NORMALIZED_AUDIO", f"v{version}")
