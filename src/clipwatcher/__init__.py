"""Clip Watcher."""

import os as _os

# A fresh Windows Server only has the roots Windows fetched so far; Python's ssl never triggers that fetch, so
# Twitch IRC / Kick / Helix TLS failed with "unable to get local issuer certificate" (19 Nov, new box: chat
# silently empty for hours). ssl.create_default_context() also loads SSL_CERT_FILE, so add certifi's bundle.
if _os.name == "nt" and "SSL_CERT_FILE" not in _os.environ:
    try:
        import certifi as _certifi

        _os.environ["SSL_CERT_FILE"] = _certifi.where()
    except ImportError:
        pass
