#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path.home() / "Desktop/projs/SP1Execution"
DEFAULT_DB = ROOT / "state/sp1execution.sqlite"
DEFAULT_LOG = ROOT / "logs/sp1_demo_forward_v04.jsonl"
NY = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 35)
SESSION_CLOSE = time(15, 55)


class ForwardSafetyError(RuntimeError):
    pass


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def validate_demo_environment() -> None:
    if os.environ.get("SP1_T212_ENV") != "demo":
        raise ForwardSafetyError("automated forward runner requires SP1_T212_ENV=demo")
    if _truthy(os.environ.get("SP1_LIVE_TRADING")):
        raise ForwardSafetyError("automated forward runner requires SP1_LIVE_TRADING=false")


def in_us_regular_window(now: datetime) -> bool:
    if now.tzinfo is None:
        raise ForwardSafetyError("runner clock must be timezone-aware")
    ny = now.astimezone(NY)
    if ny.weekday() >= 5:
        return False
    current = ny.timetz().replace(tzinfo=None)
    return SESSION_OPEN <= current <= SESSION_CLOSE


def cycle_lock_path(db_path: Path) -> Path:
    canonical = str(db_path.expanduser().resolve())
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / f"sp1execution-cycle-{digest}.lock"


def append_record(log_path: Path, record: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                record,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            + "\n"
        )
        fh.flush()
        os.fsync(fh.fileno())


def parse_cycle_output(stdout: str) -> dict[str, str]:
    wanted = {
        "ACTION",
        "REASON",
        "WORKFLOW_ID",
        "DECISION_ID",
        "BROKER_ORDER_IDS",
        "LIVE_APPROVED",
    }
    parsed: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in wanted:
            parsed[key] = value
    return parsed


def run_once(
    *,
    db_path: Path,
    log_path: Path,
    confirm_demo: bool,
    dry_run: bool,
    now: datetime | None = None,
) -> int:
    validate_demo_environment()

    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ForwardSafetyError("runner clock must be timezone-aware")

    base = {
        "schema": "sp1execution_m10_forward_runner_v1",
        "recorded_at": now.astimezone(UTC).isoformat(),
        "ny_time": now.astimezone(NY).isoformat(),
        "db_path": str(db_path.expanduser().resolve()),
        "confirm_demo": bool(confirm_demo),
        "dry_run": bool(dry_run),
        "live_approved": False,
    }

    if not in_us_regular_window(now):
        append_record(
            log_path,
            {
                **base,
                "status": "SKIP",
                "action": "SKIP_OUTSIDE_US_REGULAR_WINDOW",
                "returncode": 0,
            },
        )
        print("M10_FORWARD_ACTION=SKIP_OUTSIDE_US_REGULAR_WINDOW")
        return 0

    lock_path = cycle_lock_path(db_path)
    fd = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR,
        0o600,
    )
    acquired = False

    try:
        try:
            fcntl.flock(
                fd,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
            acquired = True
        except BlockingIOError:
            append_record(
                log_path,
                {
                    **base,
                    "status": "SKIP",
                    "action": "SKIP_CYCLE_LOCK_HELD",
                    "returncode": 0,
                },
            )
            print("M10_FORWARD_ACTION=SKIP_CYCLE_LOCK_HELD")
            return 0

        if dry_run:
            append_record(
                log_path,
                {
                    **base,
                    "status": "PASS",
                    "action": "DRY_RUN_READY",
                    "returncode": 0,
                },
            )
            print("M10_FORWARD_ACTION=DRY_RUN_READY")
            return 0

        cmd = [
            str(ROOT / ".venv/bin/sp1exec"),
            "cycle",
            "--db-path",
            str(db_path),
        ]
        if confirm_demo:
            cmd.append("--confirm-demo")

        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        print(proc.stdout, end="")

        parsed = parse_cycle_output(proc.stdout)
        action = parsed.get("ACTION", "UNKNOWN")

        append_record(
            log_path,
            {
                **base,
                "status": "PASS" if proc.returncode == 0 else "ERROR",
                "action": action,
                "cycle": parsed,
                "returncode": proc.returncode,
            },
        )

        print(f"M10_FORWARD_ACTION={action}")
        print(f"M10_FORWARD_RC={proc.returncode}")
        return proc.returncode
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB,
    )
    parser.add_argument(
        "--log-path",
        type=Path,
        default=DEFAULT_LOG,
    )
    parser.add_argument(
        "--confirm-demo",
        action="store_true",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return run_once(
        db_path=args.db_path,
        log_path=args.log_path,
        confirm_demo=args.confirm_demo,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
