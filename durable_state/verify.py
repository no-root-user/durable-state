# Copyright (c) 2026 Durable State contributors | MIT License
"""
durable_state.verify - prove your files arrived intact and readable.

Two independent questions, one command:
  1. Did the file change since the manifest was written?  (sha256)
  2. Is it the encoding I think it is?                   (strict UTF-8, no BOM)

Binary files (.mp4, .zip, images) are checked by hash only - judging a video
by its text encoding produces nonsense.

Run:  python -m durable_state.verify <dir>
Exit: 0 = intact, 1 = damaged.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

__all__ = ["sha256_of", "verify_dir", "TEXT_SUFFIXES"]

TEXT_SUFFIXES = {".py", ".md", ".json", ".txt", ".cfg", ".ini", ".yaml", ".yml", ".sha256", ".toml"}
CHUNK = 65536
BOM = b"\xef\xbb\xbf"


def sha256_of(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def read_manifest(manifest: str | Path) -> list[tuple[str, str]]:
    """Parse a sha256sum-style manifest into (expected_hash, relative_path)."""
    out = []
    for line in Path(manifest).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, rel = line.partition(" ")
        if not rel.strip():
            continue
        out.append((digest.strip(), rel.lstrip("*").strip()))
    return out


def verify_dir(base: str | Path, manifest: str | Path = "checksums.sha256") -> dict:
    """
    Verify every file listed in the manifest.

    Returns a report dict. 'ok' is True only if nothing at all is wrong.
    """
    base = Path(base)
    manifest = Path(manifest)
    if not manifest.is_absolute():
        manifest = base / manifest

    report = {
        "ok": False,
        "checked": 0,
        "missing": [],
        "hash_mismatch": [],
        "not_utf8": [],
        "has_bom": [],
        "unlisted": [],
        "manifest": str(manifest),
    }
    if not manifest.exists():
        report["error"] = "manifest not found: %s" % manifest
        return report

    listed = set()
    for expected, rel in read_manifest(manifest):
        listed.add(rel)
        target = base / rel
        if not target.exists():
            report["missing"].append(rel)
            continue
        if sha256_of(target) != expected:
            report["hash_mismatch"].append(rel)
        if target.suffix.lower() in TEXT_SUFFIXES:
            raw = target.read_bytes()
            if raw.startswith(BOM):
                report["has_bom"].append(rel)
                try:
                    raw[len(BOM):].decode("utf-8")
                except UnicodeDecodeError:
                    report["not_utf8"].append(rel)
            else:
                try:
                    raw.decode("utf-8")
                except UnicodeDecodeError as e:
                    report["not_utf8"].append("%s (%s)" % (rel, e.reason))
        report["checked"] += 1

    for p in sorted(base.rglob("*")):
        if p.is_file() and p.relative_to(base).as_posix() not in listed:
            rel = p.relative_to(base).as_posix()
            if rel != manifest.relative_to(base).as_posix() and "__pycache__" not in rel:
                report["unlisted"].append(rel)

    report["ok"] = not (
        report["missing"] or report["hash_mismatch"] or report["not_utf8"] or report["has_bom"]
    )
    return report


def format_report(report: dict) -> str:
    if "error" in report:
        return "ERROR: %s" % report["error"]
    lines = [
        "INTEGRITY REPORT",
        "  files checked : %d" % report["checked"],
        "  hash mismatch : %d %s" % (len(report["hash_mismatch"]), report["hash_mismatch"] or ""),
        "  not utf-8     : %d %s" % (len(report["not_utf8"]), report["not_utf8"] or ""),
        "  has BOM       : %d %s" % (len(report["has_bom"]), report["has_bom"] or ""),
        "  missing       : %d %s" % (len(report["missing"]), report["missing"] or ""),
    ]
    if report["unlisted"]:
        lines.append("  not in manifest (informational): %s" % report["unlisted"])
    lines.append("")
    lines.append("  INTACT" if report["ok"] else "  DAMAGED")
    return "\n".join(lines)


USAGE = """usage: python -m durable_state.verify [DIR]

Check every file in DIR against checksums.sha256, and check that the text files
are strict UTF-8 without a BOM.

  DIR   directory holding the manifest. Default: the current directory.

exit 0 = intact, exit 1 = damaged or unreadable."""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("-h", "--help") for a in argv):
        print(USAGE)
        return 0
    base = Path(argv[0]) if argv else Path.cwd()
    report = verify_dir(base)
    print(format_report(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
