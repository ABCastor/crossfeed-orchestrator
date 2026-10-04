"""Saved worker identities and bounded wakes through the owner's Gaddi CLI."""
from __future__ import annotations

import fcntl
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

try:
    from chatgpt_transport import Rejected, catalog_models, request, settings
except ImportError:
    from scripts.chatgpt_transport import Rejected, catalog_models, request, settings


LEVEL_NAMES = ("Instant", "Medium", "High", "Extra High", "Pro")

RATE_LIMIT = """(() => {
 const pattern = /too many requests|you['’]ve reached|rate limit/i;
 if (pattern.test(document.title || '')) return true;
 return [...document.querySelectorAll('[role="alert"], [role="dialog"], [role="status"]')].some(e => {
   if (e.closest('nav, aside, [role="navigation"], [data-testid^="conversation-turn"], [data-message-author-role]')) return false;
   const style = getComputedStyle(e);
   return e.getClientRects().length > 0 && style.visibility !== 'hidden' && style.visibility !== 'collapse'
     && pattern.test(e.innerText || '');
 });
})()"""


def wake_state_file(lane):
    return Path(lane.get("wake_state_file") or
                Path(lane["auth"]["key_file"]).expanduser().parent / "wake-state.json").expanduser()


def load_workers(lane):
    """Read saved settings from the gateway, the sole identity authority."""
    base, key = settings(lane)
    rows = catalog_models(request(base, key, "/models", timeout=1))
    return {row["worker_label"]: {"row": row["row"], "position": row["position"],
                                  "level": row["worker_level"]} for row in rows.values()}


def contacts(status):
    rows = status.get("workers")
    if not isinstance(rows, list):
        raise Rejected(6, "gateway did not list labelled worker contacts")
    found = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("label"), str):
            raise Rejected(6, "invalid gateway worker contact")
        if row["label"] in found:
            raise Rejected(6, "duplicate gateway worker label")
        found[row["label"]] = row.get("contact")
    return found


def worker_contacts(base, key):
    return contacts(request(base, key, "/gateway/status", timeout=1))


def worker_active(base, key, label):
    status = request(base, key, "/gateway/status", timeout=1)
    contacts(status)  # Validate the labelled status before trusting it.
    return any(row["label"] == label and (row.get("processing_claim") is True
               or (row.get("contact") == "recent" and row.get("polling") is True))
               for row in status["workers"])


# Composer settings are account-wide, so restore the saved identity on each wake.
PILL = r"""(() => {
 const visible = e => !!e && !!e.getClientRects().length;
 const button = [...document.querySelectorAll('button[aria-label="Select ChatGPT model"], [data-testid="model-switcher-dropdown-button"]')].find(visible);
 const switches = [...document.querySelectorAll('button')].filter(visible);
 const chat = switches.find(b => b.textContent.trim() === 'Chat');
 const work = switches.find(b => b.textContent.trim() === 'Work');
 const selected = b => !!b && (b.getAttribute('aria-pressed') === 'true' || b.getAttribute('aria-selected') === 'true' || b.getAttribute('data-state') === 'active');
 const modes = [];
 if (chat && work && selected(chat) !== selected(work)) modes.push(selected(chat));
 if (chat && work && chat.parentElement === work.parentElement && chat.parentElement) {
   for (const e of chat.parentElement.querySelectorAll('[style]')) {
     const m = (e.getAttribute('style') || '').match(/(?:translateX\(\s*(0|100)%\s*\)|translate(?:\s*:\s*|\(\s*)(0|100)%)/i);
     if (m) modes.push((m[1] || m[2]) === '0');
   }
 }
 return {pill: button ? (button.innerText || button.textContent).trim().replace(/\s+/g, ' ') : null,
         chat: selected(work) || modes.includes(false) ? false : modes.length ? true : null,
         url: location.origin + location.pathname};
})()"""


OPEN_PICKER = """(() => {
 const b = [...document.querySelectorAll('button[aria-label="Select ChatGPT model"], [data-testid="model-switcher-dropdown-button"]')].find(e => e.getClientRects().length);
 if (!b) return false;
 b.focus();
 return true;
})()"""

