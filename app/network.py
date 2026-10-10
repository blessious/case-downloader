from __future__ import annotations

import ssl
import sys

import certifi


def trusted_ssl_context() -> ssl.SSLContext:
    """Use Mozilla roots plus the operating-system roots available on Windows."""
    if sys.platform == "win32":
        # CryptoAPI can validate chains whose intermediate CA is omitted by
        # the server; copying only ROOT certificates into OpenSSL cannot.
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context = ssl.create_default_context(cafile=certifi.where())
    enum_certificates = getattr(ssl, "enum_certificates", None)
    if not enum_certificates:
        return context
    for certificate, encoding, _trust in enum_certificates("ROOT"):
        if encoding != "x509_asn":
            continue
        try:
            context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(certificate))
        except ssl.SSLError:
            continue
    return context


async def source_request(client, method: str, url: str, **kwargs):
    """Bounded retries for read-only source searches and decision fetches."""
    import asyncio
    import random
    import httpx
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime
    for attempt in range(3):
        try:
            response = await client.request(method, url, **kwargs)
            if response.status_code != 429 and response.status_code < 500:
                response.raise_for_status()
                return response
            if attempt == 2:
                response.raise_for_status()
            raw = response.headers.get("Retry-After", "")
            try:
                delay = float(raw)
            except ValueError:
                try:
                    delay = (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError):
                    delay = (2 if attempt == 0 else 5) + random.random()
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt == 2:
                raise
            delay = (2 if attempt == 0 else 5) + random.random()
        await asyncio.sleep(min(60, max(0, delay)))
    raise RuntimeError("Source request exhausted")
