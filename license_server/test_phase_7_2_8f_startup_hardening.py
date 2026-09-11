#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 7.2-8F: startup hardening.

Two things were wrong with the startup path:

  1. /docs and /redoc were mounted unconditionally, so a production server
     published a complete map of the activation API to anyone who asked.
  2. The `__main__` block called uvicorn with the import string "app.main:app".
     That cannot resolve: this package's layout puts app/ on sys.path and calls
     the module `main`, so `import app.main` fails with
     ModuleNotFoundError: No module named 'config'. It also bound 0.0.0.0 and
     enabled reload whenever DEBUG was set.

Covers:
  [1] static      -- the __main__ block binds loopback, never enables reload,
                     and no longer uses the broken import form
  [2] __main__ executed for real -- main.py is run as __main__ with uvicorn.run
                     intercepted, which proves the block imports and shows the
                     exact arguments it would hand to uvicorn
  [3] docs routing -- /docs and /redoc are mounted only when DEBUG is on
  [4] DEBUG=True  -- /docs and /redoc are served over real HTTP
  [5] DEBUG=False -- /docs and /redoc are gone, the API itself still works
  [6] `python main.py` -- boots for real on 127.0.0.1:8000, without a reloader
                     (skipped when port 8000 is already taken)
  [7] no listener is leaked by these tests

Databases are temporary files. Nothing here touches the repository's own
database or any server already running on this machine.
"""

import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent          # <repo>/license_server
APP = ROOT / "app"
MAIN = APP / "main.py"

PASSED = 0
FAILED = 0
SKIPPED = 0

#: Kept out of the child environment so the run reflects exactly what each check
#: passes in, never the ambient shell.
MANAGED = ("DATABASE_URL", "DEBUG", "SECRET_KEY", "ADMIN_API_KEY", "API_PREFIX",
           "DEFAULT_MAX_DEVICES", "SUSI_HELPER_PATH",
           "SUSI_DEVELOPMENT_PRIVATE_KEY_FILE", "SUSI_DEVELOPMENT_PRIVATE_KEY",
           "SUSI_DEVELOPMENT_PUBLIC_KEY")

CLI_PORT_DEBUG = 8141
CLI_PORT_PROD = 8142
MAIN_PY_PORT = 8000          # fixed in main.py's __main__ block

#: Runs main.py exactly as `python main.py` would, but with uvicorn.run replaced
#: by a recorder, so the block's real arguments can be inspected without binding
#: a port.
MAIN_BLOCK_PROBE = r'''
import json, runpy, sys
sys.path.insert(0, sys.argv[1])          # the app directory
import uvicorn

captured = {}
def record(app, **kwargs):
    captured["app_type"] = type(app).__name__
    captured["title"] = getattr(app, "title", None)
    captured["kwargs"] = sorted(kwargs)
    captured["routes"] = sorted(
        getattr(route, "path", "") for route in getattr(app, "routes", [])
    )
    for key, value in kwargs.items():
        captured[key] = value
uvicorn.run = record

runpy.run_path(sys.argv[2], run_name="__main__")   # executes the real block
print(json.dumps(captured))
'''


def check(cond, msg):
    global PASSED, FAILED, SKIPPED
    if cond:
        PASSED += 1
        print("  [ok]   " + msg, flush=True)
    else:
        FAILED += 1
        print("  [FAIL] " + msg, flush=True)


def skip(msg):
    global SKIPPED
    SKIPPED += 1
    print("  [skip] " + msg, flush=True)


def child_env(extra=None):
    env = dict(os.environ)
    for name in MANAGED:
        env.pop(name, None)
    env.update(extra or {})
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def strip_comments(text):
    """Drop '#'-to-end-of-line comments so static checks read code, not prose."""
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def run_main_block(debug, database_url):
    """Execute the __main__ block with uvicorn intercepted."""
    result = subprocess.run(
        [sys.executable, "-c", MAIN_BLOCK_PROBE, str(APP), str(MAIN)],
        cwd=str(APP),
        env=child_env({"DEBUG": debug, "DATABASE_URL": database_url}),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        return {"__error__": (result.stderr or "").strip().splitlines()[-3:]}
    return json.loads(result.stdout.strip().splitlines()[-1])


def http_get(url, timeout=3):
    """Return (status, body); status is None when the server is not up yet."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception:
        return None, ""