PICKER = r"""(() => {
 const target = __WORKER__;
 const menu = [...document.querySelectorAll('[role="menu"]')].find(e => e.getClientRects().length);
 if (!menu) return null;
 const text = e => (e.textContent || '').trim().replace(/\s+/g, ' ');
 const rows = [...menu.querySelectorAll('[role="menuitemradio"]')];
 const selected = rows.find(e => e.getAttribute('aria-checked') === 'true');
 const row = rows.find(e => text(e) === target.row || [...e.querySelectorAll('span')].some(s => text(s) === target.row));
 if (row) row.setAttribute('data-crossfeed-wake', 'row');
 const slider = menu.querySelector('[role="slider"]');
 return {row: row && selected === row ? target.row : selected ? text(selected) : null, targetVisible: !!row && !!row.getClientRects().length,
   position: slider ? Number(slider.getAttribute('aria-valuenow')) : null,
   minimum: slider ? slider.getAttribute('aria-valuemin') : null,
   maximum: slider ? slider.getAttribute('aria-valuemax') : null,
   status: [...menu.querySelectorAll('[role="status"]')].map(text).find(s => / of /i.test(s)) || null};
})()"""

FOCUS_POWER = """(() => {
 const power = document.querySelector('[role="menu"] [data-reasoning-slider="true"]');
 if (!power) return false;
 power.focus(); return true;
})()"""


def select_level(browser, tab, worker, deadline):
    """Use short Gaddi steps so Chrome can finish each picker transition."""
    expected = LEVEL_NAMES[worker["position"]]
    script = PICKER.replace("__WORKER__", json.dumps(worker))
    def open_menu():
        browser.evaluate(tab, OPEN_PICKER)
        _mutate(browser, tab, "press", "ArrowDown",
                confirmed=lambda: isinstance(browser.evaluate(tab, script), dict))
    open_menu()
    def state():
        value = browser.evaluate(tab, script)
        while not isinstance(value, dict):
            open_menu()
            value = browser.evaluate(tab, script)
            if not isinstance(value, dict):
                _pause(deadline, clock=browser.clock)
        return value
    value = state()
    if value.get("row") != worker["row"]:
        if not value.get("targetVisible"):
            _mutate(browser, tab, "click", '[data-model-picker-view-toggle="true"]',
                    confirmed=lambda: state().get("targetVisible") is True)
            while not state().get("targetVisible"):
                _pause(deadline, clock=browser.clock)
        _mutate(browser, tab, "click", '[data-crossfeed-wake="row"]',
                confirmed=lambda: state().get("row") == worker["row"])
        value = state()
        if value.get("row") != worker["row"]:
            raise Rejected(6, "saved model row was not confirmed")
    if value.get("minimum") != "0" or value.get("maximum") != "4":
        raise Rejected(6, "Chat power slider was not confirmed")
    current = value.get("position")
    if not isinstance(current, int) or isinstance(current, bool) or not 0 <= current < 5:
        raise Rejected(6, "Chat power position was not confirmed")
    # The trigger attribute is shared by Chat and Work; validate Chat levels.
    if value.get("status") != LEVEL_NAMES[current] + ", " + str(current + 1) + " of 5.":
        raise Rejected(6, "Chat power levels were not confirmed")
    if not browser.evaluate(tab, FOCUS_POWER):
        raise Rejected(6, "Chat power control was not found")
    while current != worker["position"]:
        next_position = current + (1 if worker["position"] > current else -1)
        _mutate(browser, tab, "press", "ArrowRight" if next_position > current else "ArrowLeft",
                confirmed=lambda: state().get("position") != current)
        while True:
            value = state()
            if value.get("position") == next_position and value.get("status") == LEVEL_NAMES[next_position] + ", " + str(next_position + 1) + " of 5.":
                break
            _pause(deadline, clock=browser.clock)
        current = next_position
    _mutate(browser, tab, "press", "Escape",
            confirmed=lambda: browser.evaluate(tab, script) is None)
    if os.environ.get("CHATGPT_WAKE_TRACE") == "1":
        print("wake selected level:", expected, file=sys.stderr)


MENTION = """(() => {
 const b = [...document.querySelectorAll('button')].find(b => b.getClientRects().length &&
   b.textContent.trim().toLowerCase().startsWith('crossfeed') && !b.closest('[contenteditable]'));
 if (!b) return false;
 b.setAttribute('data-crossfeed-wake', 'mention'); return true;
})()"""

COMPOSER = """(() => {
 const e = [...document.querySelectorAll('[contenteditable=true]')].find(e => e.getClientRects().length);
 if (!e) return null;
 const chip = [...e.querySelectorAll('[contenteditable=false]')].some(c =>
   /crossfeed/i.test(c.textContent || c.getAttribute('aria-label') || ''));
 return {text: e.innerText || e.textContent || '', chip};
})()"""

