"""UI-agnostic application foundation. Importing this package starts no threads."""

__all__ = ["AppCore"]


def __getattr__(name):
    if name == "AppCore":
        from .app import AppCore
        return AppCore
    raise AttributeError(name)
