"""BotsChat gateway channel plugin for Hermes Agent.

A ``kind: platform`` plugin: registers a BasePlatformAdapter that connects
Hermes to a BotsChat server over an outbound WebSocket, so the BotsChat web
UI / mobile apps / CLI can drive Hermes agents.

Enable in config.yaml::

    plugins:
      enabled: [botschat]
    gateway:
      platforms:
        botschat:
          enabled: true

Secrets come from the environment (BOTSCHAT_CLOUD_URL, BOTSCHAT_PAIRING_TOKEN,
optional BOTSCHAT_E2E_PASSWORD) or ``gateway.platforms.botschat.extra``.
"""


def register(ctx):
    """Plugin entry point — called once at Hermes startup.

    The adapter import is deferred into the call so that merely importing this
    package (e.g. under pytest's package-context collection, where the repo
    root ``__init__.py`` is imported by its directory name) does not pull in
    the whole gateway chain.
    """
    from .adapter import register as _register

    return _register(ctx)
