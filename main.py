#!/usr/bin/env python3
"""HTTP front of the Chapar python executor.

POST /v2/execute runs a pre- or post-request script (see README.md for the
contract). POST /execute is the original API, kept for older Chapar builds.
"""
import argparse
import hmac
import os

from flask import Flask, jsonify, request

import engine

VERSION = "0.3.0"
API_VERSIONS = [1, 2]
TOKEN_HEADER = "X-Chapar-Token"

app = Flask(__name__)


def _authorized():
    """When CHAPAR_EXECUTOR_TOKEN is set, every execute call must carry it."""
    token = os.environ.get("CHAPAR_EXECUTOR_TOKEN", "")
    if not token:
        return True
    return hmac.compare_digest(request.headers.get(TOKEN_HEADER, ""), token)


@app.route("/health")
def health_check():
    return jsonify({"status": "ok", "version": VERSION, "api": API_VERSIONS})


@app.route("/v2/execute", methods=["POST"])
def execute_v2():
    if not _authorized():
        return jsonify({"message": "missing or wrong %s header" % TOKEN_HEADER}), 401
    payload = request.get_json(silent=True)
    try:
        result = engine.execute(payload)
    except engine.PayloadError as e:
        return jsonify({"message": str(e)}), 400
    return jsonify(result)


@app.route("/execute", methods=["POST"])
def execute_v1():
    """The original API: a post-request script with a flat request/response."""
    if not _authorized():
        return jsonify({"error": "missing or wrong %s header" % TOKEN_HEADER}), 401
    data = request.get_json(silent=True) or {}
    req = data.get("requestData") or {}
    res = data.get("responseData") or {}
    env = data.get("environments") or data.get("variables") or {}
    payload = {
        "phase": "post",
        "protocol": "grpc" if req.get("metadata") else "http",
        "script": data.get("script", ""),
        "environment": {"vars": env},
        "request": {
            "url": req.get("url", ""),
            "method": req.get("method", ""),
            "headers": req.get("headers") or req.get("metadata") or {},
            "body": req.get("body", ""),
            "query": req.get("QueryParams") or req.get("query") or {},
            "path_params": req.get("pathParams") or req.get("params") or {},
        },
        "response": {
            "status_code": res.get("statusCode", 0),
            "headers": res.get("headers") or {},
            "body": res.get("body", ""),
        },
    }
    result = engine.execute(payload)
    if result["error"]:
        err = result["error"]
        return jsonify({"error": err["message"], "traceback": err["traceback"]}), 400
    merged = dict(env)
    merged.update(result["env_set"])
    return jsonify({
        "environments": merged,
        "set_environments": result["env_set"],
        "prints": result["prints"],
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT", 2397)),
                        help="Port to run the server on")
    parser.add_argument("--host",
                        default=os.environ.get("HOST", "127.0.0.1"),
                        help="Host to run the server on (it runs any code it is sent; keep it local)")
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
