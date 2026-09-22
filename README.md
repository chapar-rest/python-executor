Chapar Python executor
======================

Runs the Python pre- and post-request scripts of [Chapar](https://github.com/chapar-rest/chapar)
requests (HTTP, gRPC and GraphQL). Chapar starts it as a Docker container and
calls it over HTTP on localhost.

Each script runs in a forked child process, so scripts share no state with
each other, and a script that runs past its timeout is killed.

## Writing scripts

Three names are defined in every script:

| Name | What it is |
|---|---|
| `request` | The request. A pre-request script may change it. The changes apply to that one send, never to the saved request. |
| `response` | The response in a post-request script. `None` in a pre-request script. |
| `chapar` | Environment, tests, logging and skipping. `import chapar` works too. |

```python
# pre-request: sign the body and send the signature as a header
import hashlib, hmac
key = chapar.env.get("signing_key").encode()
body = request.resolved.body.encode()            # {{variables}} filled in
request.headers["X-Signature"] = hmac.new(key, body, hashlib.sha256).hexdigest()

if not chapar.env.get("token"):
    chapar.skip("log in first")                    # the request is not sent
```

```python
# post-request: keep the token and check the response
chapar.env.set("token", response.json()["token"])

@chapar.test("status is 200")
def _():
    assert response.status_code == 200

print("took", response.elapsed_ms, "ms")          # shown in the request timeline
```

### `request`

| Field | HTTP | gRPC | GraphQL |
|---|---|---|---|
| `url` | URL | server address | endpoint |
| `method` | HTTP method | `package.Service/Method` | `POST` |
| `headers` | headers | metadata (also `request.metadata`) | headers |
| `body`, `json()`, `set_json(obj)` | body | JSON message | — |
| `query` | query params | — | — |
| `path_params` | `{name}` path params | — | — |
| `graphql.query`, `graphql.variables` | — | — | document, variables (a dict) |
| `resolved` | the same request with `{{variables}}` filled in (read-only) | | |

The fields keep their `{{variables}}`. Chapar fills them in after the
pre-request script runs, so a value you set can still use them.

`headers`, `metadata` and `query` are `Pairs`: ordered, dict-like, and able
to hold a key more than once. Header and metadata keys match
case-insensitively. `h[k] = v` replaces every value of a key, `h.add(k, v)`
adds another value, `h.get_all(k)` returns all of them and `h.remove(k)`
removes them.

### `response`

`status_code`, `status`, `ok`, `headers`, `text` / `body`, `json()`,
`elapsed_ms`, `size`, `error`, `cookies` and `cookie(name)`.

- **gRPC:** `status_code` is the gRPC code (0 is OK). `metadata` and
  `trailers` hold what the server sent.
- **GraphQL:** `data` and `errors` come from the JSON body.

### `chapar`

| | |
|---|---|
| `chapar.env.get(k, default)`, `.set(k, v)`, `.unset(k)`, `.all()`, `.name` | The active environment. Values that are not text are stored as JSON. `get_env` / `set_env` / `unset_env` are shorthands. |
| `chapar.test(name, fn)` or `@chapar.test(name)` | Runs a check and reports it in the timeline. |
| `chapar.skip(reason)` | Pre-request only: stops the script and does not send the request. |
| `chapar.log(...)` / `print(...)` | Output shown in the request timeline. |
| `chapar.phase`, `chapar.protocol` | `"pre"`/`"post"` and `"http"`/`"grpc"`/`"graphql"`. |
| `chapar.on_response = fn` | Post-request: called with the response after the script. |

## API

`GET /health` returns `{"status": "ok", "version": "0.3.0", "api": [1, 2]}`.
Chapar checks that `api` contains the version it speaks.

When `CHAPAR_EXECUTOR_TOKEN` is set, every execute call must send it in the
`X-Chapar-Token` header. Chapar sets it to a random value for each container
it starts. The executor runs any code it is sent, so keep it on localhost.

### `POST /v2/execute`

```jsonc
{
  "phase": "pre",                         // or "post"
  "protocol": "http",                     // or "grpc", "graphql"
  "script": "...",
  "timeout_ms": 10000,                    // at most 60000
  "environment": {"name": "dev", "vars": {"token": "..."}},
  "request": {
    "url": "...", "method": "POST", "headers": [["k", "v"]], "body": "...",
    "query": [["k", "v"]], "path_params": {"id": "7"},        // HTTP
    "graphql": {"query": "...", "variables": "{...}"},        // GraphQL
    "resolved": { /* the same fields, variables filled in */ }
  },
  "response": {                           // post only
    "status_code": 200, "status": "200 OK", "headers": [["k", "v"]], "body": "...",
    "elapsed_ms": 12.5, "size": 123, "error": "", "cookies": [{"name": "...", "value": "..."}],
    "metadata": [["k", "v"]], "trailers": [["k", "v"]]        // gRPC
  }
}
```

A 200 response comes back even when the script raised. A 400 means the call
itself was malformed.

```jsonc
{
  "request": {"headers": [["k", "v"]]},   // pre: only the fields the script changed; post: null
  "env_set": {"token": "abc"},
  "env_unset": ["old"],
  "prints": ["..."],
  "tests": [{"name": "...", "passed": false, "error": "assertion failed at line 4"}],
  "skip": false, "skip_reason": "",
  "error": {"type": "KeyError", "message": "'x'", "line": 4, "traceback": "..."}   // or null
}
```

### `POST /execute`

This is the original API, kept for older Chapar builds. It runs a
post-request script and returns
`{"environments", "set_environments", "prints"}`.

## Development

```sh
pip install -r requirements.txt
python -m unittest -v            # tests
python main.py                   # serves on 127.0.0.1:2397
```
