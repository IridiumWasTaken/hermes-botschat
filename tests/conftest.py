"""Make the flat plugin layout importable as the package ``botschat``.

Mirrors Hermes' plugin loader (hermes_cli/plugins.py): the plugin is imported
as a package whose ``__path__`` is the plugin directory, so intra-plugin
imports are RELATIVE (``from .e2e import ...``). Under pytest we recreate that
package context without executing ``__init__.py`` (which would pull in the
whole gateway chain for the crypto/protocol/ws tests).

Tests import the plugin as ``botschat.<module>`` — the same dotted form the
gateway uses at runtime.

Adapter tests additionally import Hermes' ``gateway.*`` modules. Those resolve
from the running interpreter when hermes-agent is installed (pip/editable),
or — under a bare interpreter — from a source tree located via the
``HERMES_SOURCE`` env var or the ``hermes`` executable on PATH. No hardcoded
user paths.
"""

import os
import shutil
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if "botschat" not in sys.modules:
    pkg = types.ModuleType("botschat")
    pkg.__path__ = [ROOT]  # type: ignore[attr-defined]
    pkg.__package__ = "botschat"
    sys.modules["botschat"] = pkg


def _find_hermes_source() -> str | None:
    """Locate a Hermes source tree for the adapter tests.

    Returns the tree root to prepend to ``sys.path``, or ``None`` when the
    Hermes packages are already importable (or cannot be found at all).
    """
    # 1. Already importable in this interpreter (pip/editable install of
    #    hermes-agent) — nothing to add.
    try:
        import hermes_cli  # noqa: F401

        return None
    except ImportError:
        pass

    # 2. Explicit override: HERMES_SOURCE=<path to a Hermes checkout>
    override = os.environ.get("HERMES_SOURCE", "").strip()
    if override and os.path.isdir(os.path.join(override, "gateway")):
        return override

    # 3. The `hermes` executable on PATH — assume a source checkout whose
    #    root (two levels up from bin/) contains gateway/.
    exe = shutil.which("hermes")
    if exe:
        root = os.path.dirname(os.path.dirname(os.path.realpath(exe)))
        if os.path.isdir(os.path.join(root, "gateway")):
            return root

    return None


_src = _find_hermes_source()
if _src is not None and _src not in sys.path:
    sys.path.insert(0, _src)
