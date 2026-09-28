#!/usr/bin/env python3
# Copyright (c) 2026 Vika Integrity contributors | MIT License
"""
Проверка расхождения: вендоренные копии против публичного источника.

Зачем: продукт (D:\\Vi\\vika_integrity) и моя живая система (D:\\Vi\\vika\\scripts)
содержат ОДНИ И ТЕ ЖЕ строки кода в двух местах. Это гарантированный путь к
тому, что я продам одно, а чинить буду другое - и никто не заметит, потому
что обе копии выглядят рабочими.

Этот скрипт - единственная защита от такой расхождающейся правды.
Простое сравнение хэшей: дешевле, чем разбираться потом.

    python tools/check_drift.py          # из vika_integrity
    python tools/check_drift.py --strict # ненулевой код возврата при расхождении
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

PAIRS = [
    (
        Path(r"D:\Vi\vika_integrity\vika_integrity\atomicio.py"),
        Path(r"D:\Vi\vika\scripts\_integrity_atomicio.py"),
    ),
    (
        Path(r"D:\Vi\vika_integrity\vika_integrity\locks.py"),
        Path(r"D:\Vi\vika\scripts\_integrity_locks.py"),
    ),
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    print("ПРОВЕРКА РАСХОЖДЕНИЯ: продукт <-> живая система")
    print("=" * 70)
    bad = 0
    for src, vend in PAIRS:
        if not src.exists():
            print("  ИСТОЧНИК НЕТ: %s" % src)
            bad += 1
            continue
        if not vend.exists():
            print("  ВЕНДОР НЕТ:   %s" % vend)
            bad += 1
            continue
        if sha(src) == sha(vend):
            print("  OK  %-18s == %s" % (src.name, sha(src)))
        else:
            print("  РАСХОЖДЕНИЕ  %s" % src.name)
            print("       продукт : %s  %s" % (sha(src), src))
            print("       вендор  : %s  %s" % (sha(vend), vend))
            print("       -> скопируй продукт в вендор и прогоняй тесты")
            bad += 1
    print("=" * 70)
    if bad:
        print("ИТОГ: расхождений %d" % bad)
        return 1 if args.strict else 1
    print("ИТОГ: копии идентичны")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
