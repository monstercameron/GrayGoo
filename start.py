"""Start the GrayGoo dashboard and open it in the browser.

    uv run python start.py                 # dashboard on 8150, browser opened
    uv run python start.py --port 8160     # preferred port (8150-8169 are tried)
    uv run python start.py --no-browser    # do not open a browser
    uv run python start.py --status        # report what is running, then exit

If a GrayGoo dashboard already answers on the port, nothing is started again;
the browser just opens it. Otherwise the dashboard starts in this process
(Ctrl+C stops it), apps that were mounted last time are mounted again, and
the browser opens at http://127.0.0.1:<port>/.
"""
import argparse
import importlib.util
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DASHBOARD_FILE = ROOT / "dashboard" / "server.py"
DEFAULT_PORT = 8150
PORT_RANGE = range(8150, 8170)
READY_TIMEOUT = 15.0
HOST = "127.0.0.1"

# A proxy configured for the machine must never be used for localhost checks.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def base_url(port):
    return "http://%s:%d/" % (HOST, port)


def _get_json(url, timeout=1.0):
    """Parsed JSON from GET URL, or None when nothing sensible answers."""
    try:
        with _OPENER.open(url, timeout=timeout) as reply:
            return json.loads(reply.read().decode("utf-8"))
    except (OSError, ValueError):       # refused, timed out, HTTP error, bad JSON
        return None


def is_graygoo(url, timeout=1.0):
    """True when URL is a GrayGoo dashboard (its /api/agent/config has a "live" key)."""
    data = _get_json(url.rstrip("/") + "/api/agent/config", timeout)
    return isinstance(data, dict) and "live" in data


