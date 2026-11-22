#!/usr/bin/env python3
"""Sticky Balancer Daemon for Xray Proxy.

Maintains sticky node affinity for Xray proxy balancers to prevent IP flapping,
session terminations, and anti-bot bans.

It drives one or more "lanes". Each lane is an Xray balancer whose choice is
pinned through the API override:
  * lane "global"        -> balancer proxy-auto (all LAN traffic)
  * lane "client:<ip>"   -> balancer client-<ip> (clients pinned via tunnelctl)

Per lane (policy from the tunnelctl control file, config/tunnel_policy.json):
1. STICKY NODE: keeps routing pinned to the active node via Xray API override.
2. COUNTRY RULES: only nodes allowed by the lane filter (all / only / except
   countries) are candidates. If none of them is alive the lane falls back to
   the best alive node overall and reports a policy violation.
3. MANUAL: `mode=manual` holds the chosen node. If it dies, the lane fails
   over within the filter and comes back when it is alive again.
4. FAILOVER (DEAD): immediately fails over to the best alive node if the current
   node dies (fails health checks for N consecutive ticks).
5. HYSTERESIS: switches during normal operation ONLY if an alternative node is
   substantially better (e.g. >= 50% lower RTT and >= 100ms delta) for multiple
   consecutive checks, or if current node heavily degrades (> 2500ms).
6. ROTATION: daily at a randomized time around 6:00 AM (default), every N
   minutes/hours, or off. Strategy best / next / random. One-shot "rotate now"
   requests come from tunnelctl.

Every tick the daemon also dumps the observatory view of all nodes into the
state file, which is how tunnelctl (running on the host without gRPC) sees
latencies.
"""

import datetime
import json
import logging
import os
import random
import signal
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
# Add xray_grpc directory to python path
GRPC_DIR = SCRIPTS_DIR / "xray_grpc"
if str(GRPC_DIR) not in sys.path:
    sys.path.insert(0, str(GRPC_DIR))

import tunnel_lib as tl  # noqa: E402

try:
    import grpc
    from app.observatory.command import command_pb2 as obs_pb2
    from app.observatory.command import command_pb2_grpc as obs_pb2_grpc
    from app.router.command import command_pb2 as router_pb2
    from app.router.command import command_pb2_grpc as router_pb2_grpc
except Exception:  # pragma: no cover - lets the pure logic be imported/tested without grpc
    grpc = None

# Configuration defaults
API_SERVER = os.getenv("XRAY_API_SERVER", "127.0.0.1:10085")
BALANCER_TAG = os.getenv("XRAY_BALANCER_TAG", "proxy-auto")
OVPN_TAG = os.getenv("OPENVPN_OUT_TAG", "openvpn-out")

CHECK_INTERVAL_SEC = float(os.getenv("STICKY_CHECK_INTERVAL_SEC", "15"))
ROTATION_HOUR = int(os.getenv("STICKY_ROTATION_HOUR", "6"))
ROTATION_WINDOW_MIN = int(os.getenv("STICKY_ROTATION_WINDOW_MIN", "30"))

FAIL_THRESHOLD = int(os.getenv("STICKY_FAIL_THRESHOLD", "2"))
HYSTERESIS_RATIO = float(os.getenv("STICKY_HYSTERESIS_RATIO", "0.5"))  # Candidate must be 50% faster
MIN_DIFF_MS = int(os.getenv("STICKY_MIN_DIFF_MS", "100"))            # and at least 100ms faster
STABILITY_TICKS = int(os.getenv("STICKY_STABILITY_TICKS", "3"))       # candidate must hold advantage
MAX_DEGRADED_RTT_MS = int(os.getenv("STICKY_MAX_DEGRADED_RTT_MS", "2500"))

# Control files written by tunnelctl / the updater (./config mounted read-only)
CONFIG_DIR = Path(os.getenv("STICKY_CONFIG_DIR", "/etc/xray"))
if not CONFIG_DIR.exists():
    CONFIG_DIR = SCRIPTS_DIR.parent / "config"
POLICY_FILE = CONFIG_DIR / "tunnel_policy.json"
META_FILE = CONFIG_DIR / "nodes_meta.json"
XRAY_CONFIG_FILE = CONFIG_DIR / "config.json"


