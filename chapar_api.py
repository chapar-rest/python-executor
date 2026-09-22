"""Objects a pre/post-request script sees: `request`, `response` and `chapar`.

Everything here is built fresh for each script run. A script changes the
request it is given; Chapar applies those changes to a copy of the saved
request before sending it, so the saved request never changes.
"""
import json
import sys
import types

PROTOCOLS = ("http", "grpc", "graphql")
PHASES = ("pre", "post")


class Pairs:
    """An ordered list of key/value pairs that reads like a dict.

    Headers and gRPC metadata match keys case-insensitively; query params
    do not. A key may appear more than once: `pairs[key]` and `get` return
    the first value, `get_all` returns every one, `add` appends another,
    and `pairs[key] = value` replaces them all with one.
    """

    def __init__(self, pairs=None, case_sensitive=False):
        self._case_sensitive = case_sensitive
        self._items = []
        if isinstance(pairs, dict):
            pairs = pairs.items()
        for item in pairs or ():
            key, value = item
            self._items.append((str(key), str(value)))

    def _same(self, a, b):
        return a == b if self._case_sensitive else a.lower() == b.lower()

    def get(self, key, default=None):
        for k, v in self._items:
            if self._same(k, key):
                return v
        return default

    def get_all(self, key):
        return [v for k, v in self._items if self._same(k, key)]

    def add(self, key, value):
        self._items.append((str(key), str(value)))

    def set(self, key, value):
        self[key] = value

    def remove(self, key):
        """Remove every value of key. Removing a missing key does nothing."""
        self._items = [(k, v) for k, v in self._items if not self._same(k, key)]

    def __getitem__(self, key):
        for k, v in self._items:
            if self._same(k, key):
                return v
        raise KeyError(key)

    def __setitem__(self, key, value):
        out, placed = [], False
        for k, v in self._items:
            if self._same(k, key):
                if not placed:
                    out.append((k, str(value)))
                    placed = True
                continue
            out.append((k, v))
        if not placed:
            out.append((str(key), str(value)))
        self._items = out

    def __delitem__(self, key):
        if key not in self:
            raise KeyError(key)
        self.remove(key)

    def __contains__(self, key):
        return any(self._same(k, key) for k, _ in self._items)

    def keys(self):
        seen, out = [], []
        for k, _ in self._items:
            if not any(self._same(k, s) for s in seen):
                seen.append(k)
                out.append(k)
        return out

    def values(self):
        return [self[k] for k in self.keys()]

    def items(self):
        return [(k, self[k]) for k in self.keys()]

    def pairs(self):
        """Every (key, value), duplicates included, in order."""
        return list(self._items)

    def to_dict(self):
        return dict(self.items())

    def __iter__(self):
        return iter(self.keys())

    def __len__(self):
        return len(self.keys())

    def __bool__(self):
        return bool(self._items)

    def __eq__(self, other):
        if isinstance(other, Pairs):
            return self._items == other._items
        if isinstance(other, dict):
            return self.to_dict() == other
        return NotImplemented

    def __repr__(self):
        return "Pairs(%r)" % self._items


def _loads(text, what):
    if text is None or text == "":
        raise ValueError("%s is empty, not JSON" % what)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError("%s is not valid JSON: %s" % (what, e)) from None


class GraphQLRequest:
    """The GraphQL document and its variables. `variables` is a dict when the
    saved variables are valid JSON, otherwise the raw text."""

    def __init__(self, data):
        self.query = data.get("query", "")
        raw = data.get("variables", "")
        self._raw_variables = raw
        try:
            self.variables = json.loads(raw) if raw.strip() else {}
        except (json.JSONDecodeError, AttributeError):
            self.variables = raw

    def _variables_text(self):
        if isinstance(self.variables, str):
            return self.variables
        return json.dumps(self.variables, indent=2)

    def __repr__(self):
        return "GraphQLRequest(query=%r, variables=%r)" % (self.query, self.variables)


class Resolved:
    """Read-only view of the request with {{variables}} filled in, the way
    Chapar is about to send it (or sent it, in a post-request script)."""

    def __init__(self, data):
        data = data or {}
        self.url = data.get("url", "")
        self.headers = Pairs(data.get("headers"))
        self.body = data.get("body", "")
        self.query = Pairs(data.get("query"), case_sensitive=True)
        gql = data.get("graphql")
        self.graphql = GraphQLRequest(gql) if gql is not None else None

    def json(self):
        return _loads(self.body, "resolved request body")


