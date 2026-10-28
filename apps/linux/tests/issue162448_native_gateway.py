"""Synthetic loopback service for the real native Gateway authentication owner."""


import socket
import threading

from inline_browser import GatewayFixture
from quick_chat import QuickChatHandler


class NativeGatewayFixture(GatewayFixture):
    def __init__(self, artifacts_dir):
        super().__init__(artifacts_dir)
        # QuickChatHandler owns its bounded real WebSocket framing and challenge.
        self.lock = threading.RLock()
        self.clients = set()
        self.native_requests = []

    def request(self, _client, frame):
        method = frame.get("method")
        with self.lock:
            if len(self.native_requests) >= 200:
                raise RuntimeError("Native assistant fixture exceeded 200 RPC requests")
            self.native_requests.append(method)
        if method == "connect":
            params = frame.get("params") or {}
            client = params.get("client") or {}
            if client.get("id") != "openclaw-linux" or params.get("role") != "operator":
                raise RuntimeError("Expected the real native Linux operator connection")
            # The fresh isolated native owner has no credentials. As the synthetic
            # Gateway, issue a bounded fixture grant through the real hello wire.
            # NativeControlSession needs BOTH a grant and operator scopes; an
            # empty auth object can connect Quick Chat but cannot sign dashboard
            # challenges. The app persists/signs this grant under its private HOME.
            return {
                "type": "hello-ok",
                "protocol": 4,
                "features": {
                    "methods": ["agents.list", "config.get", "chat.send", "chat.history"],
                },
                "auth": {
                    "deviceToken": "issue162448-synthetic-device-grant",
                    "role": "operator",
                    "scopes": [
                        "operator.admin", "operator.read", "operator.write",
                        "operator.approvals", "operator.pairing",
                    ],
                },
                "policy": {"tickIntervalMs": 60000},
            }
        if method == "agents.list":
            return {
                "defaultId": "main", "mainKey": "main", "scope": "per-sender",
                "agents": [{"id": "main", "kind": "agent", "name": "OpenClaw"}],
            }
        if method == "config.get":
            return {"config": {}}
        # No native chat/provider calls are required by this panel/window proof.
        raise RuntimeError("Unexpected native assistant fixture RPC: " + str(method))

    def server_close(self):
        # The parent driver shuts down the HTTP accept loop first. Interrupt any
        # remaining daemon WebSocket reader rather than waiting its 90s timeout.
        with self.lock:
            clients = list(self.clients)
        for client in clients:
            try:
                client.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.connection.close()
        super().server_close()