ALLOW = """(() => {
 const b = [...document.querySelectorAll('button')].find(b => b.getClientRects().length && b.textContent.trim() === 'Always allow');
 if (!b) return false;
 b.setAttribute('data-crossfeed-wake', 'allow'); return true;
})()"""

READY = r"""(() => {
 const visible = e => !!e && !!e.getClientRects().length;
 const picker = [...document.querySelectorAll('button[aria-label="Select ChatGPT model"], [data-testid="model-switcher-dropdown-button"]')].some(visible);
 const composer = [...document.querySelectorAll('[contenteditable=true]')].some(visible);
 return {ready: picker && composer, picker, composer,
   loading: document.readyState !== 'complete',
   challenge: /just a moment|checking your browser/i.test(document.title),
   conversation: /\/c\/[A-Za-z0-9-]+$/.test(location.pathname),
   signIn: [...document.querySelectorAll('button')].some(b => ['Log in','Sign in'].includes(b.textContent.trim()))};
})()"""


class Gaddi:
    def __init__(self, lane, deadline, *, clock=None):
        self.clock = time if clock is None else clock
        self.cli = str(Path(lane.get("gaddi_cli") or
                           "gaddi").expanduser())
        self.deadline = deadline
        self.stage = "opening new chat"
        self.last_trace = None
        self.check = None

    def call(self, *args):
        if self.check is not None:
            self.check()
        remaining = self.deadline - self.clock.monotonic()
        if remaining <= 0:
            raise Rejected(6, "worker wake timed out during " + self.stage)
        try:
            result = subprocess.run([self.cli, "--json", *map(str, args)],
                                    capture_output=True, text=True, timeout=min(30, remaining))
            data = json.loads(result.stdout)
            if result.returncode or not isinstance(data, dict) or data.get("ok") is False or data.get("error"):
                error = data.get("error", {}) if isinstance(data, dict) else {}
                code = error.get("code") if isinstance(error, dict) else None
                message = error.get("message", "") if isinstance(error, dict) else ""
                # Report categories only, never arbitrary page or CLI output.
                category = code if code in {"stale", "held", "denied"} else "error"
                if category == "error" and isinstance(message, str):
                    if any(text in message.lower() for text in (
                            "bridge not connected", "gaddi unreachable", "econnrefused",
                            "connection refused", "could not connect")):
                        category = "unavailable"
                    elif "debugger" in message.lower():
                        category = "debugger"
                    elif "evaluation failed" in message.lower():
                        category = "evaluation"
                    elif "changed since" in message.lower():
                        category = "stale"
                    elif "busy" in message.lower():
                        category = "busy"
                    elif "timed out" in message.lower() or "timeout" in message.lower():
                        category = "timeout"
                    elif "did not respond" in message.lower():
                        category = "unresponsive"
                if os.environ.get("CHATGPT_WAKE_TRACE") == "1":
                    print("wake error:", category, file=sys.stderr)
                raise Rejected(6, "Gaddi " + category + " during " + self.stage)
            return data.get("result", data)
        except subprocess.TimeoutExpired:
            raise Rejected(6, "worker wake timed out while waiting for Gaddi during " + self.stage) from None
        except OSError:
            raise Rejected(6, "Gaddi unavailable during " + self.stage) from None
        except ValueError:
            # Never relay raw CLI output: it can contain page content or secrets.
            if result.returncode and result.stderr.startswith("gaddi: daemon not reachable at "):
                raise Rejected(6, "Gaddi unavailable during " + self.stage) from None
            raise Rejected(6, "Gaddi could not complete the worker wake during " + self.stage) from None

    def evaluate(self, tab, script, *, check_rate_limit=True):
        checked = ("(() => { if (" + RATE_LIMIT + ") return {crossfeedRateLimited: true}; return "
                   + script + "; })()") if check_rate_limit else script
        while True:
            try:
                value = self.call("eval", tab, checked)
                break
            except Rejected as exc:
                if not exc.message.startswith(TRANSIENT_READ_ERRORS):
                    raise
                try:
                    _pause(self.deadline, clock=self.clock)
                except Rejected:
                    raise exc from None
                if self.clock.monotonic() >= self.deadline:
                    raise
        if not isinstance(value, dict) or "value" not in value:
            raise Rejected(6, "Gaddi returned no observable page state")
        state = value["value"]
        if isinstance(state, dict) and state.get("crossfeedRateLimited") is True:
            self.on_rate_limit()
            raise Rejected(6, "ChatGPT rate-limit cooldown: wakes paused for 60 minutes")
        if os.environ.get("CHATGPT_WAKE_TRACE") == "1":
            # Never print eval results, including session data or page text.
            trace = self.stage
            if trace != self.last_trace:
                print("wake stage:", trace, file=sys.stderr)
                self.last_trace = trace
        return state


