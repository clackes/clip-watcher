"""The companion app: a local page to run the suite, watch it work and look at what it made.

    python -m clipwatcher.ui            # http://127.0.0.1:8765

It starts the same commands a person would type (the live lane, the VOD lane, the tests), reads their
progress events (clipwatcher.events), the staging folder, the judge's ledger and the settings, and never
uploads or approves anything itself. It listens on this machine only.
"""


class NotFound(LookupError):
    """What the page asked for is not there: the server answers 404 with this sentence."""