def wait_until_up(port, seconds=30):
    deadline = time.time() + seconds
    while time.time() < deadline:
        status, _ = http_get("http://127.0.0.1:%d/" % port)
        if status == 200:
            return True
        time.sleep(0.3)
    return False


def port_is_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) != 0


class Server:
    """A real uvicorn process, logged to a file so the log can be read back."""

    def __init__(self, argv, port, env_extra, log_path, cwd):
        self.port = port
        self._log = open(log_path, "w+", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            argv, cwd=str(cwd), env=child_env(env_extra),
            stdout=self._log, stderr=subprocess.STDOUT,
        )
        self.up = wait_until_up(port)
        self.log = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self._log.flush()
        self._log.seek(0)
        self.log = self._log.read()
        self._log.close()
        return False


def cli_argv(port):
    """The production start form: app/ on sys.path, module named `main`."""
    return [sys.executable, "-m", "uvicorn", "main:app", "--app-dir", "app",
            "--host", "127.0.0.1", "--port", str(port)]


def temp_db_url(temp, name):
    return "sqlite:///" + (pathlib.Path(temp) / name).as_posix()


if __name__ == "__main__":
    print("=" * 66)
    print("Phase 7.2-8F startup hardening")
    print("=" * 66)

    # ------------------------------------------------------------------
    print("\n[1] static: main.py's __main__ block")
    code = strip_comments(MAIN.read_text(encoding="utf-8"))
    tail = code.split("if __name__", 1)[-1]

    check("uvicorn.run(" in tail, "the block still starts uvicorn")
    check("app.main:app" not in tail,
          "the unimportable 'app.main:app' form is gone")
    # The app object is passed directly rather than as an import string, so
    # `python main.py` cannot execute this module a second time (which would
    # build a second engine and run create_all again).
    check("uvicorn.run(app" in tail, "uvicorn is handed the app object, not a string")
    check('host="127.0.0.1"' in tail, "it binds loopback (127.0.0.1)")
    check("0.0.0.0" not in tail, "it no longer binds all interfaces")
    check("port=8000" in tail, "it uses port 8000")
    check("reload" not in tail,
          "no reload argument is passed (reload is never enabled automatically)")

    with tempfile.TemporaryDirectory(prefix="dlv-8f-") as temp_dir:
        # --------------------------------------------------------------
        print("\n[2] the __main__ block executes (uvicorn intercepted)")
        prod_block = run_main_block("false", temp_db_url(temp_dir, "block.db"))
        check("__error__" not in prod_block,
              "`python main.py`'s import path resolves (no ModuleNotFoundError)")
        if "__error__" in prod_block:
            print("     " + " / ".join(prod_block["__error__"]))
        else:
            check(prod_block.get("app_type") == "FastAPI",
                  "the block passes a constructed FastAPI app")
            check(prod_block.get("kwargs") == ["host", "port"],
                  "the only arguments to uvicorn.run are host and port: %s"
                  % prod_block.get("kwargs"))
            check("reload" not in prod_block.get("kwargs", []),
                  "reload is not among the arguments (never automatic)")
            check(prod_block.get("host") == "127.0.0.1",
                  "host is 127.0.0.1, not 0.0.0.0")
            check(prod_block.get("port") == 8000, "port is 8000")

        # --------------------------------------------------------------
        print("\n[3] docs routes are mounted only when DEBUG is on")
        debug_block = run_main_block("true", temp_db_url(temp_dir, "block_dev.db"))
        check("__error__" not in debug_block, "the block executes with DEBUG=true")
        if "__error__" not in prod_block:
            prod_routes = prod_block.get("routes", [])
            check("/docs" not in prod_routes,
                  "DEBUG=false: no /docs route exists")
            check("/redoc" not in prod_routes,
                  "DEBUG=false: no /redoc route exists")
            check("/" in prod_routes and any(
                r.startswith("/api/v1/licenses") for r in prod_routes),
                "DEBUG=false: the API routes are unaffected")
        if "__error__" not in debug_block:
            debug_routes = debug_block.get("routes", [])
            check("/docs" in debug_routes, "DEBUG=true: /docs route exists")
            check("/redoc" in debug_routes, "DEBUG=true: /redoc route exists")

        # --------------------------------------------------------------
        print("\n[4] DEBUG=True -> docs are served over HTTP")
        with Server(cli_argv(CLI_PORT_DEBUG), CLI_PORT_DEBUG,
                    {"DEBUG": "true", "DATABASE_URL": temp_db_url(temp_dir, "dev.db")},
                    os.path.join(temp_dir, "dev.log"), ROOT) as dev:
            check(dev.up, "the server boots with DEBUG=true")
            if dev.up:
                status, body = http_get("http://127.0.0.1:%d/docs" % CLI_PORT_DEBUG)
                check(status == 200, "/docs returns 200")
                check("swagger" in body.lower(), "/docs serves the Swagger UI")
                status, body = http_get("http://127.0.0.1:%d/redoc" % CLI_PORT_DEBUG)
                check(status == 200, "/redoc returns 200")
                check("redoc" in body.lower(), "/redoc serves the ReDoc UI")

        # --------------------------------------------------------------
        print("\n[5] DEBUG=False -> docs are unavailable")
        with Server(cli_argv(CLI_PORT_PROD), CLI_PORT_PROD,
                    {"DEBUG": "false", "DATABASE_URL": temp_db_url(temp_dir, "prod.db")},
                    os.path.join(temp_dir, "prod.log"), ROOT) as prod:
            check(prod.up, "the server boots with DEBUG=false")
            if prod.up:
                status, body = http_get("http://127.0.0.1:%d/docs" % CLI_PORT_PROD)
                check(status == 404, "/docs returns 404 (got %s)" % status)
                check("swagger" not in body.lower(),
                      "/docs does not leak the Swagger UI")
                status, body = http_get("http://127.0.0.1:%d/redoc" % CLI_PORT_PROD)
                check(status == 404, "/redoc returns 404 (got %s)" % status)
                check("redoc" not in body.lower(),
                      "/redoc does not leak the ReDoc UI")
                status, _ = http_get("http://127.0.0.1:%d/" % CLI_PORT_PROD)
                check(status == 200, "the API root still returns 200")
                status, _ = http_get(
                    "http://127.0.0.1:%d/api/v1/licenses/validate" % CLI_PORT_PROD)
                check(status == 405,
                      "the API routes are still registered (a POST-only route"
                      " answers 405 to GET, not 404): %s" % status)

        # --------------------------------------------------------------
        print("\n[6] `python main.py` boots for real")
        if not port_is_free(MAIN_PY_PORT):
            skip("port %d is already taken by another process (left untouched);"
                 " the block itself is covered by [1]-[3]" % MAIN_PY_PORT)
        else:
            with Server([sys.executable, "main.py"], MAIN_PY_PORT,
                        {"DEBUG": "false",
                         "DATABASE_URL": temp_db_url(temp_dir, "mainpy.db")},
                        os.path.join(temp_dir, "mainpy.log"), APP) as mainpy:
                check(mainpy.up,
                      "`python main.py` serves requests on 127.0.0.1:%d"
                      % MAIN_PY_PORT)
                if mainpy.up:
                    status, _ = http_get("http://127.0.0.1:%d/" % MAIN_PY_PORT)
                    check(status == 200, "the API root returns 200")
                    check("127.0.0.1:%d" % MAIN_PY_PORT in mainpy.log,
                          "uvicorn reports the loopback address")
                    check("0.0.0.0" not in mainpy.log,
                          "uvicorn does not report binding all interfaces")
                    check("reload" not in mainpy.log.lower(),
                          "no reloader process was started")

        # --------------------------------------------------------------
        print("\n[7] these tests leak no listener")
        status, _ = http_get("http://127.0.0.1:%d/" % CLI_PORT_PROD)
        check(status is None, "the DEBUG=false server was shut down")
        status, _ = http_get("http://127.0.0.1:%d/" % CLI_PORT_DEBUG)
        check(status is None, "the DEBUG=true server was shut down")

    print("\n" + "=" * 66)
    print("RESULT: %d/%d checks passed, %d failed, %d skipped"
          % (PASSED, PASSED + FAILED, FAILED, SKIPPED))
    print("=" * 66)
    sys.exit(1 if FAILED else 0)
