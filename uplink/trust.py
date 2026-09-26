"""The one set of TLS anchors (R5): the CA bundle that shipped with the build, nothing else."""

import errno
import hashlib
import re
import ssl
from pathlib import Path

from uplink.causes import Cause, UplinkError

DEBIAN_CA_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")
_PEM_CERTIFICATE = re.compile(
    rb"-----BEGIN CERTIFICATE-----[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----")


class Trust:
    """One set of anchors per process. Project 1 builds only the public list. The deferred
    private CA is a second constructor, never an addition to this one."""

    __slots__ = ("_context", "_anchors", "_sha256")

    def __init__(self, context: ssl.SSLContext, *, anchors: int, sha256: str) -> None:
        self._context, self._anchors, self._sha256 = context, anchors, sha256

    @classmethod
    def public(cls, bundle: Path = DEBIAN_CA_BUNDLE) -> "Trust":
        """Load ONLY `bundle` (no OpenSSL default paths) into ssl.SSLContext(PROTOCOL_TLS_CLIENT),
        with every setting explicit so no Python version's defaults matter: CERT_REQUIRED,
        hostname check on (subject alternative names only), TLS 1.2 minimum,
        VERIFY_X509_TRUSTED_FIRST | VERIFY_X509_STRICT | VERIFY_X509_PARTIAL_CHAIN, no CRL
        checks. A missing or unreadable bundle, or one with no CA certificates, raises
        UplinkError(TLS, "trust_store")."""
        try:
            data = bundle.read_bytes()
        except OSError as error:
            raise UplinkError(Cause.TLS, "trust_store",
                              detail=errno.errorcode.get(error.errno, type(error).__name__)
                              ) from error
        # Only the certificate blocks: text between them (a comment, even a non-ASCII one)
        # is not the loader's business, and cadata takes ASCII only.
        certificates = b"\n".join(_PEM_CERTIFICATE.findall(data))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        context.hostname_checks_common_name = False
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.MAXIMUM_SUPPORTED
        # TRUSTED_FIRST is OpenSSL's own default, restated so that setting the flags here
        # does not clear it: it lets a local anchor end a chain the server cross-signed.
        context.verify_flags = (ssl.VERIFY_X509_TRUSTED_FIRST | ssl.VERIFY_X509_STRICT
                                | ssl.VERIFY_X509_PARTIAL_CHAIN)
        try:
            if certificates:
                context.load_verify_locations(cadata=certificates.decode("ascii"))
        except ssl.SSLError as error:
            raise UplinkError(Cause.TLS, "trust_store", detail="unparsable") from error
        anchors = context.cert_store_stats()["x509_ca"]
        if not anchors:
            raise UplinkError(Cause.TLS, "trust_store", detail="no_ca")
        return cls(context, anchors=anchors, sha256=hashlib.sha256(data).hexdigest())

    @property
    def context(self) -> ssl.SSLContext:
        """Project 2 hands this same object to httpx and websockets."""
        return self._context

    @property
    def anchors(self) -> int:
        """CA certificates loaded."""
        return self._anchors

    @property
    def sha256(self) -> str:
        """Hex digest of the bundle bytes as loaded."""
        return self._sha256