def _pause(deadline, seconds=3, *, clock=None):
    clock = time if clock is None else clock
    remaining = deadline - clock.monotonic()
    if remaining <= 0:
        raise Rejected(6, "worker wake timed out")
    clock.sleep(min(seconds, remaining))


HOME = "https://chatgpt.com/"
LOCATION = "location.origin + location.pathname"
CHAT = """(() => {
 const b = [...document.querySelectorAll('button')].find(b => b.getClientRects().length && b.textContent.trim() === 'Chat');
 if (!b) return false;
 b.setAttribute('data-crossfeed-wake', 'chat'); return true;
})()"""
ARCHIVE = """(async () => {
 try {
   const session = await fetch('/api/auth/session');
   if (!session.ok) return session.status;
   const t = (await session.json()).accessToken;
   if (typeof t !== 'string' || !t) return 401;
   const response = await fetch('/backend-api/conversation/' + __ID__, {
     method: 'PATCH', headers: {Authorization: 'Bearer ' + t, 'Content-Type': 'application/json'},
     body: '{"is_archived":true}'});
   return response.status;
 } catch { return 0; }
})()"""


def conversation_id(url):
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if (parsed.scheme != "https" or parsed.netloc != "chatgpt.com"
            or parsed.query or parsed.fragment):
        return None
    match = re.fullmatch(r"/c/([A-Za-z0-9-]+)", parsed.path)
    return match[1] if match and url == HOME + "c/" + match[1] else None


def _read_json(path):
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError):
        raise Rejected(6, "worker wake state is unreadable") from None
    if not isinstance(data, dict):
        raise Rejected(6, "invalid worker wake state")
    return data


