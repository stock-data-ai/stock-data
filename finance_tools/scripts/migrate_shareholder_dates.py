"""
One-time migration: convert shareholderDataHistory keys from YYYY-MM-DD → YYYYMMDD.

Fixes the format inconsistency introduced in commit 7934c1b65 (2026-04-16)
when the TDCC API switch accidentally changed the key format.
"""

import json
import glob
from pathlib import Path

FINANCIALS_DIR = Path(__file__).parent.parent.parent / "src/data/layer3/company-financials"


def migrate_file(fp: Path) -> bool:
    with open(fp, encoding="utf-8") as f:
        data = json.load(f)

    history = data.get("shareholderDataHistory")
    if not history:
        return False

    new_history = {}
    changed = False
    for key, val in history.items():
        if len(key) == 10 and key[4] == '-' and key[7] == '-':
            new_key = key.replace('-', '')
            new_history[new_key] = val
            changed = True
        else:
            new_history[key] = val

    if not changed:
        return False

    data["shareholderDataHistory"] = new_history
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return True


def run():
    files = sorted(FINANCIALS_DIR.glob("*.json"))
    migrated = 0
    for fp in files:
        try:
            if migrate_file(fp):
                migrated += 1
        except Exception as e:
            print(f"ERROR {fp.name}: {e}")

    print(f"Migrated {migrated}/{len(files)} files.")


if __name__ == "__main__":
    run()