def _bindable(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((HOST, port))
        except OSError:
            return False
    return True


def probe_port(port):
    """"graygoo" (a dashboard answers), "free", or "other" (something else holds it)."""
    if is_graygoo(base_url(port)):
        return "graygoo"
    return "free" if _bindable(port) else "other"


def _range_text():
    return "%d-%d" % (PORT_RANGE.start, PORT_RANGE.stop - 1)


def candidate_ports(preferred):
    """The preferred port first, then the 8150-8169 range."""
    return [preferred] + [p for p in PORT_RANGE if p != preferred]


def pick_port(preferred, probe=probe_port):
    """``(port, status)`` for the first candidate that is free or already a dashboard.

    STATUS is "free" or "graygoo"; ``(None, None)`` when every candidate is taken.
    """
    for port in candidate_ports(preferred):
        status = probe(port)
        if status in ("free", "graygoo"):
            return port, status
    return None, None


def find_dashboard(preferred, check=is_graygoo):
    """The first port (preferred first) where a GrayGoo dashboard answers, or None."""
    for port in candidate_ports(preferred):
        if check(base_url(port)):
            return port
    return None


def wait_ready(url, timeout=READY_TIMEOUT, sleep=time.sleep, check=is_graygoo):
    """True once URL answers as a GrayGoo dashboard; False after TIMEOUT seconds."""
    deadline = time.monotonic() + timeout
    while True:
        if check(url):
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(0.1)


def remembered_mounts(path):
    """``{project id: port}`` of the apps mounted last time (``{}`` if none or corrupt)."""
    import mount
    return mount.read_mounts(path)


def dashboard_listing(url):
    """``(projects, error)`` from GET /api/agent/projects."""
    data = _get_json(url.rstrip("/") + "/api/agent/projects", timeout=5.0)
    if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
        return None, "no project list from %s" % url
    return data["projects"], None


def mount_project(url, project_id, timeout=10.0):
    """``(mount_url, error)``: asks the running dashboard to mount PROJECT_ID."""
    request = urllib.request.Request(
        url.rstrip("/") + "/api/agent/projects/%s/mount" % project_id,
        data=b"", method="POST")
    try:
        with _OPENER.open(request, timeout=timeout) as reply:
            data = json.loads(reply.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        return None, str(exc)
    mount_info = data.get("mount") if isinstance(data, dict) else None
    if not mount_info:
        return None, "the dashboard did not mount it"
    return mount_info["url"], None


def remount(url, remembered, say):
    """Mount each remembered project the dashboard does not have mounted yet.

    Returns ``[(project id, url)]`` for the apps this call mounted.
    """
    listing, err = dashboard_listing(url)
    if err:
        say("could not read the project list: %s" % err)
        return []
    known = {p["id"]: p for p in listing}
    restored = []
    for project_id in remembered:
        project = known.get(project_id)
        if project is None:
            say("skipping %s: that project no longer exists" % project_id)
            continue
        if project.get("mount"):
            continue
        mounted_url, err = mount_project(url, project_id)
        if err:
            say("could not mount %s: %s" % (project_id, err))
        else:
            say("mounted %s at %s" % (project_id, mounted_url))
            restored.append((project_id, mounted_url))
    return restored


def describe_status(url, listing):
    """Lines for --status: the dashboard, its projects and the mounted apps."""
    mounted = [p for p in listing if p.get("mount")]
    lines = ["dashboard running at %s" % url,
             "projects: %d (%s)" % (len(listing), ", ".join(p["id"] for p in listing))]
    if mounted:
        lines.append("mounted apps:")
        lines += ["  %s at %s" % (p["id"], p["mount"]["url"]) for p in mounted]
    else:
        lines.append("mounted apps: none")
    return lines


def _open_browser_safely(opener, url):
    try:
        return bool(opener(url))
    except Exception:                   # no browser on this machine: the URL is printed
        return False


def _load_dashboard():
    """The dashboard's server module, loaded from its file under a private name."""
    spec = importlib.util.spec_from_file_location("graygoo_dashboard_server", DASHBOARD_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None, open_browser=webbrowser.open):
    ap = argparse.ArgumentParser(description="Start the GrayGoo dashboard.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="preferred port (default %d; %s are tried)" % (DEFAULT_PORT, _range_text()))
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser")
    ap.add_argument("--status", action="store_true",
                    help="report whether a dashboard runs, its projects and mounts; exit")
    args = ap.parse_args(argv)
    started = time.perf_counter()

    def say(message):
        print(message, flush=True)

    def elapsed_ms():
        return int((time.perf_counter() - started) * 1000)

    if args.status:
        port = find_dashboard(args.port)
        if port is None:
            say("no GrayGoo dashboard on ports %s" % _range_text())
            return 1
        url = base_url(port)
        listing, err = dashboard_listing(url)
        if err:
            say(err)
            return 1
        for line in describe_status(url, listing):
            say(line)
        return 0

    import agent_session
    mounts_file = Path(agent_session.AGENT_DIR) / "apps" / "mounts.json"

    port, status = pick_port(args.port)
    if port is None:
        say("no free port in %s; close something or pass --port" % _range_text())
        return 1
    url = base_url(port)
    if port != args.port and status == "free":
        say("port %d is taken by another program; using %d" % (args.port, port))

    if status == "graygoo":
        say("dashboard already running at %s (checked in %d ms); not starting another"
            % (url, elapsed_ms()))
        if not args.no_browser:
            say("browser opened at %s" % url
                if _open_browser_safely(open_browser, url)
                else "could not open a browser; open %s yourself" % url)
        return 0

    say("starting dashboard on port %d" % port)
    dashboard = _load_dashboard()
    try:
        server = dashboard.serve(port)
    except OSError as exc:
        say("could not start the dashboard on port %d: %s" % (port, exc))
        return 1
    threading.Thread(target=server.serve_forever, daemon=True).start()

    if not wait_ready(url):
        say("the dashboard did not answer at %s within %d s; see the messages above"
            % (url, READY_TIMEOUT))
        server.shutdown()
        server.server_close()
        return 1
    say("dashboard ready in %d ms at %s" % (elapsed_ms(), url))

    remount(url, sorted(remembered_mounts(mounts_file)), say)

    if args.no_browser:
        say("browser not opened (--no-browser); the dashboard is at %s" % url)
    elif _open_browser_safely(open_browser, url):
        say("browser opened at %s" % url)
    else:
        say("could not open a browser; open %s yourself" % url)
    say("press Ctrl+C to stop the dashboard")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        say("stopping the dashboard")
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