class Request:
    """The request being sent.

    Fields keep their {{variables}}; Chapar resolves them after a
    pre-request script runs. `resolved` shows the filled-in values.

    For gRPC, `url` is the server address, `method` the full method name
    (package.Service/Method), `headers` (also `metadata`) the metadata and
    `body` the JSON message. For GraphQL the document and variables are in
    `request.graphql`.
    """

    def __init__(self, protocol, data):
        data = data or {}
        self.protocol = protocol
        self.url = data.get("url", "")
        self.method = data.get("method", "")
        self.headers = Pairs(data.get("headers"))
        self.body = data.get("body", "")
        self.query = Pairs(data.get("query"), case_sensitive=True)
        self.path_params = dict(data.get("path_params") or {})
        gql = data.get("graphql")
        self.graphql = GraphQLRequest(gql or {}) if protocol == "graphql" else None
        self.resolved = Resolved(data.get("resolved"))
        self._original = self._snapshot()

    @property
    def metadata(self):
        return self.headers

    @metadata.setter
    def metadata(self, value):
        self.headers = value if isinstance(value, Pairs) else Pairs(value)

    def json(self):
        if self.protocol == "graphql" and self.graphql is not None:
            return {"query": self.graphql.query, "variables": self.graphql.variables}
        return _loads(self.body, "request body")

    def set_json(self, obj):
        """Replace the body with obj encoded as JSON."""
        if self.protocol == "graphql":
            raise TypeError("a GraphQL request has no body; set request.graphql.query and request.graphql.variables")
        self.body = json.dumps(obj, indent=2)

    def _snapshot(self):
        snap = {
            "url": self.url,
            "method": self.method,
            "headers": self._pairs(self.headers),
            "body": self.body,
            "query": self._pairs(self.query),
            "path_params": dict(self.path_params),
        }
        if self.graphql is not None:
            snap["graphql"] = {"query": self.graphql.query, "variables": self.graphql._variables_text()}
        return snap

    @staticmethod
    def _pairs(value):
        if isinstance(value, Pairs):
            return [list(p) for p in value.pairs()]
        if isinstance(value, dict):
            return [[str(k), str(v)] for k, v in value.items()]
        return [[str(k), str(v)] for k, v in value or ()]

    def changes(self):
        """The fields the script changed, in wire form."""
        now = self._snapshot()
        out = {}
        for key in ("url", "method", "headers", "body", "query", "path_params"):
            if now[key] != self._original[key]:
                out[key] = now[key]
        if "graphql" in now:
            before, after = self._original["graphql"], now["graphql"]
            if self.graphql._raw_variables and after["variables"] != before["variables"]:
                # Unchanged variables re-encode differently from how they
                # were saved; only report them when their value moved.
                try:
                    same = json.loads(self.graphql._raw_variables) == self.graphql.variables
                except (json.JSONDecodeError, TypeError):
                    same = False
                if same:
                    after = dict(after, variables=before["variables"])
            gql = {k: after[k] for k in after if after[k] != before[k]}
            if gql:
                out["graphql"] = gql
        return out

    def __repr__(self):
        return "Request(%s %s)" % (self.method, self.url)


class Cookie:
    def __init__(self, data):
        self.name = data.get("name", "")
        self.value = data.get("value", "")
        self.domain = data.get("domain", "")
        self.path = data.get("path", "")
        self.expires = data.get("expires", "")
        self.secure = bool(data.get("secure", False))
        self.http_only = bool(data.get("http_only", False))

    def __repr__(self):
        return "Cookie(%s=%s)" % (self.name, self.value)


