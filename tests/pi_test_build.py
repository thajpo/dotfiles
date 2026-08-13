"""Explicit test-only bypass for unit tests that do not build a P1 artifact."""

from __future__ import annotations

from unittest import mock

from scripts.pi_control.installed_builds import InstalledBuildError


def allow_test_only_registered_build_rows() -> mock._patch:
    def verify(store, build_id):
        row = store.conn.execute("SELECT * FROM installed_builds WHERE build_id=? AND status IN ('staged','active')", (build_id,)).fetchone()
        if row is None:
            raise InstalledBuildError("test fixture build is not registered")
        return row

    return mock.patch("scripts.pi_control.launch.verify_registered_build", side_effect=verify)


__all__ = ["allow_test_only_registered_build_rows"]