def resolve_data_dir() -> Path:
    env_dir = os.getenv("STICKY_DATA_DIR")
    if env_dir:
        p = Path(env_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    var_log = Path("/var/log/xray")
    try:
        var_log.mkdir(parents=True, exist_ok=True)
        test_file = var_log / ".test_perm"
        test_file.touch()
        test_file.unlink()
        return var_log
    except Exception:
        pass

    repo_data = Path(__file__).resolve().parent.parent / "data"
    try:
        repo_data.mkdir(parents=True, exist_ok=True)
        return repo_data
    except Exception:
        pass

    tmp_dir = Path("/tmp/xray")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return tmp_dir


DATA_DIR = resolve_data_dir()
LOG_FILE = DATA_DIR / "sticky_balancer.log"
STATE_FILE = DATA_DIR / "sticky_state.json"


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("sticky_balancer")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception as e:
        logger.warning(f"Could not open log file {LOG_FILE}: {e}")

    return logger


logger = setup_logger()


def compute_next_rotation(now: datetime.datetime) -> datetime.datetime:
    """Calculate next daily rotation target randomized around ROTATION_HOUR."""
    jitter = random.randint(-ROTATION_WINDOW_MIN * 60, ROTATION_WINDOW_MIN * 60)
    base_today = now.replace(hour=ROTATION_HOUR, minute=0, second=0, microsecond=0)
    target = base_today + datetime.timedelta(seconds=jitter)

    if target <= now:
        base_tomorrow = (now + datetime.timedelta(days=1)).replace(
            hour=ROTATION_HOUR, minute=0, second=0, microsecond=0
        )
        jitter = random.randint(-ROTATION_WINDOW_MIN * 60, ROTATION_WINDOW_MIN * 60)
        target = base_tomorrow + datetime.timedelta(seconds=jitter)

    return target


def schedule_next(schedule: str, now: datetime.datetime):
    """Next rotation for a lane schedule ('daily' keeps the env-configured hour)."""
    try:
        kind, _ = tl.parse_schedule(schedule)
    except ValueError as e:
        logger.warning(f"{e}; falling back to daily")
        kind, schedule = "daily", "daily"
    if kind == "daily" and "@" not in (schedule or ""):
        return compute_next_rotation(now)
    return tl.next_rotation_time(schedule, now, ROTATION_WINDOW_MIN)


def _parse_dt(raw):
    if not raw:
        return None
    try:
        return datetime.datetime.fromisoformat(raw)
    except Exception:
        return None


class Lane:
    """Sticky selection state for one Xray balancer."""

    def __init__(self, lane_id: str, balancer: str):
        self.id = lane_id
        self.balancer = balancer
        self.active_node = ""
        self.active_delay = 0
        self.last_switch_time = ""
        self.last_switch_reason = ""
        self.consecutive_failures = 0
        self.candidate_tag = ""
        self.candidate_streak = 0
        self.next_rotation = None
        self.schedule = None
        self.handled_rotate_request = ""
        self.manual_override = False
        self.status = "init"
        self.violation = ""

    # -- persistence -------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "balancer": self.balancer,
            "active_node": self.active_node,
            "active_delay_ms": self.active_delay,
            "last_switch_time": self.last_switch_time,
            "last_switch_reason": self.last_switch_reason,
            "next_rotation": self.next_rotation.isoformat() if self.next_rotation else None,
            "schedule": self.schedule,
            "handled_rotate_request": self.handled_rotate_request,
            "manual_override": self.manual_override,
            "status": self.status,
            "policy_violation": self.violation,
        }

    def load_dict(self, d: dict) -> None:
        self.active_node = d.get("active_node", "") or ""
        self.last_switch_time = d.get("last_switch_time", "") or ""
        self.last_switch_reason = d.get("last_switch_reason", "") or ""
        self.handled_rotate_request = d.get("handled_rotate_request", "") or ""
        self.schedule = d.get("schedule")
        nxt = _parse_dt(d.get("next_rotation") or d.get("next_daily_rotation"))
        if nxt and nxt > datetime.datetime.now():
            self.next_rotation = nxt

    # -- decision ----------------------------------------------------------
    def decide(self, cfg: dict, statuses: dict, order: list, countries: dict,
               current_override: str, now: datetime.datetime, rotate_request: str = ""):
        """Return (target, reason) to switch to, or None to keep the current choice.

        Pure: depends only on its inputs and lane state (and mutates the
        counters/streaks/rotation schedule the same way the daemon used to).
        """
        known = [t for t in order if t in statuses] or list(statuses)
        allowed = tl.allowed_tags(known, cfg.get("filter"), countries)
        allowed_alive = tl.alive_sorted(statuses, allowed)
        any_alive = tl.alive_sorted(statuses, known)

        # Schedule bookkeeping: re-plan when the schedule string changes; a
        # state file from the old daemon (no schedule recorded) keeps its time.
        schedule = (cfg.get("rotation") or {}).get("schedule", "daily")
        if self.schedule is None and self.next_rotation is not None:
            self.schedule = schedule
        if schedule != self.schedule or self.next_rotation is None:
            self.next_rotation = schedule_next(schedule, now)
            self.schedule = schedule
        due = self.next_rotation is not None and now >= self.next_rotation

        if not any_alive:
            self.status = "no_alive_nodes"
            return None

        self.violation = "" if allowed_alive else "no allowed node alive; using best alive node"
        pool = allowed_alive or any_alive
        best = pool[0]

        def alive(tag):
            s = statuses.get(tag)
            if s is None:
                return tag == OVPN_TAG  # not probed by observatory; trust it
            return bool(s.get("alive")) and s.get("delay", 0) > 0

        def delay(tag):
            s = statuses.get(tag) or {}
            return s["delay"] if s.get("alive") and s.get("delay", 0) > 0 else 999999

        # ---- Manual: hold the chosen node --------------------------------
        if cfg.get("mode") == "manual" and cfg.get("node"):
            if due:  # a pinned lane does not rotate; just keep the schedule moving
                self.next_rotation = schedule_next(schedule, now)
            target = cfg["node"]
            self.manual_override = True
            self.active_delay = delay(self.active_node) if self.active_node else 0
            if alive(target):
                self.consecutive_failures = 0
                self.status = "manual"
                if current_override != target or self.active_node != target:
                    return target, "manual_pin"
                return None
            self.consecutive_failures += 1
            self.status = "manual_target_dead"
            if not cfg.get("failover", True) or self.consecutive_failures < FAIL_THRESHOLD:
                if current_override != target:
                    return target, "manual_pin"
                return None
            if self.active_node != target and alive(self.active_node) and current_override == self.active_node:
                return None  # already failed over, stay until the pin comes back
            return best, f"manual_failover ('{target}' dead)"

        self.manual_override = False
        self.status = "auto"

        # ---- Initial / restore after xray restart ------------------------
        if not self.active_node or not current_override:
            reason = "initial_selection" if not self.active_node else "restore_override_after_xray_restart"
            keep = self.active_node and alive(self.active_node) and self.active_node in pool
            return (self.active_node if keep else best), reason

        # ---- Override drifted (routing hot-swap, someone else's `bo`) -----
        if current_override != self.active_node:
            keep = alive(self.active_node) and self.active_node in pool
            return (self.active_node if keep else best), f"restore_override (found '{current_override}')"

        # ---- Policy changed under us -------------------------------------
        if allowed_alive and self.active_node not in allowed and self.active_node != OVPN_TAG:
            return best, f"policy_filter ('{self.active_node}' not allowed)"

        # ---- Health of the active node -----------------------------------
        active_delay = delay(self.active_node)
        self.active_delay = active_delay
        if not alive(self.active_node):
            self.consecutive_failures += 1
            logger.warning(
                f"[{self.id}] Active node '{self.active_node}' is DEAD "
                f"(failure {self.consecutive_failures}/{FAIL_THRESHOLD})"
            )
            if self.consecutive_failures >= FAIL_THRESHOLD:
                return best, (f"failover_dead_node (failed {self.consecutive_failures} checks, "
                              f"target: {statuses[best]['delay']}ms)")
            return None
        self.consecutive_failures = 0

        # ---- Rotation (scheduled or requested) ---------------------------
        forced = bool(rotate_request) and rotate_request != self.handled_rotate_request
        if forced or due:
            if forced:
                self.handled_rotate_request = rotate_request
            if due:
                self.next_rotation = schedule_next(schedule, now)
            strategy = (cfg.get("rotation") or {}).get("strategy", "best")
            target = tl.pick(strategy, order, self.active_node, statuses, pool)
            kind = "manual_rotation" if forced else "scheduled_rotation"
            if target and target != self.active_node:
                return target, f"{kind}/{strategy} (current: {active_delay}ms, new: {target} at {delay(target)}ms)"
            logger.info(f"[{self.id}] {kind}: '{self.active_node}' kept (no better/other candidate)")
            return None

        # ---- Hysteresis / heavy degradation ------------------------------
        if active_delay > MAX_DEGRADED_RTT_MS and delay(best) < (MAX_DEGRADED_RTT_MS // 2):
            self.candidate_streak += 1
            if self.candidate_streak >= 2:
                return best, (f"severe_degradation (current: {active_delay}ms > {MAX_DEGRADED_RTT_MS}ms, "
                              f"candidate: {delay(best)}ms)")
            return None

        best_delay = delay(best)
        diff = active_delay - best_delay
        threshold_delay = active_delay * (1.0 - HYSTERESIS_RATIO)
        if best != self.active_node and best_delay <= threshold_delay and diff >= MIN_DIFF_MS:
            if self.candidate_tag == best:
                self.candidate_streak += 1
            else:
                self.candidate_tag = best
                self.candidate_streak = 1
            logger.info(
                f"[{self.id}] Candidate '{best}' ({best_delay}ms) beats active '{self.active_node}' "
                f"({active_delay}ms) by {diff}ms [streak {self.candidate_streak}/{STABILITY_TICKS}]"
            )
            if self.candidate_streak >= STABILITY_TICKS:
                return best, (f"hysteresis_switch (current: {active_delay}ms, candidate: {best_delay}ms, "
                              f"diff: -{diff}ms)")
        elif self.candidate_streak > 0:
            self.candidate_streak = 0
            self.candidate_tag = ""
        return None

    def switched(self, target: str, reason: str, delay_ms: int = 0) -> None:
        self.active_node = target
        self.active_delay = delay_ms
        self.last_switch_time = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        self.last_switch_reason = reason
        # A manual lane that failed over keeps counting failures of its dead
        # pin; resetting would make the next tick switch back to the dead node.
        if self.status != "manual_target_dead":
            self.consecutive_failures = 0
        self.candidate_tag = ""
        self.candidate_streak = 0


class StickyBalancer:
    def __init__(self):
        self.running = True
        self.channel = None
        self.obs_stub = None
        self.router_stub = None

        self.lanes: dict[str, Lane] = {tl.MAIN_LANE: Lane(tl.MAIN_LANE, BALANCER_TAG)}
        self.policy = tl.default_policy()
        self.meta: dict = {"nodes": {}}
        self.order: list[str] = []
        self._mtimes: dict[str, float] = {}
        self.policy_error = ""
        self.nodes_snapshot: dict = {}

        self.load_state()
        self.reload_inputs(force=True)

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    # Back-compat accessors for the global lane
    @property
    def active_node(self):
        return self.lanes[tl.MAIN_LANE].active_node

    def _handle_signal(self, signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        self.running = False

    # -- inputs ------------------------------------------------------------
    def _changed(self, path: Path) -> bool:
        try:
            m = path.stat().st_mtime
        except FileNotFoundError:
            m = 0.0
        if self._mtimes.get(str(path)) != m:
            self._mtimes[str(path)] = m
            return True
        return False

    def reload_inputs(self, force: bool = False) -> bool:
        """Re-read policy/meta/config when their mtime changes. True if policy changed."""
        policy_changed = False
        if self._changed(POLICY_FILE) or force:
            try:
                self.policy = tl.load_policy(POLICY_FILE)
                self.policy_error = ""
                policy_changed = True
                logger.info(f"Policy loaded from {POLICY_FILE} "
                            f"(global={self.policy['global']['mode']}, clients={len(self.policy['clients'])})")
            except Exception as e:
                self.policy_error = f"{POLICY_FILE}: {e}"
                logger.error(f"Bad policy file, keeping previous policy: {e}")
        if self._changed(META_FILE) or force:
            try:
                with open(META_FILE, "r", encoding="utf-8") as f:
                    self.meta = json.load(f)
            except FileNotFoundError:
                self.meta = {"nodes": {}}
            except Exception as e:
                logger.warning(f"Could not read {META_FILE}: {e}")
        if self._changed(XRAY_CONFIG_FILE) or force:
            try:
                with open(XRAY_CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                bal = next((b for b in cfg.get("routing", {}).get("balancers", [])
                            if b.get("tag") == BALANCER_TAG), None)
                self.order = list(bal.get("selector", [])) if bal else []
            except Exception:
                pass

        if policy_changed:
            wanted = {tl.MAIN_LANE} | {tl.lane_id_for_client(ip) for ip in self.policy["clients"]}
            for lid in list(self.lanes):
                if lid not in wanted:
                    del self.lanes[lid]
            for ip in self.policy["clients"]:
                lid = tl.lane_id_for_client(ip)
                if lid not in self.lanes:
                    self.lanes[lid] = Lane(lid, tl.client_balancer_tag(ip))
                    saved = self._saved_lanes.get(lid)
                    if saved:
                        self.lanes[lid].load_dict(saved)
        return policy_changed

    def lane_cfg(self, lane_id: str) -> dict:
        if lane_id == tl.MAIN_LANE:
            return self.policy["global"]
        return self.policy["clients"].get(lane_id.split(":", 1)[1], tl.default_lane())

    def countries(self) -> dict:
        tags = set(self.meta.get("nodes", {})) | set(self.order) | set(self.nodes_snapshot)
        return {t: tl.node_country(t, self.meta, self.policy) for t in tags}

    # -- state file --------------------------------------------------------
    def load_state(self):
        self._saved_lanes = {}
        if not STATE_FILE.exists():
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._saved_lanes = data.get("lanes") or {}
            main = self._saved_lanes.get(tl.MAIN_LANE) or data  # old flat format
            self.lanes[tl.MAIN_LANE].load_dict(main)
            lane = self.lanes[tl.MAIN_LANE]
            logger.info(
                f"Loaded state: active_node={lane.active_node}, next_rotation="
                f"{lane.next_rotation.strftime('%Y-%m-%d %H:%M:%S') if lane.next_rotation else '-'}"
            )
        except Exception as e:
            logger.warning(f"Failed to read state file: {e}")

    def save_state(self):
        main = self.lanes[tl.MAIN_LANE]
        state = {
            # flat keys of the global lane (backward compatible with old tunnelctl)
            "active_node": main.active_node,
            "active_delay_ms": main.active_delay,
            "last_switch_time": main.last_switch_time,
            "last_switch_reason": main.last_switch_reason,
            "next_daily_rotation": main.next_rotation.isoformat() if main.next_rotation else None,
            "manual_override": main.manual_override,
            # tz-aware: the host (tunnelctl) may run in another timezone than the container
            "updated_at": datetime.datetime.now().astimezone().isoformat(),
            "check_interval_sec": CHECK_INTERVAL_SEC,
            "policy_error": self.policy_error,
            "lanes": {lid: lane.to_dict() for lid, lane in self.lanes.items()},
            "nodes": self.nodes_snapshot,
        }
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = STATE_FILE.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            tmp_path.replace(STATE_FILE)
        except Exception as e:
            logger.warning(f"Failed to save state file: {e}")

    # -- gRPC --------------------------------------------------------------
    def connect_grpc(self):
        if self.channel is not None:
            try:
                self.channel.close()
            except Exception:
                pass
        self.channel = grpc.insecure_channel(API_SERVER)
        self.obs_stub = obs_pb2_grpc.ObservatoryServiceStub(self.channel)
        self.router_stub = router_pb2_grpc.RoutingServiceStub(self.channel)

    def set_override(self, lane: Lane, target: str, reason: str, delay_ms: int = 0) -> bool:
        try:
            self.router_stub.OverrideBalancerTarget(
                router_pb2.OverrideBalancerTargetRequest(balancerTag=lane.balancer, target=target)
            )
        except Exception as e:
            logger.error(f"[{lane.id}] Failed to set override to '{target}': {e}")
            return False
        old_node = lane.active_node
        lane.switched(target, reason, delay_ms)
        logger.info(
            f"[{lane.id}] ACTION: Override {lane.balancer} -> '{target}' (previous: '{old_node}'). Reason: {reason}"
        )
        return True

    # -- main loop ---------------------------------------------------------
    def run(self):
        logger.info(
            f"Starting Sticky Balancer daemon. Target API: {API_SERVER}, Balancer: {BALANCER_TAG}, "
            f"policy: {POLICY_FILE}"
        )
        logger.info(
            f"Hysteresis threshold: candidate must be {int(HYSTERESIS_RATIO*100)}% faster and >= {MIN_DIFF_MS}ms "
            f"for {STABILITY_TICKS} checks"
        )

        self.connect_grpc()

        while self.running:
            try:
                self.tick()
            except grpc.RpcError as rpc_err:
                logger.warning(f"gRPC error ({rpc_err.code()}): {rpc_err.details()}. Reconnecting...")
                time.sleep(3)
                self.connect_grpc()
            except Exception as e:
                logger.error(f"Unexpected error in loop: {e}", exc_info=True)

            # Sleep in small slices; a policy change (tunnelctl) wakes us early.
            end_sleep = time.time() + CHECK_INTERVAL_SEC
            while self.running and time.time() < end_sleep:
                time.sleep(0.5)
                if self._policy_touched():
                    break

        logger.info("Sticky Balancer daemon stopped.")

    def _policy_touched(self) -> bool:
        try:
            m = POLICY_FILE.stat().st_mtime
        except FileNotFoundError:
            m = 0.0
        return self._mtimes.get(str(POLICY_FILE)) != m

    def fetch_statuses(self) -> dict:
        obs_res = self.obs_stub.GetOutboundStatus(obs_pb2.GetOutboundStatusRequest())
        out = {}
        for s in obs_res.status.status:
            out[s.outbound_tag] = {
                "alive": bool(s.alive),
                "delay": int(s.delay),
                "last_error": s.last_error_reason,
                "last_seen": int(s.last_seen_time),
                "last_try": int(s.last_try_time),
            }
        return out

    def tick(self):
        now = datetime.datetime.now()
        self.reload_inputs()

        statuses = self.fetch_statuses()
        self.nodes_snapshot = statuses
        if not any(s["alive"] and s["delay"] > 0 for s in statuses.values()):
            logger.warning("No alive proxy nodes reported by observatory!")
            for lane in self.lanes.values():
                lane.status = "no_alive_nodes"
            self.save_state()
            return

        countries = self.countries()
        order = self.order or sorted(statuses)
        for lane in list(self.lanes.values()):
            self.tick_lane(lane, statuses, order, countries, now)
        self.save_state()

    def tick_lane(self, lane: Lane, statuses, order, countries, now):
        try:
            bal_res = self.router_stub.GetBalancerInfo(router_pb2.GetBalancerInfoRequest(tag=lane.balancer))
        except grpc.RpcError as e:
            if lane.id == tl.MAIN_LANE:
                raise
            # Client balancer not in the running config yet (tunnelctl apply pending)
            lane.status = "balancer_missing"
            logger.debug(f"[{lane.id}] balancer {lane.balancer} unavailable: {e.details()}")
            return
        current_override = bal_res.balancer.override.target if bal_res.balancer.override else ""

        cfg = self.lane_cfg(lane.id)

        # Raw `xray api bo` from outside (old-style tunnelctl): adopt it for the global lane
        if (lane.id == tl.MAIN_LANE and cfg.get("mode") != "manual" and current_override
                and lane.active_node and current_override != lane.active_node):
            logger.info(f"External override detected: '{current_override}' (was '{lane.active_node}')")
            lane.switched(current_override, "manual_or_external")
            lane.manual_override = True

        rotate_req = (self.policy.get("rotate_requests") or {}).get(lane.id, "")
        decision = lane.decide(cfg, statuses, order, countries, current_override, now, rotate_req)
        if decision:
            target, reason = decision
            delay_ms = (statuses.get(target) or {}).get("delay", 0)
            self.set_override(lane, target, reason, delay_ms)
        elif current_override and current_override == lane.active_node:
            s = statuses.get(lane.active_node) or {}
            lane.active_delay = s.get("delay", 0) if s.get("alive") else 0


def main():
    balancer = StickyBalancer()
    balancer.run()


if __name__ == "__main__":
    main()