def _atomic_json(path, data):
    # A private temp file on the same filesystem makes replacement atomic.
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix="." + path.name + ".", delete=False) as out:
            name = out.name
            json.dump(data, out, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    except (OSError, ValueError):
        raise Rejected(6, "worker wake state could not be saved") from None
    finally:
        if name and os.path.exists(name):
            os.unlink(name)  # Unpublished scratch file, never owner data.


def _pending(data):
    pending = data.get("_archive_pending", [])
    if (not isinstance(pending, list) or any(not isinstance(i, str)
            or not re.fullmatch(r"[A-Za-z0-9-]+", i) for i in pending)):
        raise Rejected(6, "invalid pending worker archives")
    return list(dict.fromkeys(pending))


def _wake_limits(state, lane):
    def timestamp(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    wakes, failed, cooldown = state.get("wakes", []), state.get("failed", {}), state.get("cooldown_until", 0)
    cap = lane.get("wake_daily_cap", 30)
    if (not isinstance(wakes, list) or not all(timestamp(t) for t in wakes)
            or not isinstance(failed, dict) or not all(timestamp(t) for t in failed.values())
            or not timestamp(cooldown) or not timestamp(state.get("pro_cooldown_until", 0))
            or not timestamp(state.get("browser_backoff_until", 0))
            or not isinstance(cap, int) or isinstance(cap, bool) or cap < 0):
        raise Rejected(6, "invalid worker wake limits or state")
    return wakes, failed, cooldown, cap


def wake_guard_status(lane):
    """Read local wake bounds without admitting a wake or revealing state data."""
    try:
        state = _read_json(wake_state_file(lane))
        wakes, failed, cooldown, cap = _wake_limits(state, lane)
        now = time.time()
        count = sum(t > now - 86400 for t in wakes)
        active = (cooldown > now or state.get("browser_backoff_until", 0) > now
                  or any(t + 600 > now for t in failed.values()))
        return f"wake guard: {count}/{cap} wakes in last 24 h, cooldown {'active' if active else 'inactive'}"
    except (Rejected, OSError, KeyError, TypeError, ValueError):
        return "wake guard: unavailable"


def _admit(state, lane, label, now):
    wakes, failed, cooldown, cap = _wake_limits(state, lane)
    if cooldown > now:
        raise Rejected(6, "ChatGPT rate-limit cooldown: wakes paused")
    if lane.get("worker_level") == "pro" and state.get("pro_cooldown_until", 0) > now:
        raise Rejected(6, "ChatGPT Pro rate-limit cooldown: wakes paused for 60 minutes")
    if state.get("browser_backoff_until", 0) > now:
        raise Rejected(6, "Gaddi unavailable: wakes paused for 5 minutes")
    if label in failed and failed[label] + 600 > now:
        raise Rejected(6, "worker failure cooldown: retry after 10 minutes")
    state["wakes"] = [t for t in wakes if t > now - 86400]
    state["failed"] = {l: t for l, t in failed.items() if t + 600 > now}
    if len(state["wakes"]) >= cap:
        raise Rejected(6, "worker wake daily cap reached (rolling 24 hours)")
    state["wakes"].append(now)


def _save_chat(lane, label, url):
    path = wake_state_file(lane)
    data = _read_json(path)
    chats = data.setdefault("chats", {})
    if not isinstance(chats, dict) or not isinstance(chats.get(label, {}), dict):
        raise Rejected(6, "invalid saved worker conversations")
    previous = conversation_id(chats.get(label, {}).get("url"))
    current = conversation_id(url)
    if current is None or current == previous:
        raise Rejected(6, "new worker conversation was not confirmed")
    chats[label] = {**chats.get(label, {}), "url": url}
    pending = _pending(data)
    if previous and previous not in pending:
        pending.append(previous)
    data["_archive_pending"] = pending
    _atomic_json(path, data)
    return data


def _retry_archives(browser, tab, lane, data):
    # Pending ids are persisted before the request, so interruptions lose none.
    retained = {conversation_id(row.get("url")) for row in data.get("chats", {}).values() if isinstance(row, dict)}
    pending = _pending(data)
    for chat_id in list(pending):
        if chat_id in retained:
            continue
        try:
            status = browser.evaluate(tab, ARCHIVE.replace("__ID__", json.dumps(chat_id)), check_rate_limit=False)
        except Rejected:
            continue
        if isinstance(status, int) and not isinstance(status, bool) and 200 <= status < 300:
            pending.remove(chat_id)
    data["_archive_pending"] = pending
    try:
        _atomic_json(wake_state_file(lane), data)
    except Rejected:
        # The pre-request queue is durable; retrying archived ids is harmless.
        pass


TRANSIENT_READ_ERRORS = ("Gaddi unresponsive ", "Gaddi busy ", "Gaddi debugger ",
                         "Gaddi timeout ", "Gaddi error ", "Gaddi evaluation ",
                         "worker wake timed out while waiting for Gaddi ")


def _mutate(browser, tab, action, *args, confirmed):
    """A lost reply can hide success: observe before repeating, at most three calls."""
    for attempt in range(3):
        if browser.clock.monotonic() >= browser.deadline:
            raise Rejected(6, "worker wake timed out during " + browser.stage)
        try:
            return browser.call(action, tab, *args)
        except Rejected as exc:
            if not exc.message.startswith(TRANSIENT_READ_ERRORS):
                raise
            if confirmed():
                return
            if attempt == 2:
                raise


def _chat_url(browser, tab, *, check_rate_limit=True):
    deadline = min(browser.deadline, browser.clock.monotonic() + 15)
    while True:
        url = browser.evaluate(tab, LOCATION, check_rate_limit=check_rate_limit)
        if conversation_id(url):
            return url
        if url != HOME:
            raise Rejected(6, "new worker conversation was not observable")
        if browser.clock.monotonic() >= deadline:
            raise Rejected(6, "new worker conversation was not observable before timeout")
        _pause(deadline, clock=browser.clock)


def _close_tab(browser, tab):
    browser.stage = "opened worker tab cleanup"
    browser.deadline = browser.clock.monotonic() + 60
    result = browser.call("close", tab)
    closed = result.get("closed", []) if isinstance(result, dict) else []
    missing = any(str(row.get("tab")) == tab and row.get("reason") == "No tab with id: " + tab + "."
                  for row in result.get("failed", []) if isinstance(row, dict)) if isinstance(result, dict) else False
    if int(tab) not in closed and tab not in closed and not missing:
        raise Rejected(6, "opened worker tab closure was not confirmed")


def _wake(lane, label, *, timeout, clock, attempt, check=None):
    """One fresh chat per wake, globally serialized with durable volume limits."""
    base, key = settings(lane)
    deadline = clock.monotonic() + timeout
    path = wake_state_file(lane)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    attempt["stage"] = "wake lock"
    with (path.parent / ".worker-wake.lock").open("a") as lock:
        while True:
            if check is not None:
                check()
            if clock.monotonic() >= deadline:
                raise Rejected(6, "worker wake timed out")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                _pause(deadline, clock=clock)
        attempt["stage"] = "saved worker lookup"
        workers = load_workers(lane)
        if label not in workers:
            raise Rejected(6, "no saved worker configuration for " + label)
        if worker_active(base, key, label):
            return
        worker = workers[label]
        attempt["stage"] = "wake admission"
        state_path = path
        state = _read_json(state_path)
        _pending(state)
        chats = state.get("chats", {})
        if (not isinstance(chats, dict) or any(not isinstance(row, dict)
                or conversation_id(row.get("url")) is None for row in chats.values())):
            raise Rejected(6, "invalid saved worker conversations")
        _admit(state, dict(lane, worker_level=worker["level"]), label, clock.time())
        _atomic_json(state_path, state)
        browser = Gaddi(lane, deadline, clock=clock)
        browser.check = check
        def rate_limited():
            field = "pro_cooldown_until" if worker["level"] == "pro" else "cooldown_until"
            state[field] = clock.time() + 3600
            _atomic_json(state_path, state)
        def failure_recorded(error=None):
            latest = _read_json(state_path)
            if isinstance(error, Rejected) and error.message.startswith("Gaddi unavailable"):
                latest["browser_backoff_until"] = clock.time() + 300
            else:
                latest.setdefault("failed", {})[label] = clock.time()
            _atomic_json(state_path, latest)
        browser.on_rate_limit = rate_limited
        tab, sent, retained = None, False, False
        try:
            opened = browser.call("open", HOME, "--group", "Crossfeed Chat workers")
            if (not isinstance(opened, dict) or not isinstance(opened.get("id"), (str, int))
                    or isinstance(opened.get("id"), bool) or not re.fullmatch(r"\d+", str(opened["id"]))
                    or int(opened["id"]) > 9007199254740991):
                raise Rejected(6, "Gaddi returned no opened tab")
            tab = str(opened["id"])
            browser.stage = "new chat loading"
            foreground_at = clock.monotonic() + 30
            shown = False
            ready_at = None
            while True:
                ready = browser.evaluate(tab, READY)
                if isinstance(ready, dict) and ready.get("ready") is True:
                    if ready_at is not None and clock.monotonic() - ready_at >= 1.5:
                        break
                    ready_at = clock.monotonic()
                    _pause(deadline, 1.5, clock=clock)
                    continue
                ready_at = None
                if not shown and clock.monotonic() >= foreground_at:
                    browser.call("show", tab)
                    shown = True
                _pause(deadline, clock=clock)
            browser.stage = "Chat mode selection"
            observed = browser.evaluate(tab, PILL)
            if not isinstance(observed, dict) or observed.get("url") != HOME:
                raise Rejected(6, "new-chat home was not confirmed")
            if observed.get("chat") is not True:
                if not browser.evaluate(tab, CHAT):
                    raise Rejected(6, "Chat toggle was not found")
                def chat_selected():
                    value = browser.evaluate(tab, PILL)
                    if not isinstance(value, dict) or value.get("url") != HOME:
                        raise Rejected(6, "new-chat home changed before selection")
                    return value.get("chat") is True
                _mutate(browser, tab, "click", '[data-crossfeed-wake="chat"]',
                        confirmed=chat_selected)
            while True:
                observed = browser.evaluate(tab, PILL)
                if not isinstance(observed, dict) or observed.get("url") != HOME:
                    raise Rejected(6, "new-chat home changed before selection")
                if observed.get("chat") is True and observed.get("pill"):
                    break
                _pause(deadline, clock=clock)
            browser.stage = "saved model and level selection"
            select_level(browser, tab, worker, deadline)
            browser.stage = "crossfeed mention selection"
            def composer_state():
                value = browser.evaluate(tab, COMPOSER)
                if not isinstance(value, dict):
                    raise Rejected(6, "composer was not observable after mutation")
                return value
            def mention_typed():
                composer = composer_state()
                if browser.evaluate(tab, MENTION):
                    return True
                text = composer.get("text", "").strip()
                if text and not text.startswith("@crossfeed"):
                    raise Rejected(6, "unexpected composer text after typing mention")
                return text.startswith("@crossfeed")
            _mutate(browser, tab, "type", "[contenteditable=true]", "@crossfeed",
                    confirmed=mention_typed)
            while not browser.evaluate(tab, MENTION):
                _pause(deadline, clock=clock)
            _mutate(browser, tab, "click", '[data-crossfeed-wake="mention"]',
                    confirmed=lambda: composer_state().get("chip") is True)
            wake_text = (" wake up as " + label
                         + ". Poll gateway_exchange with worker_label " + label + " until released.")
            _mutate(browser, tab, "type", "[contenteditable=true]", wake_text, "--mode", "append",
                    confirmed=lambda: wake_text in composer_state().get("text", ""))
            observed = browser.evaluate(tab, PILL)
            if not isinstance(observed, dict) or observed.get("url") != HOME or observed.get("chat") is not True:
                raise Rejected(6, "new-chat home or Chat mode changed before send")
            sent = True  # Enter may succeed even when Gaddi loses its reply.
            try:
                browser.call("press", tab, "Enter")
            except Rejected as exc:
                if not exc.message.startswith(TRANSIENT_READ_ERRORS):
                    raise
            browser.stage = "worker registration"
            permission_approved, permission_at = False, 0
            while True:
                if worker_active(base, key, label):
                    break
                if clock.monotonic() >= permission_at:
                    allow = browser.evaluate(tab, ALLOW if not permission_approved else "false")
                    permission_at = clock.monotonic() + 3
                    if allow:
                        browser.call("click", tab, '[data-crossfeed-wake="allow"]')
                        permission_approved = True
                _pause(deadline, 1, clock=clock)
            browser.stage = "new conversation retention"
            url = _chat_url(browser, tab)
            data = _save_chat(lane, label, url)
            retained = True
            browser.stage = "previous conversation archive"
            _retry_archives(browser, tab, lane, data)
        except (Rejected, KeyboardInterrupt) as exc:
            browser.check = None  # Cleanup still runs after caller cancellation.
            attempt["stage"] = browser.stage
            failure_recorded(exc)
            if sent and not retained and tab is not None:
                browser.deadline = clock.monotonic() + 30
                try:
                    chat_id = conversation_id(_chat_url(browser, tab, check_rate_limit=False))
                    data = _read_json(path)
                    if chat_id and chat_id != conversation_id(data.get("chats", {}).get(label, {}).get("url")):
                        data["_archive_pending"] = list(dict.fromkeys(_pending(data) + [chat_id]))
                        _atomic_json(path, data)
                except Rejected:
                    raise Rejected(6, "worker wake failed; orphan archive could not be recorded") from None
            raise
        except BaseException:
            attempt["stage"] = browser.stage
            raise
        finally:
            browser.check = None
            if tab is None:
                # Admission reserves the last slot under the global wake lock.
                # Only a confirmed open spends it, regardless of failure type.
                latest = _read_json(state_path)
                latest["wakes"].pop()
                _atomic_json(state_path, latest)
                attempt["refunded"] = True
            if tab is not None:
                failure = sys.exc_info()[1]
                if failure is None:
                    attempt["stage"] = browser.stage
                try:
                    _close_tab(browser, tab)
                except Rejected as exc:
                    if failure is None:
                        attempt["stage"] = browser.stage
                    failure_recorded(exc)
                    if isinstance(failure, Rejected):
                        raise Rejected(6, failure.message + "; opened worker tab cleanup failed") from None
                    raise


def wake_log_file(lane):
    return wake_state_file(lane).with_name("wake-log.jsonl")


def _log_wake(lane, record):
    """Serialize append and trim separately from the browser lock, including refusals."""
    path = wake_log_file(lane)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path.parent / ".worker-wake-log.lock", os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            lines = path.read_text().splitlines()[-499:]
        except FileNotFoundError:
            lines = []
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=path.parent,
                    prefix=".wake-log.", delete=False) as out:
                name = out.name
                for line in lines:
                    out.write(line + "\n")
                out.write(json.dumps(record, separators=(",", ":")) + "\n")
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, path)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)  # Unpublished temporary log, never owner data.


