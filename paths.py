"""Where this skill keeps its own data.

The skill owns its ledger, evidence, assets and caches; it must not assume a
particular host's directory layout. A host points ``XHH_STATE_ROOT`` at a
writable directory, or accepts the default user-level location.
"""
from __future__ import annotations

import os
from pathlib import Path


def state_root() -> Path:
    override = os.environ.get("XHH_STATE_ROOT")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "alma" / "state"


def scratch_root() -> Path:
    override = os.environ.get("XHH_SCRATCH_ROOT")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "alma" / "scratch"


def home_config() -> Path:
    """Host config directory, only used for optional integration files."""
    override = os.environ.get("XHH_HOST_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "alma"
