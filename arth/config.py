from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class StrategyParams:
    top_n: int = 15
    overlay: bool = True
    universe_size: int = 500
    min_price: float = 30.0
    min_history: int = 260
    vol_window: int = 252
    buffer_mult: float = 2.0
    band: float = 0.25
    ma_days: int = 200
    risk_off_exposure: float = 0.5
    scorer: str = "ensemble"
    min_tv: float = 0.0          # minimum 126-day median traded value, rupees


def load(path: str | Path | None = None) -> dict:
    p = Path(path) if path else ROOT / "config" / "arth.yaml"
    with open(p) as f:
        return yaml.safe_load(f)


def strategy_params(cfg: dict | None = None) -> StrategyParams:
    cfg = cfg or load()
    dial = cfg["dials"][cfg["dial"]]
    return StrategyParams(
        top_n=dial["top_n"], overlay=dial["overlay"],
        universe_size=dial.get("universe_size", cfg["universe"]["size"]), min_price=cfg["universe"]["min_price"],
        min_history=cfg["universe"]["min_history"], vol_window=cfg["signal"]["vol_window"],
        buffer_mult=cfg["signal"]["buffer_mult"], band=cfg["signal"]["band"],
        ma_days=cfg["overlay"]["ma_days"], risk_off_exposure=cfg["overlay"]["risk_off_exposure"],
        scorer=dial.get("scorer", cfg["signal"]["scorer"]), min_tv=float(dial.get("min_tv", 0.0)))
