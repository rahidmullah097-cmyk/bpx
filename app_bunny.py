import os
from typing import Dict, Iterable, Tuple

import requests
from flask import Flask, Response, jsonify, request
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


APP_PORT = int(os.getenv("PORT", "8080"))
TARGET_HEADER = "X-Target-Url"
CONNECT_TIMEOUT = float(os.getenv("PROXY_CONNECT_TIMEOUT", "5"))
READ_TIMEOUT = float(os.getenv("PROXY_READ_TIMEOUT", "20"))
VERIFY_TLS = os.getenv("PROXY_VERIFY_TLS", "false").lower() in {"1", "true", "yes", "on"}
SESSION_POOL_CONNECTIONS = int(os.getenv("SESSION_POOL_CONNECTIONS", "200"))
SESSION_POOL_MAXSIZE = int(os.getenv("SESSION_POOL_MAXSIZE", "200"))

UPSTREAM_HTTP_PROXY = os.getenv("UPSTREAM_HTTP_PROXY", "").strip()
UPSTREAM_HTTPS_PROXY = os.getenv("UPSTREAM_HTTPS_PROXY", "").strip()

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

REQUEST_HEADERS_TO_DROP = {
    "host",
    "content-length",
    TARGET_HEADER.lower(),
    "x-real-ip",
    "true-client-ip",
    "cf-connecting-ip",
}

RESPONSE_HEADERS_TO_DROP = {
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
}


app = Flask(__name__)


def build_session() -> requests.Session:
    session = requests.Session()

    retry = Retry(
        total=0,
        connect=0,
        read=0,
        redirect=0,
        status=0,
        backoff_factor=0,
        allowed_methods=False,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        pool_connections=SESSION_POOL_CONNECTIONS,
        pool_maxsize=SESSION_POOL_MAXSIZE,
        max_retries=retry,
    )

    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": "bunny-proxy/1.0"})

    proxies = {}
    if UPSTREAM_HTTP_PROXY:
        proxies["http"] = UPSTREAM_HTTP_PROXY
    if UPSTREAM_HTTPS_PROXY:
        proxies["https"] = UPSTREAM_HTTPS_PROXY
    if proxies:
        session.proxies.update(proxies)

    return session


session = build_session()


def filter_request_headers(headers: Iterable[Tuple[str, str]]) -> Dict[str, str]:
    forwarded = {}
    for key, value in headers:
        key_lower = key.lower()
        if key_lower in HOP_BY_HOP_HEADERS:
            continue
        if key_lower in REQUEST_HEADERS_TO_DROP:
            continue
        if key_lower.startswith("x-forwarded-"):
            continue
        if key_lower.startswith("cdn-"):
            continue
        forwarded[key] = value
    return forwarded


def filter_response_headers(headers: Iterable[Tuple[str, str]]) -> list[Tuple[str, str]]:
    filtered = []
    for key, value in headers:
        if key.lower() in RESPONSE_HEADERS_TO_DROP:
            continue
        filtered.append((key, value))
    return filtered


def request_meta() -> Dict[str, object]:
    return {
        "remote_addr": request.headers.get("X-Forwarded-For", request.remote_addr),
        "method": request.method,
        "path": request.full_path if request.query_string else request.path,
    }


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "status": "healthy"}), 200


@app.get("/ready")
def ready():
    return (
        jsonify(
            {
                "ok": True,
                "status": "ready",
                "tls_verify": VERIFY_TLS,
                "upstream_http_proxy": bool(UPSTREAM_HTTP_PROXY),
                "upstream_https_proxy": bool(UPSTREAM_HTTPS_PROXY),
            }
        ),
        200,
    )


@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
def proxy(path: str):
    _ = path
    target_url = request.headers.get(TARGET_HEADER, "").strip()
    if not target_url:
        return jsonify({"ok": False, "error": f"missing {TARGET_HEADER} header"}), 400

    try:
        upstream_response = session.request(
            method=request.method,
            url=target_url,
            headers=filter_request_headers(request.headers),
            params=request.args,
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False,
            verify=VERIFY_TLS,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            stream=False,
        )

        response_headers = filter_response_headers(upstream_response.raw.headers.items())
        return Response(
            upstream_response.content,
            status=upstream_response.status_code,
            headers=response_headers,
        )
    except requests.Timeout:
        return jsonify(
            {
                "ok": False,
                "error": "upstream timeout",
                "meta": request_meta(),
            }
        ), 504
    except requests.RequestException as exc:
        return jsonify(
            {
                "ok": False,
                "error": "upstream request failed",
                "details": str(exc),
                "meta": request_meta(),
            }
        ), 502
    except Exception as exc:
        return jsonify(
            {
                "ok": False,
                "error": "internal proxy error",
                "details": str(exc),
                "meta": request_meta(),
            }
        ), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=APP_PORT)
