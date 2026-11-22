#!/usr/bin/env python3
"""Sticky Balancer Daemon for Xray Proxy.

Maintains sticky node affinity for Xray proxy balancer to prevent IP flapping,
session terminations, and anti-bot bans.

Behavior:
1. STICKY NODE: Keeps routing pinned to the active node via Xray API override.
2. FAILOVER (DEAD): Immediately fails over to the best alive node if the current
   node dies (fails health checks for N consecutive ticks).
3. HYSTERESIS: Switches during normal operation ONLY if an alternative node is
   substantially better (e.g. >= 50% lower RTT and >= 100ms delta) for multiple
   consecutive checks, or if current node heavily degrades (> 2500ms).
4. DAILY ROTATION: Smoothly rotates to the best available node once a day at a
   randomized time around 6:00 AM (e.g. 05:30 - 06:30).
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

# Add xray_grpc directory to python path
GRPC_DIR = Path(__file__).resolve().parent / "xray_grpc"
if str(GRPC_DIR) not in sys.path:
    sys.path.insert(0, str(GRPC_DIR))

import grpc
from app.observatory.command import command_pb2 as obs_pb2
from app.observatory.command import command_pb2_grpc as obs_pb2_grpc
from app.router.command import command_pb2 as router_pb2
from app.router.command import command_pb2_grpc as router_pb2_grpc

# Configuration defaults
API_SERVER = os.getenv("XRAY_API_SERVER", "127.0.0.1:10085")
BALANCER_TAG = os.getenv("XRAY_BALANCER_TAG", "proxy-auto")

CHECK_INTERVAL_SEC = float(os.getenv("STICKY_CHECK_INTERVAL_SEC", "15"))
ROTATION_HOUR = int(os.getenv("STICKY_ROTATION_HOUR", "6"))
ROTATION_WINDOW_MIN = int(os.getenv("STICKY_ROTATION_WINDOW_MIN", "30"))

FAIL_THRESHOLD = int(os.getenv("STICKY_FAIL_THRESHOLD", "2"))
HYSTERESIS_RATIO = float(os.getenv("STICKY_HYSTERESIS_RATIO", "0.5"))  # Candidate must be 50% faster
MIN_DIFF_MS = int(os.getenv("STICKY_MIN_DIFF_MS", "100"))            # and at least 100ms faster
STABILITY_TICKS = int(os.getenv("STICKY_STABILITY_TICKS", "3"))       # candidate must hold advantage
MAX_DEGRADED_RTT_MS = int(os.getenv("STICKY_MAX_DEGRADED_RTT_MS", "2500"))


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


class StickyBalancer:
    def __init__(self):
        self.running = True
        self.channel = None
        self.obs_stub = None
        self.router_stub = None

        self.active_node = ""
        self.active_delay = 0
        self.last_switch_time = ""
        self.last_switch_reason = ""
        self.consecutive_failures = 0

        # Candidate stability tracking
        self.candidate_tag = ""
        self.candidate_streak = 0

        self.next_rotation = compute_next_rotation(datetime.datetime.now())
        self.load_state()

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        self.running = False

    def load_state(self):
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.active_node = data.get("active_node", "")
                    self.last_switch_time = data.get("last_switch_time", "")
                    self.last_switch_reason = data.get("last_switch_reason", "")
                    raw_next = data.get("next_daily_rotation")
                    if raw_next:
                        try:
                            saved_dt = datetime.datetime.fromisoformat(raw_next)
                            if saved_dt > datetime.datetime.now():
                                self.next_rotation = saved_dt
                        except Exception:
                            pass
                logger.info(
                    f"Loaded state: active_node={self.active_node}, next_rotation={self.next_rotation.strftime('%Y-%m-%d %H:%M:%S')}"
                )
            except Exception as e:
                logger.warning(f"Failed to read state file: {e}")

    def save_state(self, manual_override: bool = False):
        state = {
            "active_node": self.active_node,
            "active_delay_ms": self.active_delay,
            "last_switch_time": self.last_switch_time,
            "last_switch_reason": self.last_switch_reason,
            "next_daily_rotation": self.next_rotation.isoformat(),
            "manual_override": manual_override,
            "updated_at": datetime.datetime.now().isoformat(),
        }
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = STATE_FILE.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            tmp_path.replace(STATE_FILE)
        except Exception as e:
            logger.warning(f"Failed to save state file: {e}")

    def connect_grpc(self):
        if self.channel is not None:
            try:
                self.channel.close()
            except Exception:
                pass
        self.channel = grpc.insecure_channel(API_SERVER)
        self.obs_stub = obs_pb2_grpc.ObservatoryServiceStub(self.channel)
        self.router_stub = router_pb2_grpc.RoutingServiceStub(self.channel)

    def set_override(self, target: str, reason: str):
        now_str = datetime.datetime.now().isoformat()
        try:
            self.router_stub.OverrideBalancerTarget(
                router_pb2.OverrideBalancerTargetRequest(balancerTag=BALANCER_TAG, target=target)
            )
            old_node = self.active_node
            self.active_node = target
            self.last_switch_time = now_str
            self.last_switch_reason = reason
            self.consecutive_failures = 0
            self.candidate_tag = ""
            self.candidate_streak = 0
            self.save_state()
            logger.info(
                f"ACTION: Override set to '{target}' (previous: '{old_node}'). Reason: {reason}"
            )
        except Exception as e:
            logger.error(f"Failed to set override to '{target}': {e}")

    def run(self):
        logger.info(
            f"Starting Sticky Balancer daemon. Target API: {API_SERVER}, Balancer: {BALANCER_TAG}"
        )
        logger.info(
            f"Daily rotation scheduled for ~{ROTATION_HOUR:02d}:00 (next: {self.next_rotation.strftime('%Y-%m-%d %H:%M:%S')})"
        )
        logger.info(
            f"Hysteresis threshold: candidate must be {int(HYSTERESIS_RATIO*100)}% faster and >= {MIN_DIFF_MS}ms for {STABILITY_TICKS} checks"
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

            end_sleep = time.time() + CHECK_INTERVAL_SEC
            while self.running and time.time() < end_sleep:
                time.sleep(0.5)

        logger.info("Sticky Balancer daemon stopped.")

    def tick(self):
        now = datetime.datetime.now()

        # 1. Fetch current balancer override status
        bal_res = self.router_stub.GetBalancerInfo(
            router_pb2.GetBalancerInfoRequest(tag=BALANCER_TAG)
        )
        current_override = bal_res.balancer.override.target if bal_res.balancer.override else ""

        # 2. Fetch observatory report
        obs_res = self.obs_stub.GetOutboundStatus(obs_pb2.GetOutboundStatusRequest())
        statuses = {s.outbound_tag: s for s in obs_res.status.status}

        alive_nodes = [s for s in statuses.values() if s.alive and s.delay > 0]
        alive_nodes.sort(key=lambda s: s.delay)

        if not alive_nodes:
            logger.warning("No alive proxy nodes reported by observatory!")
            return

        best_candidate = alive_nodes[0]

        # 3. Check if override was lost (e.g. Xray restarted) or changed externally
        if current_override and current_override != self.active_node:
            if not self.active_node:
                self.active_node = current_override
                logger.info(f"Adopted existing Xray override: '{current_override}'")
            else:
                logger.info(
                    f"External override detected: '{current_override}' (was '{self.active_node}')"
                )
                self.active_node = current_override
                self.last_switch_reason = "manual_or_external"
                self.last_switch_time = now.isoformat()
                self.save_state(manual_override=True)

        # 4. Initial startup: no active node set yet
        if not self.active_node or not current_override:
            reason = "initial_selection" if not self.active_node else "restore_override_after_xray_restart"
            target = self.active_node if (self.active_node in statuses and statuses[self.active_node].alive) else best_candidate.outbound_tag
            self.set_override(target, reason)
            return

        # 5. Evaluate health of current active node
        active_status = statuses.get(self.active_node)
        is_active_alive = active_status.alive if active_status else False
        active_delay = active_status.delay if (active_status and active_status.delay > 0) else 999999
        self.active_delay = active_delay

        if not is_active_alive:
            self.consecutive_failures += 1
            logger.warning(
                f"Active node '{self.active_node}' is DEAD (failure {self.consecutive_failures}/{FAIL_THRESHOLD})"
            )
            if self.consecutive_failures >= FAIL_THRESHOLD:
                # Urgent failover!
                self.set_override(
                    best_candidate.outbound_tag,
                    f"failover_dead_node (failed {self.consecutive_failures} checks, target: {best_candidate.delay}ms)",
                )
            return
        else:
            self.consecutive_failures = 0

        # 6. Check Daily Rotation around 6:00 AM
        if now >= self.next_rotation:
            self.next_rotation = compute_next_rotation(now)
            logger.info(
                f"DAILY ROTATION WINDOW REACHED! Next rotation set for {self.next_rotation.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            if best_candidate.outbound_tag != self.active_node:
                self.set_override(
                    best_candidate.outbound_tag,
                    f"daily_rotation (current: {active_delay}ms, best: {best_candidate.outbound_tag} at {best_candidate.delay}ms)",
                )
            else:
                logger.info(
                    f"Daily rotation check: active node '{self.active_node}' is already the best alive node ({active_delay}ms). Keeping it."
                )
            return

        # 7. Check Hysteresis / Heavy Degradation
        # Rule A: Extreme degradation (current node > MAX_DEGRADED_RTT_MS, candidate normal)
        if active_delay > MAX_DEGRADED_RTT_MS and best_candidate.delay < (MAX_DEGRADED_RTT_MS // 2):
            self.candidate_streak += 1
            if self.candidate_streak >= 2:
                self.set_override(
                    best_candidate.outbound_tag,
                    f"severe_degradation (current: {active_delay}ms > {MAX_DEGRADED_RTT_MS}ms, candidate: {best_candidate.delay}ms)",
                )
            return

        # Rule B: Standard Hysteresis check
        # Candidate must be substantially better:
        # delay <= active_delay * (1 - HYSTERESIS_RATIO) AND (active_delay - candidate_delay) >= MIN_DIFF_MS
        diff = active_delay - best_candidate.delay
        threshold_delay = active_delay * (1.0 - HYSTERESIS_RATIO)

        if best_candidate.outbound_tag != self.active_node and best_candidate.delay <= threshold_delay and diff >= MIN_DIFF_MS:
            if self.candidate_tag == best_candidate.outbound_tag:
                self.candidate_streak += 1
            else:
                self.candidate_tag = best_candidate.outbound_tag
                self.candidate_streak = 1

            logger.info(
                f"Candidate '{best_candidate.outbound_tag}' ({best_candidate.delay}ms) beats active '{self.active_node}' "
                f"({active_delay}ms) by {diff}ms [streak {self.candidate_streak}/{STABILITY_TICKS}]"
            )

            if self.candidate_streak >= STABILITY_TICKS:
                self.set_override(
                    best_candidate.outbound_tag,
                    f"hysteresis_switch (current: {active_delay}ms, candidate: {best_candidate.delay}ms, diff: -{diff}ms)",
                )
        else:
            if self.candidate_streak > 0:
                self.candidate_streak = 0
                self.candidate_tag = ""

        # Update saved state
        self.save_state()


def main():
    balancer = StickyBalancer()
    balancer.run()


if __name__ == "__main__":
    main()
