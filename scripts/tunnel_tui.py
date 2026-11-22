"""Curses TUI for tunnelctl (stdlib only). Started by `tunnelctl tui` / bare `tunnelctl`.

Tabs: 1 Nodes · 2 Clients · 3 Rules. All actions go through the same act_*
functions as the CLI, so the TUI is only a view + key bindings.
"""

from __future__ import annotations

import contextlib
import curses
import datetime as dt
import io
import subprocess
import threading
import time

import tunnel_lib as tl

REFRESH_SEC = 2.0
TABS = ("Nodes", "Clients", "Rules")

HELP = {
    0: "↵ use  n next  N next-in-country  a auto  r rotate  p pin→client  o set country  "
       "c view country  A alive-only  s sort  ? help  q quit",
    1: "↵ actions  n next  r rotate  u unpin  e rename  A all-known  ? help  q quit",
    2: "↵ edit lane  a auto  r rotate  ? help  q quit",
}

HELP_TEXT = """\
tunnelctl TUI

Global keys:  1/2/3 or Tab — switch tab    ↑↓ PgUp PgDn Home End — move    q — quit
              R — reload now               ? — this help

Nodes:    Enter/u  pin ALL traffic (global lane) to the selected node (manual mode)
          n        next available node allowed by the global rules
          N        next available node in the selected node's country
          a        back to automatic sticky selection     r  rotate now
          p        pin a client to the selected node      o  fix the node's country
          c        show only some countries (empty = all)  A  alive only   s  sort

Clients:  Enter    action menu (pin to node / country, own lane, rules, rename, unpin)
          n / r    next node / rotate now for the pinned client   u  unpin   e  rename
          A        also show known idle clients

Rules:    Enter    edit the lane: all / ONLY / EXCEPT countries, rotation schedule
                   (off, daily, daily@H, 30m, 6h…), strategy (best / next / random)

Changes are written to config/tunnel_policy.json; the balancer daemon applies
them within a second. Pinning a NEW client hot-swaps xray routing (no restart).
"""


class LogFeeder(threading.Thread):
    """`docker logs -f` reader feeding a ClientTracker (never re-reads the whole log)."""

    def __init__(self, ctl, since="15m"):
        super().__init__(daemon=True)
        self.ctl = ctl
        self.since = since
        self.tracker = tl.ClientTracker()
        self.lock = threading.Lock()
        self.error = ""
        self.proc = None
        self.stop = False

    def run(self):
        while not self.stop:
            try:
                self.proc = subprocess.Popen(
                    ["docker", "logs", "-f", "--since", self.since, self.ctl.XRAY_CONTAINER],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)
                self.error = ""
                for line in self.proc.stdout:
                    if self.stop:
                        break
                    if " accepted " in line or " rejected " in line:
                        with self.lock:
                            self.tracker.feed(line)
                self.proc.wait()
            except Exception as e:
                self.error = f"client log: {e}"
            if not self.stop:
                self.since = "1m"
                time.sleep(3)

    def snapshot(self):
        with self.lock:
            t = tl.ClientTracker()
            t.clients = {k: dict(v, hosts=dict(v["hosts"]), outbounds=dict(v["outbounds"]))
                         for k, v in self.tracker.clients.items()}
        return t

    def close(self):
        self.stop = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


