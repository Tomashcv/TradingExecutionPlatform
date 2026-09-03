from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass

import httpx

IVV_LATEST_HOLDINGS_URL = (
    "https://www.ishares.com/us/products/239726/"
    "ishares-core-s-p-500-etf/latest-holdings.csv"
)


@dataclass(frozen=True)
class IVVHolding:
    ticker: str
    name: str
    asset_class: str
    weight_pct: float


@dataclass(frozen=True)
class IVVSnapshot:
    as_of: str
    sha256: str
    holdings: tuple[IVVHolding, ...]

    @property
    def top2(self) -> tuple[IVVHolding, IVVHolding]:
        equities = [h for h in self.holdings if h.asset_class.lower() == "equity"]
        if len(equities) < 2:
            raise RuntimeError("IVV snapshot has fewer than 2 equity holdings.")
        equities.sort(key=lambda h: h.weight_pct, reverse=True)
        return equities[0], equities[1]


def parse_ivv_holdings_csv(raw: bytes) -> IVVSnapshot:
    text = raw.decode("utf-8-sig", errors="strict")
    lines = text.splitlines()
    if len(lines) < 10:
        raise ValueError("IVV holdings CSV unexpectedly short.")

    m = re.search(r'Fund Holdings as of,"([^"]+)"', text[:1000])
    if not m:
        raise ValueError("Could not parse IVV as-of date.")
    as_of = m.group(1)

    header_index = None
    for i, line in enumerate(lines):
        if line.startswith("Ticker,Name,Sector,Asset Class,"):
            header_index = i
            break
    if header_index is None:
        raise ValueError("Could not locate IVV holdings header.")

    reader = csv.DictReader(io.StringIO("\n".join(lines[header_index:])))
    holdings: list[IVVHolding] = []
    for row in reader:
        ticker = (row.get("Ticker") or "").strip()
        asset_class = (row.get("Asset Class") or "").strip()
        weight_raw = (row.get("Weight (%)") or "").replace(",", "").strip()
        if not ticker or not weight_raw:
            continue
        try:
            weight = float(weight_raw)
        except ValueError:
            continue
        holdings.append(
            IVVHolding(
                ticker=ticker,
                name=(row.get("Name") or "").strip(),
                asset_class=asset_class,
                weight_pct=weight,
            )
        )

    if len(holdings) < 400:
        raise ValueError(f"IVV holdings count unexpectedly low: {len(holdings)}")

    return IVVSnapshot(
        as_of=as_of,
        sha256=hashlib.sha256(raw).hexdigest(),
        holdings=tuple(holdings),
    )


def fetch_latest_ivv_snapshot(timeout: float = 20.0) -> IVVSnapshot:
    headers = {
        "User-Agent": "SP1Execution/0.2 (+paper research; holdings snapshot)"
    }
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as c:
        r = c.get(IVV_LATEST_HOLDINGS_URL)
        r.raise_for_status()
        return parse_ivv_holdings_csv(r.content)
