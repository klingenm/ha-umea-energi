"""Tests for the integration manifest."""

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_manifest_version_matches_pyproject() -> None:
    # knope bumps both on release; Home Assistant and HACS read the manifest.
    manifest = json.loads((ROOT / "custom_components/umea_energi/manifest.json").read_text())
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert manifest["version"] == pyproject["project"]["version"]
