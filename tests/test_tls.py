import asyncio
import json
import ssl
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import jwt
import pytest
import requests
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from ms_graph_mcp.app import create_app
from ms_graph_mcp.auth import EntraVerifier
from ms_graph_mcp.obo import OboClient, _EntraSession
from ms_graph_mcp.tls import create_ssl_context


@pytest.fixture
def enterprise_server(tmp_path, signing_key):
    now = datetime.now(UTC)
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test enterprise root")])
    root = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()), critical=False
        )
        .sign(root_key, hashes.SHA256())
    )
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(name)
        .public_key(signing_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()),
            critical=False,
        )
        .sign(root_key, hashes.SHA256())
    )
    ca_path = tmp_path / "root.pem"
    ca_path.write_bytes(root.public_bytes(serialization.Encoding.PEM))
    cert_path = tmp_path / "server.pem"
    cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path = tmp_path / "key.pem"
    key_path.write_bytes(
        signing_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_json = jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
    key_json["kid"] = "test-key"
    body = json.dumps({"keys": [key_json], "id": "test-user"}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield ca_path, f"https://localhost:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize("client_kind", ["graph", "obo", "jwks"])
@pytest.mark.parametrize("trusted", [True, False])
async def test_real_tls_on_every_outbound_stack(settings, enterprise_server, client_kind, trusted):
    ca_path, url = enterprise_server
    if trusted:
        settings.extra_ca_file = ca_path
    context = create_ssl_context(settings)
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
    # Custom roots augment, rather than replace, the public root store.
    assert context.cert_store_stats()["x509_ca"] > 10

    async def request():
        if client_kind == "graph":
            async with httpx.AsyncClient(verify=context, trust_env=False) as client:
                return (await client.get(url)).json()
        if client_kind == "obo":

            def get():
                with _EntraSession(2, context) as client:
                    return client.get(url).json()

            return await asyncio.to_thread(get)
        verifier = EntraVerifier(settings, OboClient(settings))
        verifier.jwks.uri = url
        return await asyncio.to_thread(verifier.jwks.get_jwk_set)

    if trusted:
        assert await request()
    else:
        with pytest.raises(
            (httpx.ConnectError, requests.exceptions.SSLError, jwt.PyJWKClientConnectionError)
        ):
            await request()


@pytest.mark.parametrize("client_kind", ["graph", "obo", "jwks"])
async def test_custom_ca_does_not_disable_hostname_checks(settings, enterprise_server, client_kind):
    ca_path, url = enterprise_server
    settings.extra_ca_file = ca_path
    context = create_ssl_context(settings)
    url = url.replace("localhost", "127.0.0.1")
    if client_kind == "graph":
        async with httpx.AsyncClient(verify=context, trust_env=False) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(url)
    elif client_kind == "obo":
        with _EntraSession(2, context) as client:
            with pytest.raises(requests.exceptions.SSLError):
                await asyncio.to_thread(client.get, url)
    else:
        verifier = EntraVerifier(settings, OboClient(settings))
        verifier.jwks.uri = url
        with pytest.raises(jwt.PyJWKClientConnectionError):
            await asyncio.to_thread(verifier.jwks.get_jwk_set)


def test_invalid_pem_fails_before_serving(settings, tmp_path):
    path = tmp_path / "invalid.pem"
    path.write_text("not a certificate")
    settings.extra_ca_file = path
    with pytest.raises(ssl.SSLError):
        OboClient(settings)


@pytest.fixture
def https_app(settings, enterprise_server):
    ca_path, _ = enterprise_server
    started = threading.Event()

    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            started.set()

    config = uvicorn.Config(
        create_app(settings),
        host="127.0.0.1",
        port=0,
        ssl_certfile=str(ca_path.parent / "server.pem"),
        ssl_keyfile=str(ca_path.parent / "key.pem"),
        access_log=False,
        proxy_headers=False,
        log_level="error",
    )
    listener = config.bind_socket()
    port = listener.getsockname()[1]
    server = Server(config)
    worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    worker.start()
    try:
        assert started.wait(3)
        yield ca_path, f"https://localhost:{port}"
    finally:
        server.should_exit = True
        worker.join(timeout=3)
        listener.close()
        assert not worker.is_alive()


async def test_real_mcp_https_listener_and_server_certificate_verification(https_app):
    ca_path, url = https_app
    context = ssl.create_default_context(cafile=str(ca_path))
    async with httpx.AsyncClient(verify=context, trust_env=False, timeout=2) as client:
        assert (await client.get(url + "/healthz")).status_code == 200
        response = await client.post(
            url + "/mcp",
            json={},
            headers={"Host": "testserver", "Accept": "application/json, text/event-stream"},
        )
        assert response.status_code == 401
        with pytest.raises(httpx.HTTPError):
            await client.get(url.replace("https://", "http://") + "/healthz")
        with pytest.raises(httpx.ConnectError):
            await client.get(url.replace("localhost", "127.0.0.1") + "/healthz")
    async with httpx.AsyncClient(trust_env=False, timeout=2) as untrusted:
        with pytest.raises(httpx.ConnectError):
            await untrusted.get(url + "/healthz")
