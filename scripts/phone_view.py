"""One phone-size read-only view, served separately on a private Unix socket."""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import http.server
import json
import os
import re
import signal
import socketserver
import stat
from pathlib import Path

CSS = ("html{color-scheme:light dark;font:16px system-ui}body{margin:0 auto;padding:1rem;max-width:32rem}"
       "h1{margin:.35rem 0;font-size:1.65rem}h2{font-size:1rem;margin:1.4rem 0 .5rem}"
       "p{margin:.3rem 0}ul{padding:0;list-style:none;margin:0}li{padding:.65rem 0;"
       "border-bottom:1px solid color-mix(in srgb,currentColor 18%,transparent)}"
       "strong{overflow-wrap:anywhere}.line{display:flex;justify-content:space-between;gap:.75rem;"
       "align-items:baseline}.reading{white-space:nowrap;font-variant-numeric:tabular-nums}"
       "small{display:block;margin-top:.2rem;font-size:.86rem}")
CSS_HASH = base64.b64encode(hashlib.sha256(CSS.encode()).digest()).decode()
CSP = f"default-src 'none'; style-src 'sha256-{CSS_HASH}'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
LOGIN = re.compile(r"[!-~]{1,320}\Z")


def cached_snapshot(fleet, overlay: Path, state: Path) -> dict:
    """The same projection as `glance --json --no-refresh`; no discovery or refresh."""
    roster = fleet.read_overlay(overlay, state, discover=False)
    runtime = fleet.load_json(state / 'runtime.json', {}) or {}
    overview = fleet.fleet_overview(roster, runtime, state)
    return fleet.glance_payload(overlay, roster, overview, fleet.resolve_lead_pool(roster, None))


def render(snapshot: dict) -> str:
    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           f'<title>Crossfeed</title><style>{CSS}</style></head>',
           '<body><main><h1>Crossfeed</h1><p>Saved readings · read only</p><h2>Quotas</h2><ul>']
    for pool in snapshot['pools']:
        used = pool['used_percent']
        stale = pool.get('gauge_error') or pool.get('stale_age_s') is not None
        if used is None:
            value = 'Not read' if pool['measured'] or stale else 'No quota window'
        else:
            value = f'{100 - used}% left'
        out.append(f'<li><div class="line"><strong>{html.escape(pool["name"] or "Pool")}</strong>'
                   f'<span class="reading">{value}</span></div>')
        if used is not None:
            reset = f'Resets {pool["resets"]}' if pool['resets'] else 'Reset unknown'
            details = [reset, (pool.get('pace') or {}).get('words'), 'Stale' if stale else None]
            out.append(f'<small>{html.escape(" · ".join(filter(None, details)))}</small>')
        out.append('</li>')
    if not snapshot['pools']:
        out.append('<li>No pools configured</li>')
    out.append('</ul><h2>Monthly subscriptions</h2>')
    spend = snapshot['subscription_spend']
    if spend['unavailable']:
        out.append('<p>Spend unavailable</p>')
    elif not spend['monthly']:
        out.append('<p>No prices entered</p>' if spend['unpriced'] else '<p>No subscriptions configured</p>')
    else:
        out.append('<ul>')
        for row in spend['monthly']:
            total = html.escape(f'{row["currency"]} {row["total"]}')
            own = html.escape(f'{row["currency"]} {row["operator"]}') if row['operator'] is not None else 'not set'
            out.append(f'<li><strong>{total}</strong><small>Your share: {own}</small></li>')
        out.append('</ul>')
        if spend['unpriced']:
            out.append(f'<p>Partial total · {spend["unpriced"]} without a price</p>')
    out.append('</main></body></html>')
    return ''.join(out)


def operator_login(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('phone config is not private')
        data = handle.read(4097)
    if len(data) > 4096:
        raise ValueError('phone config is too large')
    cfg = json.loads(data)
    login = cfg.get('operator_login') if isinstance(cfg, dict) else None
    if not isinstance(login, str) or not LOGIN.fullmatch(login):
        raise ValueError('operator login is missing')
    return login


def make_handler(login, snapshot):
    class Phone(http.server.BaseHTTPRequestHandler):
        server_version = 'Crossfeed'
        sys_version = ''
        timeout = 5

        def log_message(self, format, *args):
            pass  # request paths and identity headers do not enter logs

        def send_page(self, status, body, media='text/plain; charset=utf-8'):
            data = body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', media)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'private, no-store')
            self.send_header('Content-Security-Policy', CSP)
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(data)

        def do_GET(self):
            identities = self.headers.get_all('Tailscale-User-Login') or []
            if len(identities) != 1 or not LOGIN.fullmatch(identities[0]) or not hmac.compare_digest(identities[0], login):
                self.send_page(403, 'Crossfeed is available only to the configured tailnet login.\n')
                return
            if self.path not in {'/', '/crossfeed', '/crossfeed/'}:
                self.send_page(404, 'Not found.\n')
                return
            try:
                body = render(snapshot())
            except Exception:  # the read failure may include local paths or config values
                self.send_page(503, 'Readings unavailable.\n')
                return
            self.send_page(200, body, 'text/html; charset=utf-8')

        do_HEAD = do_GET

        def do_POST(self):
            self.send_page(405, 'Read only.\n')

    return Phone


class PhoneServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def open_server(path: Path, login: str, snapshot):
    if not path.is_absolute():
        raise ValueError('socket path must be absolute')
    parent = path.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700):
        raise ValueError('socket directory must be private')
    server = PhoneServer(str(path), make_handler(login, snapshot))
    try:
        path.chmod(0o600)
    except OSError:
        server.server_close()
        raise
    return server


def main() -> int:
    import fleetctl

    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument('--config', required=True, type=Path)
    args.add_argument('--socket', required=True, type=Path)
    args.add_argument('--overlay', type=Path, default=fleetctl.DEFAULT_OVERLAY)
    args.add_argument('--state-dir', type=Path, default=fleetctl.DEFAULT_STATE_DIR)
    cfg = args.parse_args()
    overlay, state = cfg.overlay.expanduser(), cfg.state_dir.expanduser()
    try:
        login = operator_login(cfg.config.expanduser())
        server = open_server(cfg.socket, login, lambda: cached_snapshot(fleetctl, overlay, state))
    except (ValueError, OSError, RecursionError):
        print('Crossfeed phone view needs a private mode-600 config and an unused socket in a mode-700 directory.')
        return 2

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        with server:
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        cfg.socket.unlink(missing_ok=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
