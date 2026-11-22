"""TLS context shared by all outbound HTTP clients."""

import ssl

import truststore


def verified_context() -> ssl.SSLContext:
    """Use OS-trusted roots without disabling chain or hostname verification."""
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
