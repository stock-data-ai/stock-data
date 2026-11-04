"""
compare_etf_fees.py

ETF 管理費人工對照：拿使用者每月自己整理的「台股ETF管理費排行」CSV，跟 src/data/etf/{code}.json 的
managementFee（保管＋管理，%）逐檔比對。預設只印差異，不寫檔；確認後用 --apply 指定代號才寫入。

為什麼人工：管理費沒有公開 API（證交所、櫃買、集保、FinMind 都沒有）。2026-11-04 起不再爬玩股網；
本機排程每月第一個週五寄信提醒整理一份 CSV 來對照。

CSV 格式（試算表匯出）：第 1 欄代碼、第 2 欄名稱、第 10 欄「內扣費用(保管+管理)」；表頭可重複出現。
試算表會吃掉代碼開頭的 0（0050 → 50、00878 → 878），這裡依站上已有的代號補回。

注意：CSV 對主動式 ETF 常只列保管費（例 00981A 寫 0.1，實際含經理費約 1.0），差異大時先查公開說明書。

用法：
    uv run python etf_Crawler/compare_etf_fees.py ~/Downloads/台股ETF管理費排行11月.csv
    uv run python etf_Crawler/compare_etf_fees.py 檔案.csv --apply 00731,00770     # 只把這幾檔改成 CSV 的值
"""

import csv
import json
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

REPO_ROOT = Path(__file__).parent.parent
ETF_DIR = REPO_ROOT / "src/data/etf"
TOLERANCE = 0.005
FEE_COL = 9


def load_site() -> Dict[str, Dict]:
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in ETF_DIR.glob("*.json") if p.stem != "index"}


def normalize_code(raw: str, known) -> str:
    """試算表吃掉的開頭 0 補回：依序試 0～3 個 0，對得上站上代號就用它。"""
    for k in range(4):
        cand = "0" * k + raw
        if cand in known:
            return cand
    return raw


def load_csv(path: Path, known) -> Dict[str, Tuple[str, Optional[float]]]:
    out: Dict[str, Tuple[str, Optional[float]]] = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if len(row) <= FEE_COL or not row[0].strip() or row[0].strip() == "代碼":
                continue
            try:
                fee: Optional[float] = float(row[FEE_COL])
            except ValueError:
                fee = None
            out[normalize_code(row[0].strip(), known)] = (row[1].strip(), fee)
    return out


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(2)
    apply = set()
    if "--apply" in sys.argv:
        i = sys.argv.index("--apply")
        apply = {c.strip() for c in sys.argv[i + 1].split(",") if c.strip()} if i + 1 < len(sys.argv) else set()
        args = [a for a in args if a != sys.argv[i + 1]] if i + 1 < len(sys.argv) else args
    site = load_site()
    sheet = load_csv(Path(args[0]).expanduser(), site.keys())

    diffs, missing_in_csv, empty_site = [], [], []
    for code, data in sorted(site.items()):
        ours = data.get("managementFee")
        if code not in sheet:
            missing_in_csv.append(code)
            continue
        name, theirs = sheet[code]
        if theirs is None:
            continue
        if not isinstance(ours, (int, float)):
            empty_site.append((code, data.get("name") or name, theirs))
        elif abs(ours - theirs) > TOLERANCE:
            diffs.append((code, data.get("name") or name, ours, theirs))

    print(f"站上 {len(site)} 檔、CSV {len(sheet)} 檔；不一致 {len(diffs)} 檔、站上空白 {len(empty_site)} 檔、CSV 沒有 {len(missing_in_csv)} 檔\n")
    if diffs:
        print("【不一致】代碼\t名稱\t站上\tCSV")
        for code, name, ours, theirs in diffs:
            print(f"{code}\t{name}\t{ours}\t{theirs}")
    if empty_site:
        print("\n【站上空白，CSV 有值】代碼\t名稱\tCSV")
        for code, name, theirs in empty_site:
            print(f"{code}\t{name}\t{theirs}")
    if missing_in_csv:
        print(f"\n【CSV 沒有】{', '.join(missing_in_csv)}")

    if apply:
        changed = []
        for code in sorted(apply):
            if code not in site or code not in sheet or sheet[code][1] is None:
                print(f"[略過] {code}：站上或 CSV 沒有這檔的管理費")
                continue
            path = ETF_DIR / f"{code}.json"
            data = site[code]
            data["managementFee"] = sheet[code][1]
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            changed.append(code)
        print(f"\n已更新管理費：{', '.join(changed) or '無'}（下次 ETF 週更重建索引後上線）")


if __name__ == "__main__":
    main()
