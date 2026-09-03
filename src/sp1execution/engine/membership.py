from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sp1execution.market_data.ivv_holdings import fetch_latest_ivv_snapshot


@dataclass(frozen=True)
class Membership:
    month: str
    source_as_of: str
    symbols: tuple[str, str]
    source: str


def _contract_paths() -> list[Path]:
    return sorted(Path("contracts/memberships").glob("????-??.json"))


def load_latest_frozen_membership() -> Membership:
    paths = _contract_paths()
    state_paths = sorted(Path("state/memberships").glob("????-??.json"))
    candidates = paths + state_paths
    if not candidates:
        raise RuntimeError("No frozen monthly membership exists.")

    def month_of(path: Path) -> str:
        return path.stem

    path = max(candidates, key=month_of)
    payload = json.loads(path.read_text())
    symbols = tuple(payload["symbols"])
    if len(symbols) != 2 or symbols[0] == symbols[1]:
        raise RuntimeError(f"Invalid Top2 membership in {path}")
    return Membership(
        month=str(payload["month"]),
        source_as_of=str(payload["source_as_of"]),
        symbols=(str(symbols[0]), str(symbols[1])),
        source=str(payload.get("source", "iShares IVV holdings")),
    )


def archive_latest_ivv() -> Path:
    snapshot = fetch_latest_ivv_snapshot()
    a, b = snapshot.top2
    as_of_iso = date.fromisoformat(
        __import__("datetime").datetime.strptime(snapshot.as_of, "%b %d, %Y").date().isoformat()
    ).isoformat()

    directory = Path("state/holdings")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{as_of_iso}.json"
    path.write_text(
        json.dumps(
            {
                "as_of": as_of_iso,
                "source_sha256": snapshot.sha256,
                "symbols": [a.ticker, b.ticker],
                "weights_pct": [a.weight_pct, b.weight_pct],
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
    return path


def freeze_month_from_archive(month: str) -> Path:
    archive = Path("state/holdings")
    rows = []
    for path in archive.glob(f"{month}-??.json"):
        payload = json.loads(path.read_text())
        rows.append((payload["as_of"], payload))
    if not rows:
        raise RuntimeError(f"No archived IVV snapshots for {month}.")

    _, latest = max(rows, key=lambda x: x[0])
    symbols = list(latest["symbols"])
    if len(symbols) != 2:
        raise RuntimeError("Archived Top2 does not contain exactly two symbols.")

    directory = Path("state/memberships")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{month}.json"
    if path.exists():
        existing = json.loads(path.read_text())
        if existing != {
            "month": month,
            "source": "iShares IVV holdings archived snapshot",
            "source_as_of": latest["as_of"],
            "source_sha256": latest["source_sha256"],
            "symbols": symbols,
        }:
            raise RuntimeError(f"Frozen membership already exists and differs: {path}")
        return path

    payload = {
        "month": month,
        "source": "iShares IVV holdings archived snapshot",
        "source_as_of": latest["as_of"],
        "source_sha256": latest["source_sha256"],
        "symbols": symbols,
    }
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
    return path
