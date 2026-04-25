"""Plan 2: get_chrome_path() prefers Chrome for Testing if installed.

The apply layer requires --load-extension to be honored. Branded Chrome
Stable/Beta/Dev/Canary silently reject the flag since Chrome 137. CfT is
the supported automation Chrome that still accepts it. The fix in
config.get_chrome_path puts CfT at the head of the candidate list on
Linux so that any installed CfT wins over a system google-chrome.
"""
from __future__ import annotations

import platform
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.mark.skipif(platform.system() != "Linux", reason="CfT preference is Linux-only")
class TestChromePathCftPreference:
    def test_cft_preferred_when_installed(self, tmp_path, monkeypatch):
        """If CfT is at ~/.applypilot/chrome-for-testing/..., it wins over
        any system google-chrome on PATH."""
        # Pretend $HOME points at tmp_path
        fake_home = tmp_path
        cft_bin = fake_home / ".applypilot" / "chrome-for-testing" / "chrome-linux64" / "chrome"
        cft_bin.parent.mkdir(parents=True)
        cft_bin.write_text("#!/bin/sh\necho fake-cft\n")
        cft_bin.chmod(0o755)

        # Pretend google-chrome exists on PATH
        sys_chrome = tmp_path / "bin" / "google-chrome"
        sys_chrome.parent.mkdir()
        sys_chrome.write_text("#!/bin/sh\necho fake-system-chrome\n")
        sys_chrome.chmod(0o755)

        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("PATH", str(sys_chrome.parent))
        monkeypatch.delenv("CHROME_PATH", raising=False)

        # Reload config so Path.home() picks up the new HOME
        import importlib

        import applypilot.config as config_module
        importlib.reload(config_module)

        result = config_module.get_chrome_path()
        assert result == str(cft_bin), \
            f"Expected CfT at {cft_bin}, got {result}"

    def test_falls_back_to_system_chrome_when_cft_missing(self, tmp_path, monkeypatch):
        """If CfT is NOT installed, the system google-chrome (or chromium) wins."""
        fake_home = tmp_path  # No CfT subtree under here

        sys_chrome = tmp_path / "bin" / "google-chrome"
        sys_chrome.parent.mkdir()
        sys_chrome.write_text("#!/bin/sh\necho fake-system-chrome\n")
        sys_chrome.chmod(0o755)

        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("PATH", str(sys_chrome.parent))
        monkeypatch.delenv("CHROME_PATH", raising=False)

        import importlib

        import applypilot.config as config_module
        importlib.reload(config_module)

        result = config_module.get_chrome_path()
        assert result == str(sys_chrome), \
            f"Expected system chrome at {sys_chrome}, got {result}"

    def test_chrome_path_env_overrides_cft(self, tmp_path, monkeypatch):
        """CHROME_PATH env var overrides everything, including CfT."""
        fake_home = tmp_path
        cft_bin = fake_home / ".applypilot" / "chrome-for-testing" / "chrome-linux64" / "chrome"
        cft_bin.parent.mkdir(parents=True)
        cft_bin.write_text("#!/bin/sh\n")
        cft_bin.chmod(0o755)

        override = tmp_path / "my-custom-chrome"
        override.write_text("#!/bin/sh\n")
        override.chmod(0o755)

        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("CHROME_PATH", str(override))

        import importlib

        import applypilot.config as config_module
        importlib.reload(config_module)

        result = config_module.get_chrome_path()
        assert result == str(override), f"CHROME_PATH override failed: got {result}"