class Response:
    """The response, in post-request scripts.

    For gRPC, `status_code` is the gRPC code (0 is OK), `status` its name,
    and `metadata` / `trailers` hold what the server sent. For GraphQL,
    `data` and `errors` come from the JSON body.
    """

    def __init__(self, protocol, data):
        data = data or {}
        self.protocol = protocol
        self.status_code = data.get("status_code", 0)
        self.status = data.get("status", "")
        self.headers = Pairs(data.get("headers"))
        self.text = data.get("body", "")
        self.elapsed_ms = data.get("elapsed_ms", 0)
        self.size = data.get("size", 0)
        self.error = data.get("error") or None
        self.cookies = [Cookie(c) for c in data.get("cookies") or ()]
        self.metadata = Pairs(data.get("metadata"))
        self.trailers = Pairs(data.get("trailers"))

    @property
    def body(self):
        return self.text

    @property
    def ok(self):
        if self.error:
            return False
        if self.protocol == "grpc":
            return self.status_code == 0
        return 200 <= self.status_code < 300

    def json(self):
        return _loads(self.text, "response body")

    def cookie(self, name, default=None):
        for c in self.cookies:
            if c.name == name:
                return c.value
        return default

    def _graphql(self, key):
        try:
            doc = json.loads(self.text)
        except (json.JSONDecodeError, TypeError):
            return None
        return doc.get(key) if isinstance(doc, dict) else None

    @property
    def data(self):
        return self._graphql("data")

    @property
    def errors(self):
        return self._graphql("errors")

    def __repr__(self):
        return "Response(%s %s)" % (self.status_code, self.status)


class Env:
    """The active environment. Values read are strings; values set may be any
    type and are stored as text (JSON for lists and dicts)."""

    def __init__(self, name, values):
        self.name = name or ""
        self._values = dict(values or {})
        self._set = {}
        self._unset = []

    def get(self, key, default=None):
        return self._values.get(key, default)

    def set(self, key, value):
        key = str(key)
        self._values[key] = value
        self._set[key] = value
        if key in self._unset:
            self._unset.remove(key)

    def unset(self, key):
        key = str(key)
        self._values.pop(key, None)
        self._set.pop(key, None)
        if key not in self._unset:
            self._unset.append(key)

    def all(self):
        return dict(self._values)

    def __getitem__(self, key):
        return self._values[key]

    def __setitem__(self, key, value):
        self.set(key, value)

    def __delitem__(self, key):
        self.unset(key)

    def __contains__(self, key):
        return key in self._values

    def __repr__(self):
        return "Env(%r)" % self.name


class SkipRequest(Exception):
    """Raised by chapar.skip() to stop the script and cancel the send."""


class Context:
    """What one script run reads and produces."""

    def __init__(self, phase, protocol, request, response, env):
        self.phase = phase
        self.protocol = protocol
        self.request = request
        self.response = response
        self.env = env
        self.tests = []
        self.skip_reason = None
        self.on_response = None


def make_module(ctx, script_filename):
    """Build the `chapar` module for one run."""
    mod = types.ModuleType("chapar")
    mod.__doc__ = "Chapar scripting API."
    mod.__file__ = "<chapar>"

    mod.phase = ctx.phase
    mod.protocol = ctx.protocol
    mod.request = ctx.request
    mod.response = ctx.response
    mod.env = ctx.env
    mod.Pairs = Pairs

    def get_env(name, default=None):
        return ctx.env.get(name, default)

    def set_env(name, value):
        ctx.env.set(name, value)

    def unset_env(name):
        ctx.env.unset(name)

    def log(*args, sep=" "):
        print(*args, sep=sep)

    def skip(reason=""):
        """Stop the script and do not send the request (pre-request only)."""
        if ctx.phase != "pre":
            raise RuntimeError("chapar.skip() only works in a pre-request script")
        ctx.skip_reason = str(reason)
        raise SkipRequest(reason)

    def _run_test(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - a failing test records any error
            ctx.tests.append({"name": name, "passed": False, "error": _describe(e, script_filename)})
            return False
        ctx.tests.append({"name": name, "passed": True, "error": None})
        return True

    def test(name, fn=None):
        """Run fn and record whether it passed. Without fn, use as a decorator:

            @chapar.test("status is 200")
            def _():
                assert response.status_code == 200
        """
        if fn is not None:
            return _run_test(str(name), fn)

        def decorator(f):
            _run_test(str(name), f)
            return f

        return decorator

    mod.get_env = get_env
    mod.set_env = set_env
    mod.unset_env = unset_env
    mod.log = log
    mod.skip = skip
    mod.test = test
    mod.on_response = None
    return mod


def _describe(exc, script_filename):
    """One line for a failed test: the message, or where the bare assert is."""
    msg = str(exc)
    if msg:
        return "%s: %s" % (type(exc).__name__, msg) if not isinstance(exc, AssertionError) else msg
    line = None
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == script_filename:
            line = tb.tb_lineno
        tb = tb.tb_next
    what = "assertion failed" if isinstance(exc, AssertionError) else type(exc).__name__
    return "%s at line %d" % (what, line) if line else what


def install(mod):
    sys.modules["chapar"] = mod