SAFE_WAKE_MESSAGES = frozenset({
    'Chat power control was not found',
    'Chat power levels were not confirmed',
    'Chat power position was not confirmed',
    'Chat power slider was not confirmed',
    'Chat toggle was not found',
    'ChatGPT rate-limit cooldown: wakes paused',
    'ChatGPT rate-limit cooldown: wakes paused for 60 minutes',
    'Gaddi unavailable: wakes paused for 5 minutes',
    'Gaddi returned no observable page state',
    'Gaddi returned no opened tab',
    'composer was not observable after mutation',
    'duplicate gateway worker label',
    'gateway did not list labelled worker contacts',
    'invalid gateway worker contact',
    'invalid pending worker archives',
    'invalid saved worker conversations',
    'invalid worker wake limits or state',
    'invalid worker wake state',
    'new worker conversation was not confirmed',
    'new worker conversation was not observable',
    'new worker conversation was not observable before timeout',
    'new-chat home changed before selection',
    'new-chat home or Chat mode changed before send',
    'new-chat home was not confirmed',
    'opened worker tab closure was not confirmed',
    'saved model row was not confirmed',
    'unexpected composer text after typing mention',
    'worker failure cooldown: retry after 10 minutes',
    'worker wake daily cap reached (rolling 24 hours)',
    'worker wake failed; orphan archive could not be recorded',
    'worker wake state could not be saved',
    'worker wake state is unreadable',
    'worker wake timed out',
})


