#!/usr/bin/env python3
"""
Single-writer apply pipeline for Xray config.

Responsibilities:
- Validate candidate config structure.
- Serialize writes via an exclusive file lock.
- Atomically replace target config (os.replace on same filesystem).
- Preserve current config on any failure (fail-closed update behavior).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows host tests
    fcntl = None


REQUIRED_TOP_LEVEL_KEYS = ("inbounds", "outbounds", "routing")
IGNORED_PROXY_PROTOCOLS = {"freedom", "blackhole", "dns"}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json_file(path: Path) -> tuple[bytes, dict]:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError(f"Candidate config is not UTF-8: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Candidate config is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("Candidate config root must be a JSON object")
    return raw, payload


def validate_candidate_config(config: dict) -> None:
    missing = [key for key in REQUIRED_TOP_LEVEL_KEYS if key not in config]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"Candidate config missing required keys: {joined}")

    inbounds = config.get("inbounds")
    outbounds = config.get("outbounds")
    routing = config.get("routing")

    if not isinstance(inbounds, list) or not inbounds:
        raise ValueError("Candidate config has empty or invalid 'inbounds'")
    if not isinstance(outbounds, list) or not outbounds:
        raise ValueError("Candidate config has empty or invalid 'outbounds'")
    if not isinstance(routing, dict):
        raise ValueError("Candidate config has invalid 'routing' section")

    rules = routing.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("Candidate config has empty or invalid 'routing.rules'")

    outbound_tags = []
    for outbound in outbounds:
        if not isinstance(outbound, dict):
            continue
        tag = outbound.get("tag")
        if isinstance(tag, str) and tag:
            outbound_tags.append(tag)

    seen_outbound_tags = set()
    duplicate_outbound_tags = set()
    for tag in outbound_tags:
        if tag in seen_outbound_tags:
            duplicate_outbound_tags.add(tag)
        seen_outbound_tags.add(tag)
    if duplicate_outbound_tags:
        joined = ", ".join(sorted(duplicate_outbound_tags))
        raise ValueError(f"Candidate config has duplicate outbound tags: {joined}")

    balancers = routing.get("balancers", [])
    if balancers is None:
        balancers = []
    if not isinstance(balancers, list):
        raise ValueError("Candidate config has invalid 'routing.balancers' section")

    balancer_tags = set()
    for balancer in balancers:
        if not isinstance(balancer, dict):
            raise ValueError("Candidate config has invalid balancer entry")
        tag = balancer.get("tag")
        if not isinstance(tag, str) or not tag:
            raise ValueError("Candidate config balancer is missing non-empty 'tag'")
        balancer_tags.add(tag)

    for idx, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"routing.rules[{idx}] must be an object")

        outbound_tag = rule.get("outboundTag")
        balancer_tag = rule.get("balancerTag")

        if outbound_tag and balancer_tag:
            raise ValueError(
                f"routing.rules[{idx}] cannot define both outboundTag and balancerTag"
            )

        if outbound_tag:
            if outbound_tag not in outbound_tags:
                raise ValueError(
                    "routing.rules"
                    f"[{idx}] references unknown outboundTag: {outbound_tag}"
                )
        elif balancer_tag:
            if balancer_tag not in balancer_tags:
                raise ValueError(
                    "routing.rules"
                    f"[{idx}] references unknown balancerTag: {balancer_tag}"
                )
        else:
            raise ValueError(
                f"routing.rules[{idx}] must define outboundTag or balancerTag"
            )

    has_block = any(
        isinstance(ob, dict)
        and (ob.get("tag") == "block" or ob.get("protocol") == "blackhole")
        for ob in outbounds
    )
    if not has_block:
        raise ValueError("Candidate config must include fail-closed 'block' outbound")

    has_proxy_outbound = False
    for outbound in outbounds:
        if not isinstance(outbound, dict):
            continue
        tag = outbound.get("tag")
        proto = outbound.get("protocol")
        if not tag:
            continue
        if tag in {"direct", "block"}:
            continue
        if proto in IGNORED_PROXY_PROTOCOLS:
            continue
        has_proxy_outbound = True
        break

    if not has_proxy_outbound:
        raise ValueError("Candidate config has no proxy outbounds")


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def apply_candidate(candidate_path: Path, target_path: Path) -> bool:
    raw_candidate, parsed_candidate = _read_json_file(candidate_path)
    validate_candidate_config(parsed_candidate)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target_path.parent / f".{target_path.name}.lock"

    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)

        current_raw = target_path.read_bytes() if target_path.exists() else b""
        if current_raw and _sha256_bytes(current_raw) == _sha256_bytes(raw_candidate):
            return False

        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target_path.name}.",
            suffix=".tmp",
            dir=str(target_path.parent),
        )
        tmp_path = Path(tmp_name)

        try:
            with os.fdopen(fd, "wb") as tmp_handle:
                tmp_handle.write(raw_candidate)
                tmp_handle.flush()
                os.fsync(tmp_handle.fileno())

            os.replace(tmp_path, target_path)
            _fsync_directory(target_path.parent)
            return True
        finally:
            if tmp_path.exists():
                tmp_path.unlink()


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "Usage: apply_xray_config.py <candidate_json> <target_json>",
            file=sys.stderr,
        )
        return 2

    candidate_path = Path(sys.argv[1])
    target_path = Path(sys.argv[2])

    try:
        changed = apply_candidate(candidate_path, target_path)
        if changed:
            print(f"INFO Config applied atomically: {target_path}")
        else:
            print("INFO Config unchanged; no replace")
        return 0
    except Exception as exc:
        print(f"apply_xray_config.py error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())