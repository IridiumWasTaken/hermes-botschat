"""Tests for conftest's Hermes-source discovery (no hardcoded paths).

The resolver has three branches: (1) hermes-agent already importable in the
interpreter → nothing to add; (2) ``HERMES_SOURCE`` env override; (3) the
``hermes`` executable on PATH. Branch 1 is exercised implicitly by the rest
of the suite; branches 2 and 3 are unit-tested here.
"""

import builtins
import os
import shutil

import pytest

import conftest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def _block_hermes_cli(monkeypatch):
    """Simulate an interpreter without hermes-agent installed."""

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "hermes_cli":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    monkeypatch.setattr(shutil, "which", lambda _: None)


def test_override_env_wins_when_tree_present(monkeypatch, _block_hermes_cli, tmp_path):
    # A HERMES_SOURCE without gateway/ is rejected…
    monkeypatch.setenv("HERMES_SOURCE", "/nonexistent/tree")
    assert conftest._find_hermes_source() is None

    # …a real tree (shaped like a Hermes checkout) is accepted.
    tmp = _make_fake_hermes_tree(tmp_path)
    monkeypatch.setenv("HERMES_SOURCE", tmp)
    assert conftest._find_hermes_source() == tmp


def test_path_executable_discovery(monkeypatch, _block_hermes_cli, tmp_path):
    tmp = _make_fake_hermes_tree(tmp_path)
    # Fake `hermes` at <tmp>/bin/hermes — root is two levels up.
    monkeypatch.setattr(
        shutil, "which", lambda name: os.path.join(tmp, "bin", "hermes") if name == "hermes" else None
    )
    assert conftest._find_hermes_source() == tmp


def test_no_source_found_returns_none(monkeypatch, _block_hermes_cli):
    assert conftest._find_hermes_source() is None


def _make_fake_hermes_tree(tmp_path):
    root = tmp_path / "hermes-checkout"
    (root / "gateway").mkdir(parents=True)
    (root / "bin").mkdir()
    return str(root)