def _wake_category(error):
    """Fixed categories only: exceptions can carry untrusted labels or gateway text."""
    if error is None:
        return "completed"
    if isinstance(error, KeyboardInterrupt):
        return "interrupted"
    if isinstance(error, Rejected):
        message = error.message
        reason = message.removesuffix("; opened worker tab cleanup failed")
        if reason in SAFE_WAKE_MESSAGES:
            return message
        if message.startswith("Gaddi "):
            category = message.split(" ", 2)[1]
            if category in {"stale", "held", "denied", "error", "debugger", "evaluation",
                            "busy", "timeout", "unresponsive", "unavailable"}:
                return "Gaddi " + category
        for prefix, category in (
            ("worker wake timed out", "timeout"),
            ("ChatGPT rate-limit cooldown", "rate-limit cooldown"),
            ("worker failure cooldown", "failure cooldown"),
            ("worker wake daily cap", "daily cap"),
            ("no saved worker configuration", "unknown worker"),
            ("gateway HTTP", "gateway HTTP error"),
            ("gateway unavailable", "gateway unavailable"),
        ):
            if message.startswith(prefix):
                return category
        return "wake rejected"
    return "unexpected error"


def wake_log_status(lane, *, clock=None):
    """Summarize locally retained attempts; never echo log fields from disk."""
    clock = time if clock is None else clock
    try:
        path = wake_log_file(lane)
        try:
            lines = path.read_text().splitlines()
        except FileNotFoundError:
            lines = []
        attempts = successes = 0
        failures = {}
        now = clock.time()
        for line in lines:
            row = json.loads(line)
            ts = row["ts"]
            if (not isinstance(ts, (int, float)) or isinstance(ts, bool)
                    or not math.isfinite(ts) or row["outcome"] not in {"ok", "failed"}
                    or row["stage"] not in WAKE_STAGES):
                raise ValueError()
            if not now - 86400 < ts <= now:
                continue
            attempts += 1
            if row["outcome"] == "ok":
                successes += 1
            else:
                stage = row["stage"]
                failures[stage] = failures.get(stage, 0) + 1
        stages = ", ".join(f"{stage}={count}" for stage, count in sorted(failures.items())) or "none"
        return (f"wake log (last 24 h, retained): {attempts} attempts, {successes} successes, "
                f"{attempts - successes} failures; failures by stage: {stages}")
    except (OSError, ValueError, TypeError, KeyError):
        return "wake log: unavailable"


