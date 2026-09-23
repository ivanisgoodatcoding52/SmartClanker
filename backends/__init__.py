"""Registry of available quiz backends. Add new backends here."""

from .qbreader import QBReaderBackend

BACKENDS = {
    "qbreader": QBReaderBackend,
}


def get_backend_class(name: str):
    try:
        return BACKENDS[name]
    except KeyError:
        available = ", ".join(BACKENDS)
        raise ValueError(f"Unknown backend '{name}'. Available: {available}")
