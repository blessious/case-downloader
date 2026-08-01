from __future__ import annotations

import ssl

import certifi


def trusted_ssl_context() -> ssl.SSLContext:
    """Use Mozilla roots plus the operating-system roots available on Windows."""
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
