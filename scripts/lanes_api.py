"""Loopback chat completions backed by the fleet's read-only text dispatcher."""
from __future__ import annotations

import argparse
import hmac
import json
import os
from pathlib import Path
import stat
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import uuid

import fleetctl
import selector

MAX_BODY_BYTES = 64 * 1024  # Leave room for wrapper headers under portable argv limits.


class APIError(Exception):
    def __init__(self, status: int, message: str, code: str):
        self.status, self.message, self.code = status, message, code


def load_key(path: Path | None) -> str:
    if path is None:
        key = os.environ.get("CROSSFEED_API_KEY", "")
    else:
        try:
            fd = os.open(path.expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                info = os.fstat(handle.fileno())
                if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_uid != os.getuid()):
                    raise fleetctl.FleetError("API key file must be owned by you and have mode 0600")
                key = handle.read(8193).strip()
        except (OSError, UnicodeError):
            raise fleetctl.FleetError("API key file is unavailable") from None
    if not key or len(key) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise fleetctl.FleetError("set CROSSFEED_API_KEY or provide a nonempty 0600 --key-file")
    return key


def model_id(option: dict) -> str:
    # Gateway model keys already include their public chatgpt: prefix.
    key = option["model_key"]
    prefix = "chatgpt" if option["harness"] == "chatgpt-chat" else option["harness"]
    return key if key.startswith(prefix + ":") else prefix + ":" + key


def parse_request(body: object) -> tuple[str, str, bool]:
    if not isinstance(body, dict):
        raise APIError(400, "request must be a JSON object", "invalid_request")
    unsupported = set(body) - {"model", "messages", "stream", "n"}
    if unsupported:
        raise APIError(400, "only model, messages, stream and n=1 are supported; tools and write mode are refused",
                       "unsupported_parameter")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise APIError(400, "model must be a nonempty string", "invalid_model")
    if type(body.get("stream", False)) is not bool or type(body.get("n", 1)) is not int or body.get("n", 1) != 1:
        raise APIError(400, "stream must be boolean and n must be 1", "unsupported_parameter")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise APIError(400, "messages must be a nonempty list", "invalid_messages")
    clean = []
    for message in messages:
        if (not isinstance(message, dict) or set(message) - {"role", "content", "name"}
                or not isinstance(message.get("role"), str)
                or message["role"] not in {"system", "developer", "user", "assistant"}):
            raise APIError(400, "only system, developer, user and assistant text messages are supported", "invalid_messages")
        content = message.get("content")
        if isinstance(content, list):
            if not all(isinstance(part, dict) and set(part) == {"type", "text"}
                       and part["type"] == "text" and isinstance(part["text"], str) for part in content):
                raise APIError(400, "message content must contain text only", "invalid_messages")
            content = "\n".join(part["text"] for part in content)
        if not isinstance(content, str):
            raise APIError(400, "message content must be text", "invalid_messages")
        clean.append({"role": message["role"], "content": content})
    if not any(message["role"] == "user" and message["content"].strip() for message in clean):
        raise APIError(400, "messages must include a nonempty user message", "invalid_messages")
    prompt = ("Reply as the assistant to this text conversation. Preserve the order and roles of the messages. "
              "This is a read-only request.\n" + json.dumps(clean, ensure_ascii=False))
    return model, prompt, body.get("stream", False)


