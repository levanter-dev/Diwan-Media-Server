"""Entry point for the native Windows media-server executable."""

import logging
import os
import socket
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("LOCAL_MEDIA_NATIVE", "1")
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

_data_dir = Path(os.getenv("LOCALAPPDATA", Path.home())) / "LocalMediaServer" if os.getenv("LOCAL_MEDIA_NATIVE") == "1" else Path("./data")
_data_dir.mkdir(parents=True, exist_ok=True)

handlers: list[logging.Handler] = [
    logging.StreamHandler(sys.stderr),
]

_trace_file = _data_dir / "scraper-traces.log"
try:
    fh = logging.FileHandler(str(_trace_file), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    handlers.append(fh)
except OSError:
    pass

logging.basicConfig(
    level=logging.DEBUG if os.getenv("MEDIA_DEBUG_LOGS") else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=handlers,
)

import uvicorn
from app.main import app

try:
    from zeroconf import IPVersion, ServiceInfo, Zeroconf
except Exception:  # pragma: no cover - optional dependency during partial installs
    IPVersion = None
    ServiceInfo = None
    Zeroconf = None


def _lan_ipv4() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except OSError:
        pass
    try:
        addresses = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM)
        return next((str(item[4][0]) for item in addresses
                     if not str(item[4][0]).startswith(("127.", "169.254."))), None)
    except OSError:
        return None


class _RedirectHandler(BaseHTTPRequestHandler):
    portal_port = 8080

    def _redirect(self) -> None:
        host = self.headers.get("Host", "localhost").split(":", 1)[0] or "localhost"
        location = f"http://{host}:{self.portal_port}{self.path}"
        self.send_response(HTTPStatus.TEMPORARY_REDIRECT)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self._redirect()

    def do_HEAD(self) -> None:  # noqa: N802
        self._redirect()

    def do_POST(self) -> None:  # noqa: N802
        self._redirect()

    def do_PUT(self) -> None:  # noqa: N802
        self._redirect()

    def do_DELETE(self) -> None:  # noqa: N802
        self._redirect()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._redirect()

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        logging.getLogger(__name__).debug("Port 80 redirect: " + format, *args)


def _start_port80_redirect(portal_port: int) -> ThreadingHTTPServer | None:
    try:
        _RedirectHandler.portal_port = portal_port
        server = ThreadingHTTPServer(("0.0.0.0", 80), _RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, name="port80-redirect", daemon=True)
        thread.start()
        logging.getLogger(__name__).info("Serving port 80 redirect to http://localhost:%s", portal_port)
        return server
    except OSError as exc:
        logging.getLogger(__name__).warning("Could not bind port 80 redirect: %s", exc)
        return None


def _start_mdns(domain: str, port: int) -> tuple[Zeroconf, ServiceInfo] | None:
    if not domain.endswith(".local") or Zeroconf is None or ServiceInfo is None or IPVersion is None:
        return None

    ip = _lan_ipv4()
    if not ip:
        logging.getLogger(__name__).warning("Could not determine LAN IPv4 for %s advertisement", domain)
        return None

    hostname = domain[:-6]
    zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
    info = ServiceInfo(
        "_http._tcp.local.",
        f"{hostname}._http._tcp.local.",
        addresses=[socket.inet_aton(ip)],
        port=port,
        server=f"{hostname}.local.",
        properties={b"path": b"/"},
    )
    try:
        zeroconf.register_service(info)
        logging.getLogger(__name__).info("mDNS: http://%s -> %s:%s", domain, ip, port)
        return zeroconf, info
    except Exception as exc:
        logging.getLogger(__name__).warning("Could not register mDNS for %s: %s", domain, exc)
        zeroconf.close()
        return None


def _stop_mdns(registration: tuple[Zeroconf, ServiceInfo] | None) -> None:
    if not registration:
        return
    zeroconf, info = registration
    try:
        zeroconf.unregister_service(info)
    except Exception:
        pass
    zeroconf.close()


def main() -> None:
    host = os.getenv("MEDIA_API_BIND", "0.0.0.0")
    port = int(os.getenv("MEDIA_API_PORT", "8080"))
    domain = os.getenv("DOMAIN", "").strip().lower().removeprefix("http://").removeprefix("https://").rstrip(".")
    if domain and not domain.endswith(".local"):
        logging.getLogger(__name__).warning(
            "DOMAIN=%s requires a router/hosts DNS record; automatic discovery only supports .local names", domain
        )
    advertise_domain = os.getenv("MEDIA_DISABLE_DOMAIN_ADVERTISEMENT", "0") != "1"

    redirect_server = _start_port80_redirect(port) if advertise_domain and domain and port != 80 else None
    mdns_registration = _start_mdns(domain, 80 if redirect_server else port) if advertise_domain and domain else None

    try:
        uvicorn.run(app, host=host, port=port)
    finally:
        if redirect_server is not None:
            redirect_server.shutdown()
            redirect_server.server_close()
        _stop_mdns(mdns_registration)


if __name__ == "__main__":
    main()

