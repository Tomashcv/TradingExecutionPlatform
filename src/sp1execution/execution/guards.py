from dataclasses import dataclass


@dataclass(frozen=True)
class RiskLimits:
    max_order_fraction_of_nav: float=0.40
    max_total_daily_turnover: float=1.25
    allow_short: bool=False
    allow_margin: bool=False

def validate_target_weights(sp2,sp500):
    if sp2<0 or sp500<0: raise ValueError("negative target forbidden")
    if abs(sp2+sp500-1.0)>1e-9: raise ValueError("weights must sum to 1")
