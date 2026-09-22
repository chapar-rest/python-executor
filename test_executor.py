import json
import os
import unittest

import engine
import main


def payload(script, phase="pre", protocol="http", **kw):
    p = {"phase": phase, "protocol": protocol, "script": script}
    p.update(kw)
    return p


HTTP_REQ = {
    "url": "https://{{host}}/users/{id}",
    "method": "POST",
    "headers": [["Content-Type", "application/json"], ["X-Trace", "a"], ["x-trace", "b"]],
    "body": '{"name": "a"}',
    "query": [["page", "1"]],
    "path_params": {"id": "7"},
    "resolved": {"url": "https://api.test/users/7", "headers": [["Content-Type", "application/json"]],
                 "body": '{"name": "a"}'},
}


class EngineTest(unittest.TestCase):
    def test_env_get_set_unset(self):
        r = engine.execute(payload(
            "chapar.env.set('token', chapar.get_env('user') + '!')\n"
            "chapar.set_env('n', 5)\n"
            "chapar.env.unset('old')\n",
            environment={"name": "dev", "vars": {"user": "bob", "old": "x"}},
        ))
        self.assertIsNone(r["error"])
        self.assertEqual(r["env_set"], {"token": "bob!", "n": 5})
        self.assertEqual(r["env_unset"], ["old"])

    def test_pre_script_changes_are_reported(self):
        r = engine.execute(payload(
            "request.headers['Authorization'] = 'Bearer ' + request.resolved.url\n"
            "request.headers['x-trace'] = 'c'\n"
            "body = request.json(); body['name'] = 'b'; request.set_json(body)\n"
            "request.query.add('page', '2')\n",
            request=HTTP_REQ,
        ))
        self.assertIsNone(r["error"])
        ch = r["request"]
        self.assertEqual(ch["headers"], [["Content-Type", "application/json"], ["X-Trace", "c"],
                                         ["Authorization", "Bearer https://api.test/users/7"]])
        self.assertEqual(json.loads(ch["body"]), {"name": "b"})
        self.assertEqual(ch["query"], [["page", "1"], ["page", "2"]])
        self.assertNotIn("url", ch)

    def test_untouched_request_reports_no_changes(self):
        r = engine.execute(payload("x = request.headers.get('content-type')", request=HTTP_REQ))
        self.assertEqual(r["request"], {})

    def test_functions_see_script_globals(self):
        # exec with separate globals/locals used to hide these from functions.
        r = engine.execute(payload(
            "import json\nLIMIT = 3\n"
            "def check():\n    return json.dumps(response.json()['n'] < LIMIT)\n"
            "print(check())\n",
            phase="post", response={"status_code": 200, "body": '{"n": 1}'},
        ))
        self.assertIsNone(r["error"])
        self.assertEqual(r["prints"], ["true"])

    def test_post_response_fields(self):
        r = engine.execute(payload(
            "print(response.ok, response.status, response.elapsed_ms, response.headers['content-type'])\n"
            "print(response.cookie('sid'), response.request if hasattr(response, 'request') else '-')\n"
            "chapar.on_response = lambda res: print('cb', res.status_code)\n",
            phase="post",
            response={"status_code": 201, "status": "201 Created", "elapsed_ms": 12.5,
                      "headers": [["Content-Type", "text/plain"]],
                      "cookies": [{"name": "sid", "value": "abc"}]},
        ))
        self.assertIsNone(r["error"])
        self.assertEqual(r["prints"], ["True 201 Created 12.5 text/plain", "abc -", "cb 201"])
        self.assertIsNone(r["request"])

    def test_grpc(self):
        r = engine.execute(payload(
            "print(response.ok, response.status, response.metadata['x-id'], response.trailers.get('t'))\n"
            "print(request.metadata['auth'], request.json()['id'])\n",
            phase="post", protocol="grpc",
            request={"url": "localhost:50051", "method": "pkg.Svc/Get",
                     "headers": [["auth", "k"]], "body": '{"id": 4}'},
            response={"status_code": 0, "status": "OK", "metadata": [["x-id", "9"]],
                      "trailers": [["t", "z"]], "body": "{}"},
        ))
        self.assertIsNone(r["error"])
        self.assertEqual(r["prints"], ["True OK 9 z", "k 4"])

    def test_grpc_pre_metadata_change(self):
        r = engine.execute(payload(
            "request.metadata['auth'] = 'new'",
            protocol="grpc", request={"headers": [["auth", "old"]]},
        ))
        self.assertEqual(r["request"], {"headers": [["auth", "new"]]})

    def test_graphql(self):
        r = engine.execute(payload(
            "request.graphql.variables['id'] = 2\n",
            protocol="graphql",
            request={"url": "https://g/graphql", "graphql": {"query": "{ me }", "variables": '{"id": 1}'}},
        ))
        self.assertIsNone(r["error"])
        self.assertEqual(json.loads(r["request"]["graphql"]["variables"]), {"id": 2})
        self.assertNotIn("query", r["request"]["graphql"])

        r = engine.execute(payload(
            "print(response.data['me'], response.errors)",
            phase="post", protocol="graphql",
            response={"status_code": 200, "body": '{"data": {"me": "x"}}'},
        ))
        self.assertEqual(r["prints"], ["x None"])

    def test_graphql_unchanged_variables_not_reported(self):
        r = engine.execute(payload(
            "q = request.graphql.variables",
            protocol="graphql", request={"graphql": {"query": "{ a }", "variables": '{"id":1}'}},
        ))
        self.assertEqual(r["request"], {})

    def test_tests_and_skip(self):
        r = engine.execute(payload(
            "chapar.test('ok', lambda: None)\n"
            "@chapar.test('bare assert')\n"
            "def _():\n    assert 1 == 2\n"
            "chapar.test('message', lambda: (_ for _ in ()).throw(AssertionError('nope')))\n"
            "chapar.skip('not today')\n"
            "print('unreached')\n",
        ))
        self.assertIsNone(r["error"])
        self.assertTrue(r["skip"])
        self.assertEqual(r["skip_reason"], "not today")
        self.assertEqual(r["prints"], [])
        self.assertEqual([t["passed"] for t in r["tests"]], [True, False, False])
        self.assertEqual(r["tests"][1]["error"], "assertion failed at line 4")
        self.assertEqual(r["tests"][2]["error"], "nope")

    def test_error_points_at_script_line(self):
        r = engine.execute(payload("x = 1\ny = {}\nprint('before')\ny['missing']\n"))
        self.assertEqual(r["error"]["type"], "KeyError")
        self.assertEqual(r["error"]["line"], 4)
        self.assertIn("<pre-request>", r["error"]["traceback"])
        self.assertNotIn("engine.py", r["error"]["traceback"])
        self.assertEqual(r["prints"], ["before"])
        self.assertIsNone(r["request"])

    def test_syntax_error_line(self):
        r = engine.execute(payload("a = 1\nif a\n  pass\n"))
        self.assertEqual(r["error"]["type"], "SyntaxError")
        self.assertEqual(r["error"]["line"], 2)

    def test_empty_body_json_is_clear(self):
        r = engine.execute(payload("response.json()", phase="post", response={"body": ""}))
        self.assertEqual(r["error"]["message"], "response body is empty, not JSON")

    def test_timeout_kills_script(self):
        if engine._fork_context() is None:
            self.skipTest("no fork")
        r = engine.execute(payload("while True: pass", timeout_ms=300))
        self.assertEqual(r["error"]["type"], "TimeoutError")

    def test_runs_do_not_share_state(self):
        engine.execute(payload("import json; json.dumps = None; chapar.env.set('a', 1)"))
        r = engine.execute(payload("import json; print(json.dumps(1))"))
        self.assertIsNone(r["error"])
        self.assertEqual(r["env_set"], {})

    def test_sys_exit_is_an_error(self):
        r = engine.execute(payload("import sys; sys.exit(3)"))
        self.assertEqual(r["error"]["type"], "SystemExit")

    def test_bad_payload(self):
        with self.assertRaises(engine.PayloadError):
            engine.execute({"phase": "during", "script": ""})


