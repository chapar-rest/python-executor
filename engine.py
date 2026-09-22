"""Runs one pre/post-request script and reports what it did.

Each run happens in a forked child process when the platform allows it, so
a script cannot leak state (imports, globals, monkey-patches) into the next
one, and a script that runs too long is killed without taking the server
down with it.
"""
import builtins
import contextlib
import io
import linecache
import multiprocessing
import traceback

import chapar_api as api

DEFAULT_TIMEOUT_MS = 10_000
MAX_TIMEOUT_MS = 60_000
MAX_OUTPUT_CHARS = 256 * 1024

SCRIPT_FILENAMES = {"pre": "<pre-request>", "post": "<post-request>"}


class PayloadError(ValueError):
    """The request to the executor is malformed (not the script's fault)."""


def validate(payload):
    if not isinstance(payload, dict):
        raise PayloadError("body must be a JSON object")
    phase = payload.get("phase")
    if phase not in api.PHASES:
        raise PayloadError("phase must be one of %s" % ", ".join(api.PHASES))
    protocol = payload.get("protocol", "http")
    if protocol not in api.PROTOCOLS:
        raise PayloadError("protocol must be one of %s" % ", ".join(api.PROTOCOLS))
    if not isinstance(payload.get("script", ""), str):
        raise PayloadError("script must be a string")


def timeout_seconds(payload):
    ms = payload.get("timeout_ms") or DEFAULT_TIMEOUT_MS
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        ms = DEFAULT_TIMEOUT_MS
    return max(1, min(ms, MAX_TIMEOUT_MS)) / 1000


class _Capped(io.StringIO):
    """stdout that stops growing past MAX_OUTPUT_CHARS."""

    truncated = False

    def write(self, s):
        room = MAX_OUTPUT_CHARS - self.tell()
        if room <= 0:
            self.truncated = True
            return len(s)
        if len(s) > room:
            self.truncated = True
            super().write(s[:room])
            return len(s)
        return super().write(s)


def _error(exc, filename):
    """Describe exc with only the frames that are in the user's script."""
    te = traceback.TracebackException.from_exception(exc)
    te.stack = traceback.StackSummary.from_list([f for f in te.stack if f.filename == filename])
    line = None
    if isinstance(exc, SyntaxError) and exc.filename == filename:
        line = exc.lineno
    elif te.stack:
        line = te.stack[-1].lineno
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "line": line,
        "traceback": "".join(te.format()),
    }


def _env_value(v):
    # JSON can carry any of these; anything else becomes its text.
    if v is None or isinstance(v, (str, int, float, bool, list, dict)):
        return v
    return str(v)


def run(payload):
    """Run the script in this process. Returns the result dict."""
    phase = payload["phase"]
    protocol = payload.get("protocol", "http")
    filename = SCRIPT_FILENAMES[phase]
    script = payload.get("script", "")

    env_data = payload.get("environment") or {}
    env = api.Env(env_data.get("name"), env_data.get("vars"))
    request = api.Request(protocol, payload.get("request"))
    response = api.Response(protocol, payload.get("response")) if phase == "post" else None
    ctx = api.Context(phase, protocol, request, response, env)
    mod = api.make_module(ctx, filename)
    api.install(mod)

    # Tracebacks read source lines through linecache.
    linecache.cache[filename] = (len(script), None, script.splitlines(True), filename)

    namespace = {
        "__name__": "__main__",
        "__builtins__": builtins,
        "chapar": mod,
        "request": request,
        "response": response,
    }
    out = _Capped()
    error = None
    skipped = False
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            exec(compile(script, filename, "exec"), namespace)
            if phase == "post" and callable(mod.on_response):
                mod.on_response(response)
        except api.SkipRequest:
            skipped = True
        except BaseException as e:  # noqa: BLE001 - SystemExit and friends are script errors too
            error = _error(e, filename)

    prints = out.getvalue().splitlines()
    if out.truncated:
        prints.append("… output truncated at %d characters" % MAX_OUTPUT_CHARS)

    result = {
        "request": request.changes() if phase == "pre" and error is None else None,
        "env_set": {k: _env_value(v) for k, v in env._set.items()},
        "env_unset": list(env._unset),
        "prints": prints,
        "tests": ctx.tests,
        "skip": skipped,
        "skip_reason": ctx.skip_reason or "",
        "error": error,
    }
    return result


def _child(payload, conn):
    try:
        conn.send(run(payload))
    except BaseException as e:  # noqa: BLE001 - report anything, e.g. an unpicklable result
        conn.send({"error": {"type": type(e).__name__, "message": str(e), "line": None, "traceback": ""}})
    finally:
        conn.close()


def _fork_context():
    if "fork" in multiprocessing.get_all_start_methods():
        return multiprocessing.get_context("fork")
    return None


def execute(payload):
    """Run the script isolated in a child process, killed after its timeout.

    Where fork is unavailable (Windows) it runs in-process with no timeout.
    """
    validate(payload)
    ctx = _fork_context()
    if ctx is None:
        return run(payload)

    timeout = timeout_seconds(payload)
    recv, send = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child, args=(payload, send), daemon=True)
    proc.start()
    send.close()
    try:
        if recv.poll(timeout):
            try:
                return recv.recv()
            except EOFError:
                proc.join(1)
                return _failed("ProcessExited", "script process exited unexpectedly (exit code %s)" % proc.exitcode)
        proc.kill()
        return _failed("TimeoutError", "script did not finish within %g seconds" % timeout)
    finally:
        recv.close()
        proc.join(1)


def _failed(kind, message):
    return {
        "request": None,
        "env_set": {},
        "env_unset": [],
        "prints": [],
        "tests": [],
        "skip": False,
        "skip_reason": "",
        "error": {"type": kind, "message": message, "line": None, "traceback": ""},
    }