class LanesAPI:
    def __init__(self, overlay: Path, state_dir: Path, directory: Path, role: str):
        self.overlay = overlay.expanduser().resolve()
        self.state_dir = state_dir.expanduser().resolve()
        self.directory = directory.expanduser().resolve()
        self.role = role
        if not self.directory.is_dir():
            raise fleetctl.FleetError("API --dir must name an existing directory")

    def snapshot(self) -> tuple[dict, dict, list[dict]]:
        roster = fleetctl.read_overlay(self.overlay, self.state_dir)
        fleetctl.refresh_stale_pools(self.state_dir,
                                     set(roster.get("quota_pools", {})) | fleetctl.lane_pools(roster), roster=roster)
        runtime = fleetctl.load_json(self.state_dir / "runtime.json", {}) or {}
        options, _ = selector.enumerate_options(roster, runtime, self.role, mode="read-only", modality="text", fleet=fleetctl)
        capped = {pool for pool, settings in roster.get("quota_pools", {}).items()
                  if settings.get("daily_usd_cap") is not None and
                  fleetctl.pool_spent_since(self.state_dir, roster, pool, fleetctl.local_day_start())
                  >= float(settings["daily_usd_cap"])}
        options = [option for option in options if option["pool"] not in capped]
        return roster, runtime, options

    def models(self) -> dict:
        _, _, options = self.snapshot()
        ids = {model_id(option) for option in options}
        ids.add("crossfeed:auto")
        return {"object": "list", "data": [{"id": key, "object": "model", "created": 0,
                                            "owned_by": "crossfeed"} for key in sorted(ids)]}

    def complete(self, requested: str, prompt: str) -> dict:
        roster, runtime, options = self.snapshot()
        target = None
        if requested != "crossfeed:auto":
            matches = [option for option in options if model_id(option) == requested]
            if not matches:
                raise APIError(404, "model is unknown or currently unavailable for read-only text", "model_not_available")
            target = (matches[0]["harness"], matches[0]["model_key"])
        try:
            selection = fleetctl.select_option(roster, runtime, self.state_dir, self.role,
                                               mode="read-only", modality="text", target=target,
                                               lead=fleetctl.resolve_lead_pool(roster))
        except fleetctl.FleetError:
            raise APIError(429, "no admitted lane is available; check fleet quota, switches and selector receipts", "lane_unavailable") from None
        response = {}
        args = argparse.Namespace(dir=self.directory, last=None, overlay=self.overlay, dry_run=False)
        rc = fleetctl.dispatch_selection(selection, args, self.state_dir, prompt, response=response)
        if rc:
            status = 429 if rc in {4, 5, 6} else 502
            raise APIError(status, "worker refused or failed; inspect local dispatch receipts", "dispatch_failed")
        dispatch = response["dispatch"]
        identity = None
        try:
            record = fleetctl.load_json(Path(dispatch["model_receipt"]))
            if (isinstance(record, dict) and record.get("schema") == "crossfeed-model-run/v1"
                    and record.get("dispatch_id") == dispatch["dispatch_id"] and record.get("returncode") == 0):
                identity = {key: record.get(key) for key in
                            ("requested_model", "selected_model", "selector", "actual_model", "identity_source")}
        except (OSError, ValueError):
            pass
        selected = response["option"]
        if identity and isinstance(identity.get("selected_model"), str) and identity["selected_model"]:
            # Direct wrappers can apply a new owner switch between selection and launch.
            selected = dict(selected, model_key=identity["selected_model"])
        return {"id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion",
                "created": int(time.time()), "model": model_id(selected),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": response["content"]},
                             "finish_reason": "stop"}],
                "crossfeed": {"requested_model": requested, "dispatch_id": dispatch["dispatch_id"],
                              "selection_file": response["option"]["selection_file"],
                              "model_receipt": dispatch["model_receipt"], "identity": identity}}


def make_server(api: LanesAPI, key: str, port: int = 4320) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log paths, headers, request bodies or bearer keys.

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def send_json(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def handle_request(self, post=False):
            try:
                provided = self.headers.get("Authorization", "")
                if not hmac.compare_digest(provided.encode(), ("Bearer " + key).encode()):
                    raise APIError(401, "valid bearer authentication is required", "invalid_api_key")
                if not post and self.path == "/v1/models":
                    self.send_json(200, api.models())
                    return
                if not post or self.path != "/v1/chat/completions":
                    raise APIError(404, "unknown endpoint", "not_found")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise APIError(400, "invalid Content-Length", "invalid_request") from None
                if self.headers.get("Transfer-Encoding") or not 0 < length <= MAX_BODY_BYTES:
                    raise APIError(400, "provide a Content-Length between 1 and 65536 bytes", "invalid_request")
                try:
                    body = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeError):
                    raise APIError(400, "invalid JSON body", "invalid_json") from None
                model, prompt, stream = parse_request(body)
                completion = api.complete(model, prompt)
                if not stream:
                    self.send_json(200, completion)
                    return
                # Wrappers publish complete answers; SSE is buffered, not token streaming.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                chunk = {key: completion[key] for key in ("id", "created", "model")}
                chunk.update(object="chat.completion.chunk", crossfeed=completion["crossfeed"])
                for delta, finish in ((completion["choices"][0]["message"], None), ({}, "stop")):
                    chunk["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish}]
                    self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except APIError as exc:
                self.send_json(exc.status, {"error": {"message": exc.message,
                               "type": "invalid_request_error" if exc.status < 500 else "server_error",
                               "param": None, "code": exc.code}})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self.send_json(500, {"error": {"message": "local fleet operation failed; check configuration and receipts",
                                             "type": "server_error", "param": None, "code": "fleet_error"}})

        do_GET = handle_request

        def do_POST(self):
            self.handle_request(post=True)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(overlay: Path, state_dir: Path, *, port: int = 4320, key_file: Path | None = None,
          directory: Path, role: str = "review") -> int:
    key = load_key(key_file)
    api = LanesAPI(overlay, state_dir, directory, role)
    try:
        server = make_server(api, key, port)
    except (OSError, OverflowError):
        raise fleetctl.FleetError("API port is unavailable or invalid") from None
    print(f"Crossfeed lanes API: http://127.0.0.1:{server.server_port}/v1 (read-only text)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