WAKE_STAGES = frozenset({
    "wake setup", "wake lock", "saved worker lookup", "wake admission",
    "opening new chat", "new chat loading", "Chat mode selection",
    "saved model and level selection", "crossfeed mention selection",
    "worker registration", "new conversation retention", "previous conversation archive",
    "opened worker tab cleanup",
})


def wake(lane, label, *, timeout=900, clock=None, check=None):
    """Observe one call, including admission refusals and cleanup failures."""
    clock = time if clock is None else clock
    started, ts = clock.monotonic(), clock.time()
    attempt = {"stage": "wake setup"}
    error = None
    try:
        return _wake(lane, label, timeout=timeout, clock=clock, attempt=attempt, check=check)
    except BaseException as exc:
        error = exc
        raise
    finally:
        record = {"ts": ts,
                  "label": label if isinstance(label, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", label) else "invalid-label",
                  "outcome": "ok" if error is None else "failed",
                  "seconds": round(max(0, clock.monotonic() - started), 3),
                  "stage": attempt["stage"], "message": _wake_category(error)}
        if attempt.get("refunded"):
            record["refunded"] = True
        try:
            _log_wake(lane, record)
        except (OSError, ValueError, KeyError, TypeError, Rejected):
            # Diagnostics must not mask the original failure or trigger a duplicate wake.
            print("wake log: could not record attempt", file=sys.stderr)
