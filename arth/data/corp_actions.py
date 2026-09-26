"""
Corporate actions from NSE's daily PR archive (Bc*.csv), turned into price-adjustment factors.

Handled: face-value splits, bonuses, consolidations. A factor f on ex-date t means prices before t are
multiplied by f to be comparable with prices from t on (split 10->2: f = 0.2; bonus 1:1: f = 0.5).
Not handled (documented bias): rights issues (usually a few percent) and demergers (the parent's drop
stays in the series, which understates returns for holders - a conservative error).
"""
from __future__ import annotations
import glob
import re
from pathlib import Path
import pandas as pd

NUM = r"(\d+(?:\.\d+)?)"
SPLIT = re.compile(r"(?:SPLT|SPLIT|SPL\b|SUB[\s-]*DIVISION).*?(?:RS\.?|RE\.?|FRM|FROM)\s*" + NUM
                   + r"(?:/-)?.*?\bTO\b\s*(?:RS\.?|RE\.?)?\s*" + NUM, re.I)
BONUS = re.compile(r"\bBON(?:US)?\b\s*[-:]?\s*" + NUM + r"\s*:\s*" + NUM, re.I)
CONSOL = re.compile(r"CONSOLIDAT.*?(?:RS|RE)\.?\s*" + NUM + r".*?(?:TO|-)\s*(?:RS|RE)\.?\s*" + NUM, re.I)


def factor_from_purpose(purpose: str) -> tuple[float, str] | None:
    """Multiply every split / bonus / consolidation found in the purpose text."""
    p = str(purpose); f = 1.0; kinds = []
    m = SPLIT.search(p)
    if m:
        old, new = float(m.group(1)), float(m.group(2))
        if old > 0 and 0 < new < old:
            f *= new / old; kinds.append("split")
    m = BONUS.search(p)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if a > 0 and b > 0:
            f *= b / (a + b); kinds.append("bonus")
    m = CONSOL.search(p)
    if m:
        old, new = float(m.group(1)), float(m.group(2))
        if old > 0 and new > old:
            f *= new / old; kinds.append("consolidation")
    return (f, "+".join(kinds)) if kinds else None


def load(bc_dir: str = "data/raw/bc") -> pd.DataFrame:
    parts = []
    for f in sorted(glob.glob(f"{bc_dir}/*.parquet")):
        d = pd.read_parquet(f); d["file_date"] = Path(f).stem; parts.append(d)
    bc = pd.concat(parts, ignore_index=True)
    bc = bc[bc.SERIES.isin(["EQ", "BE", "BZ"])]
    iso = pd.to_datetime(bc.EX_DT, format="%Y-%m-%d", errors="coerce")          # UDiFF-era files
    dmy = pd.to_datetime(bc.EX_DT, format="%d/%m/%Y", errors="coerce")          # older files
    dmy2 = pd.to_datetime(bc.EX_DT, format="%d-%b-%Y", errors="coerce")
    bc["ex"] = iso.fillna(dmy).fillna(dmy2)
    bc = bc.dropna(subset=["ex"]).drop_duplicates(["SYMBOL", "ex", "PURPOSE"])
    rows = []
    for sym, ex, pur in zip(bc.SYMBOL, bc.ex, bc.PURPOSE):
        r = factor_from_purpose(pur)
        if r:
            rows.append((sym, ex, r[0], r[1], pur))
    ca = pd.DataFrame(rows, columns=["symbol", "ex", "factor", "kind", "purpose"])
    # one factor per symbol and ex-date (a split and a bonus on the same day multiply)
    ca = ca.drop_duplicates(["symbol", "ex", "kind", "factor"])
    return ca.groupby(["symbol", "ex"], as_index=False).agg(factor=("factor", "prod"),
                                                            kind=("kind", "+".join), purpose=("purpose", " | ".join))