class App:
    def __init__(self, ctl):
        self.ctl = ctl
        self.tab = 0
        self.sel = [0, 0, 0]
        self.top = [0, 0, 0]
        self.msg = ""
        self.msg_until = 0.0
        self.snap = None
        self.snap_at = 0.0
        self.view_countries: list[str] = []
        self.alive_only = False
        self.sort = "order"
        self.show_all_clients = False
        self.feeder = LogFeeder(ctl)
        self.rows_cache: list = []
        self.client_cache = ([], 0.0)

    # ------------------------------------------------------------------ util
    def say(self, text, sec=5):
        self.msg = text.replace("\n", " ").strip()
        self.msg_until = time.time() + sec

    def act(self, fn, *a, ok=None, **kw):
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                res = fn(*a, **kw)
            extra = err.getvalue().strip()
            if ok:
                self.say((ok(res) if callable(ok) else ok) + (f"  ({extra})" if extra else ""))
            self.refresh(force=True)
            return res
        except (self.ctl.TunnelError, ValueError) as e:
            self.say(f"✗ {e}", 8)
        except Exception as e:  # never crash the panel
            self.say(f"✗ {type(e).__name__}: {e}", 8)
        return None

    def refresh(self, force=False):
        if force or time.time() - self.snap_at > REFRESH_SEC or self.snap is None:
            try:
                self.snap = self.ctl.Snapshot()
            except Exception as e:
                self.say(f"✗ {e}", 8)
            self.snap_at = time.time()

    def flag(self, iso):
        return self.ctl.flag_txt(iso) if not self.ctl.ASCII else ""

    def put(self, y, x, text, attr=0, width=None):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        width = min(width if width is not None else w - x, w - x - (1 if y == h - 1 else 0))
        if width <= 0:
            return
        try:
            self.scr.addstr(y, x, self.ctl.fit(str(text), width), attr)
        except curses.error:
            pass

    # ------------------------------------------------------------- dialogs
    def prompt(self, label, initial=""):
        h, w = self.scr.getmaxyx()
        buf = list(initial)
        curses.curs_set(1)
        self.scr.timeout(-1)  # main loop polls with a timeout; a prompt must block
        try:
            while True:
                line = f"{label}{''.join(buf)}"
                self.put(h - 1, 0, line, curses.A_REVERSE, w - 1)
                try:
                    self.scr.move(h - 1, min(w - 2, self.ctl.disp_width(line)))
                except curses.error:
                    pass
                ch = self.scr.get_wch()
                if ch in ("\n", "\r", curses.KEY_ENTER):
                    return "".join(buf).strip()
                if ch == "\x1b":
                    return None
                if ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    if buf:
                        buf.pop()
                elif ch == "\x15":  # ^U
                    buf = []
                elif isinstance(ch, str) and ch.isprintable():
                    buf.append(ch)
        finally:
            curses.curs_set(0)
            self.scr.timeout(500)

    def menu(self, title, items, current=0):
        """items: list[(label, value)] -> value or None. Type to filter."""
        sel, query = current, ""
        while True:
            shown = [it for it in items if query.lower() in it[0].lower()] if query else items
            h, w = self.scr.getmaxyx()
            mw = min(w - 4, max([self.ctl.disp_width(lbl) for lbl, _ in items] + [len(title) + 4, 30]) + 4)
            mh = min(h - 4, len(shown) + 4)
            y0, x0 = max(1, (h - mh) // 2), max(1, (w - mw) // 2)
            win = curses.newwin(mh, mw, y0, x0)
            win.keypad(True)
            win.erase()
            win.box()
            hdr = f" {title} " + (f"[{query}] " if query else "")
            try:
                win.addstr(0, 2, self.ctl.fit(hdr, mw - 4), curses.A_BOLD)
            except curses.error:
                pass
            vis = mh - 4
            sel = max(0, min(sel, len(shown) - 1))
            top = max(0, sel - vis + 1)
            for i, (lbl, _) in enumerate(shown[top:top + vis]):
                attr = curses.A_REVERSE if top + i == sel else 0
                try:
                    win.addstr(2 + i, 2, self.ctl.fit(lbl, mw - 4), attr)
                except curses.error:
                    pass
            if not shown:
                win.addstr(2, 2, "(nothing matches)")
            win.refresh()
            ch = win.get_wch()
            if ch in ("\x1b",):
                return None
            if ch in ("\n", "\r", curses.KEY_ENTER):
                return shown[sel][1] if shown else None
            if ch == curses.KEY_UP:
                sel = max(0, sel - 1)
            elif ch == curses.KEY_DOWN:
                sel = min(len(shown) - 1, sel + 1)
            elif ch == curses.KEY_PPAGE:
                sel = max(0, sel - vis)
            elif ch == curses.KEY_NPAGE:
                sel = min(len(shown) - 1, sel + vis)
            elif ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                query = query[:-1]
            elif isinstance(ch, str) and ch.isprintable():
                query += ch
                sel = 0

    def pick_node(self, title, countries=None):
        s = self.snap
        tags = self.ctl.candidates_in(s, countries) if countries else s.tags
        tags = sorted(tags, key=lambda t: (not s.alive(t), s.delay(t) or 10 ** 9))
        items = [(f"{t:10} {self.flag(s.country(t))} {s.country(t)}  {self.ctl.fmt_delay(s.delay(t)):>7}  "
                  f"{'alive' if s.alive(t) else 'dead '}  {s.name(t) or s.host(t)}", t) for t in tags]
        return self.menu(title, items)

    def pick_countries(self, title, preset=None):
        """Multi-select countries: Space toggles, Enter confirms."""
        s = self.snap
        cmap = s.countries_map()
        ccs = sorted(set(cmap.values()), key=lambda c: (c == "??", c))
        chosen = set(preset or [])
        sel = 0
        while True:
            items = []
            for c in ccs:
                tags = [t for t in s.tags if cmap[t] == c]
                alive = sum(1 for t in tags if s.alive(t))
                items.append((f"[{'x' if c in chosen else ' '}] {self.flag(c)} {c}  {tl.country_name(c):16} "
                              f"{alive}/{len(tags)} alive", c))
            h, w = self.scr.getmaxyx()
            mw, mh = min(w - 4, 56), min(h - 4, len(items) + 5)
            y0, x0 = max(1, (h - mh) // 2), max(1, (w - mw) // 2)
            win = curses.newwin(mh, mw, y0, x0)
            win.keypad(True)
            win.erase()
            win.box()
            try:
                win.addstr(0, 2, self.ctl.fit(f" {title} ", mw - 4), curses.A_BOLD)
                win.addstr(mh - 1, 2, self.ctl.fit(" Space toggle · Enter ok · Esc cancel ", mw - 4))
            except curses.error:
                pass
            vis = mh - 4
            top = max(0, sel - vis + 1)
            for i, (lbl, _) in enumerate(items[top:top + vis]):
                try:
                    win.addstr(2 + i, 2, self.ctl.fit(lbl, mw - 4), curses.A_REVERSE if top + i == sel else 0)
                except curses.error:
                    pass
            win.refresh()
            ch = win.get_wch()
            if ch == "\x1b":
                return None
            if ch in ("\n", "\r", curses.KEY_ENTER):
                return [c for c in ccs if c in chosen]
            if ch == " " and items:
                c = items[sel][1]
                chosen.symmetric_difference_update({c})
            elif ch == curses.KEY_UP:
                sel = max(0, sel - 1)
            elif ch == curses.KEY_DOWN:
                sel = min(len(items) - 1, sel + 1)

    def pick_client(self, title):
        rows = self.client_rows()
        items = [(f"{r['ip']:15} {r.get('name') or '':16} {self.ctl.describe_pin(r['pin'])}", r["ip"]) for r in rows]
        items.append(("… enter an IP manually", "__manual__"))
        ip = self.menu(title, items)
        if ip == "__manual__":
            ip = self.prompt("Client IP: ")
        return ip or None

    def show_text(self, text):
        h, w = self.scr.getmaxyx()
        lines = text.splitlines()
        mh, mw = min(h - 2, len(lines) + 2), min(w - 2, max(len(ln) for ln in lines) + 4)
        win = curses.newwin(mh, mw, max(0, (h - mh) // 2), max(0, (w - mw) // 2))
        win.box()
        for i, ln in enumerate(lines[: mh - 2]):
            try:
                win.addstr(1 + i, 2, ln[: mw - 4])
            except curses.error:
                pass
        win.refresh()
        win.getch()

    # ---------------------------------------------------------------- data
    def node_rows(self):
        s = self.snap
        tags = list(s.tags)
        if self.view_countries:
            tags = self.ctl.candidates_in(s, self.view_countries)
        if self.alive_only:
            tags = [t for t in tags if s.alive(t)]
        if self.sort == "delay":
            tags.sort(key=lambda t: s.delay(t) or 10 ** 9)
        elif self.sort == "country":
            tags.sort(key=lambda t: (s.country(t), s.delay(t) or 10 ** 9))
        return tags

    def client_rows(self):
        rows, at = self.client_cache
        if time.time() - at > REFRESH_SEC:
            rows = self.ctl.client_rows(self.snap, self.feeder.snapshot(), self.show_all_clients)
            self.client_cache = (rows, time.time())
        return rows

    def lane_rows(self):
        s = self.snap
        rows = [(None, s.policy["global"])]
        rows += [(ip, cfg) for ip, cfg in sorted(s.policy["clients"].items(), key=lambda kv: tl.ip_sort_key(kv[0]))]
        return rows

    # ---------------------------------------------------------------- draw
    def draw(self):
        s = self.snap
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        C = self.colors

        act = s.active()
        age = s.daemon_age()
        g = s.policy["global"]
        head = (f" tunnelctl │ global → {act or '?'} {self.flag(s.country(act)) if act else ''} "
                f"{s.country(act) if act else ''} {self.ctl.fmt_delay(s.delay(act)) if act else ''} │ "
                f"{'MANUAL' if g['mode'] == 'manual' else 'auto'} │ {self.ctl.describe_filter(g['filter'])} │ "
                f"rot {g['rotation']['schedule']}/{g['rotation']['strategy']}")
        self.put(0, 0, head, C["head"], w)
        hb = "daemon ?" if age is None else (f"daemon {int(age)}s" + (" STALE" if age > 90 else ""))
        self.put(0, max(0, w - len(hb) - 1), hb, C["bad"] if age is None or age > 90 else C["head"])

        x = 1
        for i, name in enumerate(TABS):
            label = f" {i + 1} {name} "
            self.put(1, x, label, C["tab_on"] if i == self.tab else C["tab"])
            x += len(label) + 1
        alive = sum(1 for t in s.tags if s.alive(t))
        info = f"{alive}/{len(s.tags)} alive · {len(s.policy['clients'])} pinned"
        self.put(1, max(x + 1, w - len(info) - 1), info, C["dim"])

        body_top, body_h = 3, h - 5
        if self.tab == 0:
            self.draw_nodes(body_top, body_h)
        elif self.tab == 1:
            self.draw_clients(body_top, body_h)
        else:
            self.draw_rules(body_top, body_h)

        if time.time() < self.msg_until and self.msg:
            self.put(h - 2, 0, " " + self.msg, C["msg"], w)
        elif self.feeder.error:
            self.put(h - 2, 0, " " + self.feeder.error, C["bad"], w)
        self.put(h - 1, 0, " " + HELP[self.tab], C["dim"], w)
        scr.refresh()

    def _scroll(self, n, body_h):
        t = self.tab
        self.sel[t] = max(0, min(self.sel[t], n - 1))
        if self.sel[t] < self.top[t]:
            self.top[t] = self.sel[t]
        if self.sel[t] >= self.top[t] + body_h:
            self.top[t] = self.sel[t] - body_h + 1
        return self.top[t]

    def draw_nodes(self, y0, bh):
        s, C = self.snap, self.colors
        tags = self.node_rows()
        self.rows_cache = tags
        allowed = set(self.ctl.candidates_in(s, None, s.policy["global"]))
        cols = [(1, " "), (10, "TAG"), (2, ""), (3, "CC"), (13, "COUNTRY"), (7, "RTT"), (6, "STATE"),
                (14, "USED BY"), (0, "NAME / HOST")]
        x = 1
        for wdt, title in cols:
            self.put(y0, x, title, C["hdr"], wdt or None)
            x += wdt + 1
        flt = []
        if self.view_countries:
            flt.append("view: " + ",".join(self.view_countries))
        if self.alive_only:
            flt.append("alive only")
        flt.append(f"sort: {self.sort}")
        self.put(y0, max(1, self.scr.getmaxyx()[1] - 40), " · ".join(flt), C["dim"], 39)
        bh -= 1
        top = self._scroll(len(tags), bh)
        act = s.active()
        for i, t in enumerate(tags[top:top + bh]):
            y = y0 + 1 + i
            selected = top + i == self.sel[0]
            alive = s.alive(t)
            base = C["sel"] if selected else 0
            col = C["ok"] if alive else (C["bad"] if alive is False else C["dim"])
            if t not in allowed:
                col = C["dim"]
            cells = ["▶" if t == act else ("·" if t not in allowed else " "), t, self.flag(s.country(t)),
                     s.country(t), tl.country_name(s.country(t)), self.ctl.fmt_delay(s.delay(t)),
                     "alive" if alive else ("dead" if alive is False else "n/a"),
                     ",".join(s.lane_users(t)), s.name(t) or s.host(t)]
            if selected:
                self.put(y, 0, "", base)
                self.scr.chgat(y, 0, -1, base)
            x = 1
            for (wdt, _), cell in zip(cols, cells):
                attr = base | (C["act"] if t == act and wdt in (1, 10) else col if wdt in (7, 6) else 0)
                self.put(y, x, cell, attr, wdt or None)
                x += wdt + 1
        if not tags:
            self.put(y0 + 2, 2, "(no nodes match)", C["dim"])

    def draw_clients(self, y0, bh):
        s, C = self.snap, self.colors
        rows = self.client_rows()
        self.rows_cache = rows
        cols = [(15, "IP"), (16, "NAME"), (5, "SEEN"), (5, "CONN"), (10, "VIA"), (2, ""), (18, "PIN"),
                (10, "LANE NODE"), (0, "TOP HOSTS")]
        x = 1
        for wdt, title in cols:
            self.put(y0, x, title, C["hdr"], wdt or None)
            x += wdt + 1
        bh -= 1
        top = self._scroll(len(rows), bh)
        for i, r in enumerate(rows[top:top + bh]):
            y = y0 + 1 + i
            selected = top + i == self.sel[1]
            base = C["sel"] if selected else 0
            if selected:
                self.put(y, 0, "", base)
                self.scr.chgat(y, 0, -1, base)
            lane = s.lanes.get(tl.lane_id_for_client(r["ip"])) or {}
            via = r.get("outbound") or ""
            cells = [r["ip"], r.get("name") or "", self.ctl.ago(r.get("last_seen")), str(r.get("conns", 0)), via,
                     self.flag(s.country(via)) if via in s.tags else "", self.ctl.describe_pin(r["pin"]),
                     lane.get("active_node", "") if r["pin"] else "", ", ".join(r.get("top_hosts", [])[:4])]
            x = 1
            for (wdt, _), cell in zip(cols, cells):
                attr = base | (C["act"] if r["pin"] and wdt == 18 else 0)
                self.put(y, x, cell, attr, wdt or None)
                x += wdt + 1
        if not rows:
            self.put(y0 + 2, 2, "(no client traffic seen yet — waiting for xray access log)", C["dim"])

    def draw_rules(self, y0, bh):
        s, C = self.snap, self.colors
        rows = self.lane_rows()
        self.rows_cache = rows
        top = self._scroll(len(rows), max(1, bh // 3))
        y = y0
        for i, (ip, cfg) in enumerate(rows[top:]):
            if y + 2 >= y0 + bh:
                break
            selected = top + i == self.sel[2]
            base = C["sel"] if selected else 0
            lane = s.lanes.get(tl.lane_id_for_client(ip) if ip else tl.MAIN_LANE) or {}
            node = lane.get("active_node", "")
            title = "GLOBAL (all other clients)" if ip is None else f"{ip}  {s.client_label(ip)}"
            self.put(y, 1, f"{'▸' if selected else ' '} {title}", base | curses.A_BOLD)
            self.put(y + 1, 4, f"mode: {'MANUAL → ' + str(cfg.get('node')) if cfg['mode'] == 'manual' else 'auto'}"
                               f"   now: {node or '—'} {self.flag(s.country(node)) if node else ''} "
                               f"{self.ctl.fmt_delay(s.delay(node)) if node else ''}   status: {lane.get('status', '—')}",
                     base)
            viol = lane.get("policy_violation")
            self.put(y + 2, 4, f"rules: {self.ctl.describe_filter(cfg['filter'])}   rotation: "
                               f"{cfg['rotation']['schedule']} / {cfg['rotation']['strategy']}   next: "
                               f"{(lane.get('next_rotation') or '—')[:16]}" + (f"   ⚠ {viol}" if viol else ""),
                     base | (C["bad"] if viol else 0))
            y += 4

    # --------------------------------------------------------------- input
    def current(self):
        rows = self.rows_cache
        if not rows:
            return None
        return rows[max(0, min(self.sel[self.tab], len(rows) - 1))]

    def lane_menu(self, client):
        s = self.snap
        cfg = s.lane_cfg(client)
        who = client or "global"
        choice = self.menu(f"Lane: {who}", [
            ("Pin to a node…", "node"),
            ("Pin to the best node of a country…", "country_pin"),
            ("Automatic (sticky) selection", "auto"),
            ("Next available node", "next"),
            ("Rotate now", "rotate"),
            ("Rules: all countries", "f_all"),
            ("Rules: ONLY these countries…", "f_only"),
            ("Rules: all EXCEPT these countries…", "f_except"),
            (f"Rotation schedule… (now {cfg['rotation']['schedule']})", "sched"),
            (f"Rotation strategy… (now {cfg['rotation']['strategy']})", "strat"),
        ] + ([("Rename client…", "rename"), ("Unpin client", "unpin")] if client else []))
        self.do_lane(choice, client)

    def do_lane(self, choice, client):
        c = self.ctl
        cfg = self.snap.lane_cfg(client)
        who = client or "global"
        if choice == "node":
            t = self.pick_node(f"{who}: pin to node")
            if t:
                self.act(c.act_use, t, client=client, ok=lambda r: f"{who} → {r}")
        elif choice == "country_pin":
            cc = self.pick_countries(f"{who}: countries")
            if cc:
                if client:
                    self.act(c.act_filter, "only", cc, client)
                    self.act(c.act_auto, client, ok=f"{who}: sticky inside {' '.join(cc)}")
                else:
                    self.act(c.act_use, None, cc, ok=lambda r: f"global → {r} (best in {' '.join(cc)})")
        elif choice == "auto":
            self.act(c.act_auto, client, ok=f"{who}: automatic")
        elif choice == "next":
            self.act(c.act_next, None, client, ok=lambda r: f"{who} → {r}")
        elif choice == "rotate":
            self.act(c.act_rotate, client, ok=f"{who}: rotation requested")
        elif choice == "f_all":
            self.act(c.act_filter, "all", [], client, ok=f"{who}: all countries")
        elif choice in ("f_only", "f_except"):
            mode = choice[2:]
            preset = cfg["filter"]["countries"] if cfg["filter"]["mode"] == mode else []
            cc = self.pick_countries(f"{who}: {'ONLY' if mode == 'only' else 'EXCEPT'}", preset)
            if cc:
                self.act(c.act_filter, mode, cc, client, ok=lambda r: f"{who}: {c.describe_filter(r['filter'])}")
        elif choice == "sched":
            v = self.prompt("Schedule (off | daily | daily@H | 30m | 6h | 1d): ", cfg["rotation"]["schedule"])
            if v:
                self.act(c.act_rotation, v, None, client, ok=f"{who}: rotation {v}")
        elif choice == "strat":
            v = self.menu("Strategy", [("best — lowest latency", "best"), ("next — next in list", "next"),
                                       ("random — any alive", "random")])
            if v:
                self.act(c.act_rotation, None, v, client, ok=f"{who}: strategy {v}")
        elif choice == "rename" and client:
            v = self.prompt(f"Name for {client}: ", self.snap.client_label(client) if self.snap.client_label(client) != client else "")
            if v is not None:
                self.act(c.act_alias, client, v, ok="renamed")
        elif choice == "unpin" and client:
            self.act(c.act_unpin, client, ok=f"{client} unpinned")

    def client_menu(self, row):
        ip = row["ip"]
        if row["pin"]:
            return self.lane_menu(ip)
        c = self.ctl
        choice = self.menu(f"Client {ip} {row.get('name') or ''}", [
            ("Pin to a node…", "node"),
            ("Pin to a country…", "country"),
            ("Own sticky lane (global rules)", "own"),
            ("Rename…", "rename"),
        ])
        if choice == "node":
            t = self.pick_node(f"{ip}: pin to node")
            if t:
                self.act(c.act_pin, ip, t, None, row.get("name") or None, ok=lambda r: f"{ip} pinned: {r}")
        elif choice == "country":
            cc = self.pick_countries(f"{ip}: pin to countries")
            if cc:
                self.act(c.act_pin, ip, None, cc, row.get("name") or None, ok=lambda r: f"{ip} pinned: {r}")
        elif choice == "own":
            self.act(c.act_pin, ip, "auto", None, row.get("name") or None, ok=lambda r: f"{ip} pinned: {r}")
        elif choice == "rename":
            v = self.prompt(f"Name for {ip}: ", row.get("name") or "")
            if v is not None:
                self.act(c.act_alias, ip, v, ok="renamed")

    def handle(self, ch) -> bool:
        c = self.ctl
        n = len(self.rows_cache)
        t = self.tab
        h = self.scr.getmaxyx()[0]
        if ch in ("q", "Q"):
            return False
        if ch in ("1", "2", "3"):
            self.tab = int(ch) - 1
        elif ch == "\t":
            self.tab = (self.tab + 1) % len(TABS)
        elif ch == curses.KEY_BTAB:
            self.tab = (self.tab - 1) % len(TABS)
        elif ch in (curses.KEY_UP, "k"):
            self.sel[t] = max(0, self.sel[t] - 1)
        elif ch in (curses.KEY_DOWN, "j"):
            self.sel[t] = min(max(0, n - 1), self.sel[t] + 1)
        elif ch == curses.KEY_PPAGE:
            self.sel[t] = max(0, self.sel[t] - (h - 7))
        elif ch == curses.KEY_NPAGE:
            self.sel[t] = min(max(0, n - 1), self.sel[t] + (h - 7))
        elif ch == curses.KEY_HOME:
            self.sel[t] = 0
        elif ch == curses.KEY_END:
            self.sel[t] = max(0, n - 1)
        elif ch == "?":
            self.show_text(HELP_TEXT)
        elif ch == "R":
            self.refresh(force=True)
            self.client_cache = ([], 0.0)
        elif ch == curses.KEY_RESIZE:
            pass
        elif t == 0:
            self.handle_nodes(ch)
        elif t == 1:
            self.handle_clients(ch)
        else:
            self.handle_rules(ch)
        return True

    def handle_nodes(self, ch):
        c, s = self.ctl, self.snap
        tag = self.current()
        if ch in ("\n", "\r", curses.KEY_ENTER, "u") and tag:
            self.act(c.act_use, tag, ok=lambda r: f"global → {r} (manual; 'a' for auto)")
        elif ch == "n":
            self.act(c.act_next, None, ok=lambda r: f"global → {r}")
        elif ch == "N" and tag:
            cc = s.country(tag)
            self.act(c.act_next, [cc], ok=lambda r: f"global → {r} (next in {cc})")
        elif ch == "a":
            self.act(c.act_auto, ok="global: automatic")
        elif ch == "r":
            self.act(c.act_rotate, ok="global: rotation requested")
        elif ch == "p" and tag:
            ip = self.pick_client(f"Pin which client to {tag}?")
            if ip:
                if ip in s.policy["clients"]:
                    self.act(c.act_use, tag, client=ip, ok=lambda r: f"{ip} → {r}")
                else:
                    self.act(c.act_pin, ip, tag, None, ok=lambda r: f"{ip} pinned: {r}")
        elif ch == "o" and tag:
            v = self.prompt(f"Country for {tag} (ISO code, 'auto' = detect): ", s.country(tag))
            if v:
                self.act(c.act_country, tag, v, ok=f"{tag}: country {v.upper()}")
        elif ch == "c":
            cc = self.pick_countries("Show countries (none = all)", self.view_countries)
            if cc is not None:
                self.view_countries = cc
                self.sel[0] = 0
        elif ch == "A":
            self.alive_only = not self.alive_only
        elif ch == "s":
            order = ("order", "delay", "country")
            self.sort = order[(order.index(self.sort) + 1) % len(order)]

    def handle_clients(self, ch):
        c = self.ctl
        row = self.current()
        if ch == "A":
            self.show_all_clients = not self.show_all_clients
            self.client_cache = ([], 0.0)
            return
        if not row:
            return
        ip = row["ip"]
        if ch in ("\n", "\r", curses.KEY_ENTER, "p"):
            self.client_menu(row)
        elif ch == "n":
            if row["pin"]:
                self.act(c.act_next, None, ip, ok=lambda r: f"{ip} → {r}")
            else:
                self.say("client uses the global lane — pin it first (Enter)")
        elif ch == "r":
            if row["pin"]:
                self.act(c.act_rotate, ip, ok=f"{ip}: rotation requested")
            else:
                self.say("client uses the global lane — pin it first (Enter)")
        elif ch == "u" and row["pin"]:
            self.act(c.act_unpin, ip, ok=f"{ip} unpinned")
        elif ch == "e":
            v = self.prompt(f"Name for {ip}: ", row.get("name") or "")
            if v is not None:
                self.act(c.act_alias, ip, v, ok="renamed")
        self.client_cache = ([], 0.0)

    def handle_rules(self, ch):
        row = self.current()
        if not row:
            return
        ip, _ = row
        if ch in ("\n", "\r", curses.KEY_ENTER):
            self.lane_menu(ip)
        elif ch == "a":
            self.do_lane("auto", ip)
        elif ch == "r":
            self.do_lane("rotate", ip)

    # ---------------------------------------------------------------- main
    def main(self, scr):
        self.scr = scr
        curses.curs_set(0)
        scr.keypad(True)
        scr.timeout(500)
        curses.use_default_colors()
        pairs = {"head": (curses.COLOR_BLACK, curses.COLOR_CYAN), "tab_on": (curses.COLOR_BLACK, curses.COLOR_YELLOW),
                 "ok": (curses.COLOR_GREEN, -1), "bad": (curses.COLOR_RED, -1), "act": (curses.COLOR_YELLOW, -1),
                 "hdr": (curses.COLOR_CYAN, -1), "msg": (curses.COLOR_BLACK, curses.COLOR_GREEN)}
        self.colors = {"tab": curses.A_DIM, "dim": curses.A_DIM, "sel": curses.A_REVERSE}
        if curses.has_colors():
            curses.start_color()
            for i, (k, (fg, bg)) in enumerate(pairs.items(), 1):
                try:
                    curses.init_pair(i, fg, bg)
                    self.colors[k] = curses.color_pair(i)
                except curses.error:
                    self.colors[k] = 0
            self.colors["hdr"] |= curses.A_BOLD
            self.colors["act"] |= curses.A_BOLD
        else:
            self.colors.update({k: 0 for k in pairs})
        self.feeder.start()
        try:
            while True:
                self.refresh()
                if self.snap is None:
                    scr.erase()
                    self.put(0, 0, f"cannot load state: {self.msg}")
                    scr.refresh()
                    if scr.getch() in (ord("q"), 27):
                        return
                    continue
                self.draw()
                try:
                    ch = scr.get_wch()
                except curses.error:
                    continue
                if not self.handle(ch):
                    return
        finally:
            self.feeder.close()


def run(ctl):
    import locale
    locale.setlocale(locale.LC_ALL, "")
    import os
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(App(ctl).main)