class HTTPTest(unittest.TestCase):
    def setUp(self):
        self.client = main.app.test_client()
        os.environ.pop("CHAPAR_EXECUTOR_TOKEN", None)

    def tearDown(self):
        os.environ.pop("CHAPAR_EXECUTOR_TOKEN", None)

    def test_health(self):
        body = self.client.get("/health").get_json()
        self.assertIn(2, body["api"])
        self.assertEqual(body["version"], main.VERSION)

    def test_token(self):
        os.environ["CHAPAR_EXECUTOR_TOKEN"] = "s3cret"
        p = payload("print(1)")
        self.assertEqual(self.client.post("/v2/execute", json=p).status_code, 401)
        self.assertEqual(self.client.post("/execute", json={}).status_code, 401)
        res = self.client.post("/v2/execute", json=p, headers={"X-Chapar-Token": "s3cret"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["prints"], ["1"])

    def test_bad_payload_is_400(self):
        self.assertEqual(self.client.post("/v2/execute", json={"phase": "x"}).status_code, 400)
        self.assertEqual(self.client.post("/v2/execute", data="nope").status_code, 400)

    def test_v1_compat(self):
        res = self.client.post("/execute", json={
            "script": "chapar.set_env('t', response.json()['t'] + chapar.get_env('p'))\nprint(request.url)",
            "requestData": {"url": "http://x", "headers": {"A": "b"}},
            "responseData": {"statusCode": 200, "body": '{"t": "v"}'},
            "variables": {"p": "!"},
        })
        body = res.get_json()
        self.assertEqual(res.status_code, 200, body)
        self.assertEqual(body["set_environments"], {"t": "v!"})
        self.assertEqual(body["environments"], {"p": "!", "t": "v!"})
        self.assertEqual(body["prints"], ["http://x"])

    def test_v1_error(self):
        res = self.client.post("/execute", json={"script": "1/0"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("division by zero", res.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
