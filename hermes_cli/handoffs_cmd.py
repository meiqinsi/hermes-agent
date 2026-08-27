"""Read-only CLI projection over durable Hermes handoff carriers.

V1 intentionally projects only async delegation and gateway final-delivery
state.  It creates no schema, writes no acknowledgements, and never reads the
stored prompt/result/response payload columns.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from hermes_constants import get_default_hermes_root, get_hermes_home

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_PLATFORM_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_PRODUCER_STATES = frozenset(
    {
        "running",
        "finalizing",
        "completed",
        "success",
        "error",
        "failed",
        "unknown",
        "stalled",
        "interrupted",
    }
)
_DELEGATION_DELIVERY_STATES = frozenset({"pending", "delivered", "dropped"})
_FINAL_DELIVERY_STATES = frozenset(
    {"pending", "attempting", "delivered", "failed", "abandoned"}
)
_SAFE_FIELDS = (
    "carrier",
    "kind",
    "id",
    "profile",
    "producer_state",
    "delivery_state",
    "origin_session",
    "target_summary",
    "created_at",
    "updated_at",
    "attempts",
    "stale_reason",
    "payload_available",
    "terminal",
)


def build_parser(parent_subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = parent_subparsers.add_parser(
        "handoffs",
        help="Inspect durable handoff activity (read-only)",
        description=(
            "Read-only projection of async delegation and final reply delivery state. "
            "Payload text and raw target addresses are never shown."
        ),
    )
    sub = parser.add_subparsers(dest="handoffs_action")
    listing = sub.add_parser("list", help="List durable handoff activity")
    listing.add_argument(
        "--state",
        choices=["actionable"],
        help=(
            "Conservative projection: hide only confirmed delivered rows; "
            "native carrier state remains unchanged"
        ),
    )
    listing.add_argument(
        "--all-profiles",
        action="store_true",
        help="Read every profile state store in SQLite read-only mode",
    )
    listing.add_argument("--json", action="store_true", dest="as_json")
    show = sub.add_parser("show", help="Show one carrier-qualified handoff")
    show.add_argument("handoff_id", help="Namespaced id: delegation:<id> or delivery:<id>")
    show.add_argument("--json", action="store_true", dest="as_json")
    parser.set_defaults(_handoffs_parser=parser)
    return parser


def _safe_id(value: Any) -> str:
    text = str(value or "")
    if _SAFE_ID_RE.fullmatch(text):
        return text
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
    return f"opaque-{digest}"


def _native_state(value: Any, allowed: frozenset[str]) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else "unknown"


def _safe_session_ref(profile: str, *candidates: Any) -> str | None:
    for candidate in candidates:
        text = str(candidate or "").strip()
        if _SAFE_ID_RE.fullmatch(text):
            return f"@session:{profile}/{text}"
    return None


def _safe_platform(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if _SAFE_PLATFORM_RE.fullmatch(text) else None


def _safe_count(value: Any) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, count)


def _safe_timestamp(value: Any) -> float | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return timestamp if math.isfinite(timestamp) else None


def _terminal_for_delegation(producer: str, delivery: str) -> bool:
    return producer not in {"running", "finalizing", "unknown"} and delivery in {
        "delivered",
        "dropped",
    }


def _delegation_row(native: dict[str, Any], profile: str) -> dict[str, Any]:
    producer = _native_state(native.get("producer_state"), _PRODUCER_STATES)
    delivery = _native_state(
        native.get("delivery_state"), _DELEGATION_DELIVERY_STATES
    )
    stale_reason = "delivery_dropped" if delivery == "dropped" else None
    row = {
        "carrier": "delegation",
        "kind": "async_delegation",
        "id": _safe_id(native.get("delegation_id")),
        "profile": profile,
        "producer_state": producer,
        "delivery_state": delivery,
        "origin_session": _safe_session_ref(
            profile,
            native.get("parent_session_id"),
            native.get("origin_session_id"),
            native.get("session_key"),
        ),
        "target_summary": None,
        "created_at": _safe_timestamp(native.get("dispatched_at")),
        "updated_at": _safe_timestamp(native.get("updated_at")),
        "attempts": _safe_count(native.get("delivery_attempts")),
        "stale_reason": stale_reason,
        "payload_available": bool(native.get("payload_available")),
        "terminal": _terminal_for_delegation(producer, delivery),
    }
    return {key: row[key] for key in _SAFE_FIELDS}


def _delivery_row(native: dict[str, Any], profile: str) -> dict[str, Any]:
    delivery = _native_state(native.get("delivery_state"), _FINAL_DELIVERY_STATES)
    stale_reason = "delivery_abandoned" if delivery == "abandoned" else None
    row = {
        "carrier": "delivery",
        "kind": "gateway_final_response",
        "id": _safe_id(native.get("obligation_id")),
        "profile": profile,
        "producer_state": "unknown",
        "delivery_state": delivery,
        "origin_session": None,
        "target_summary": _safe_platform(native.get("platform")),
        "created_at": _safe_timestamp(native.get("created_at")),
        "updated_at": _safe_timestamp(native.get("updated_at")),
        "attempts": _safe_count(native.get("attempts")),
        "stale_reason": stale_reason,
        "payload_available": bool(native.get("payload_available")),
        "terminal": delivery in {"delivered", "abandoned"},
    }
    return {key: row[key] for key in _SAFE_FIELDS}


def _read_profile(profile: str, home: Path) -> tuple[list[dict[str, Any]], list[str]]:
    db_path = home / "state.db"
    if db_path.is_symlink():
        return [], [f"{profile}: state store is a symlink"]
    if not db_path.is_file():
        return [], [f"{profile}: state store is missing"]

    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    try:
        from tools.async_delegation import list_durable_delegations_readonly

        native_rows = list_durable_delegations_readonly(db_path)
        rows.extend(_delegation_row(item, profile) for item in native_rows)
        if any(item.get("_schema_incomplete") for item in native_rows):
            warnings.append(f"{profile}: delegation store uses a legacy schema")
    except Exception as exc:
        warnings.append(f"{profile}: delegation store unavailable ({type(exc).__name__})")
    try:
        from gateway.delivery_ledger import list_delivery_obligations_readonly

        native_rows = list_delivery_obligations_readonly(db_path)
        rows.extend(_delivery_row(item, profile) for item in native_rows)
        if any(item.get("_schema_incomplete") for item in native_rows):
            warnings.append(f"{profile}: delivery store uses a legacy schema")
    except Exception as exc:
        warnings.append(f"{profile}: delivery store unavailable ({type(exc).__name__})")
    return rows, warnings


def _current_profile() -> tuple[str, Path]:
    try:
        from hermes_cli.profiles import get_active_profile_name

        name = get_active_profile_name()
    except Exception:
        name = "default"
    return name, get_hermes_home()


def _list_current() -> tuple[list[dict[str, Any]], list[str]]:
    profile, home = _current_profile()
    rows, warnings = _read_profile(profile, Path(home))
    rows.sort(key=lambda row: (-(float(row["updated_at"] or 0)), row["carrier"], row["id"]))
    return rows, warnings


def _list_all_profiles() -> tuple[list[dict[str, Any]], list[str]]:
    root = Path(get_default_hermes_root())
    profile_homes: list[tuple[str, Path]] = [("default", root)]
    warnings: list[str] = []
    try:
        from hermes_cli.profiles import validate_profile_name

        profiles_root = root / "profiles"
        if profiles_root.is_symlink():
            warnings.append("profiles: profile directory is a symlink")
        elif profiles_root.is_dir():
            for entry in sorted(profiles_root.iterdir()):
                try:
                    validate_profile_name(entry.name)
                except ValueError:
                    continue
                if entry.is_symlink():
                    warnings.append(f"{entry.name}: profile directory is a symlink")
                elif entry.is_dir():
                    profile_homes.append((entry.name, entry))
    except Exception as exc:
        warnings.append(f"profiles: unable to enumerate ({type(exc).__name__})")
    rows: list[dict[str, Any]] = []
    for profile, home in profile_homes:
        profile_rows, profile_warnings = _read_profile(profile, home)
        rows.extend(profile_rows)
        warnings.extend(profile_warnings)
    rows.sort(
        key=lambda row: (
            -(float(row["updated_at"] or 0)),
            row["profile"],
            row["carrier"],
            row["id"],
        )
    )
    return rows, warnings


def _is_actionable(row: dict[str, Any]) -> bool:
    """Conservative attended-work projection, never a persisted state."""
    return row.get("delivery_state") != "delivered"


def _print_text(rows: list[dict[str, Any]], warnings: list[str]) -> None:
    if not rows:
        print("No durable handoff activity found.")
    else:
        print(f"Handoffs ({len(rows)}):")
        for row in rows:
            target = (
                f" target={row['target_summary']}" if row["target_summary"] else ""
            )
            print(
                f"  {row['profile']}  {row['carrier']}:{row['id']}  "
                f"producer={row['producer_state']} delivery={row['delivery_state']} "
                f"attempts={row['attempts']}{target}"
            )
    for warning in warnings:
        print(f"Warning: {warning}")


def handoffs_command(args: argparse.Namespace) -> int:
    action = getattr(args, "handoffs_action", None)
    if not action:
        parser = getattr(args, "_handoffs_parser", None)
        if parser is not None:
            parser.print_help()
        return 0
    rows, warnings = (
        _list_all_profiles()
        if getattr(args, "all_profiles", False)
        else _list_current()
    )
    if action == "show":
        reference = str(getattr(args, "handoff_id", "") or "")
        if ":" not in reference:
            print("handoffs: id must be carrier-qualified", file=__import__("sys").stderr)
            return 2
        carrier, native_id = reference.split(":", 1)
        if carrier not in {"delegation", "delivery"} or not native_id:
            print("handoffs: unknown carrier or empty id", file=__import__("sys").stderr)
            return 2
        found = next(
            (row for row in rows if row["carrier"] == carrier and row["id"] == native_id),
            None,
        )
        if found is None:
            print(f"handoffs: no such handoff: {reference}", file=__import__("sys").stderr)
            return 1
        if getattr(args, "as_json", False):
            print(json.dumps({"handoff": found, "warnings": warnings}, sort_keys=True))
        else:
            _print_text([found], warnings)
        return 0
    if action != "list":
        return 2
    if getattr(args, "state", None) == "actionable":
        rows = [row for row in rows if _is_actionable(row)]
    if getattr(args, "as_json", False):
        print(json.dumps({"handoffs": rows, "warnings": warnings}, sort_keys=True))
    else:
        _print_text(rows, warnings)
    return 0
