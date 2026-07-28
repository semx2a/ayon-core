"""Tests for workfile lock helpers."""
import pytest

from ayon_core.pipeline.workfile.lock_workfile import (
    is_workfile_lock_enabled,
)


def _project_settings(profiles):
    """Build minimal project settings holding lock profiles.

    Args:
        profiles (list[dict]): Workfile lock profiles.

    Returns:
        dict: Project settings subset used by 'is_workfile_lock_enabled'.

    """
    return {
        "core": {
            "tools": {
                "Workfiles": {
                    "workfile_lock_profiles": profiles,
                }
            }
        }
    }


# Profile order matters for these fixtures. Filtering by a key that does not
#   exist on the profiles makes 'filter_profiles' score every profile as
#   neutral (0), so none is rejected and the first profile in the list is
#   returned for any host name. Both orders are covered so that the
#   regression is caught no matter which profile comes first.
DISABLED_FIRST = [
    {"host_names": ["nuke"], "enabled": False},
    {"host_names": ["maya"], "enabled": True},
]
ENABLED_FIRST = [
    {"host_names": ["nuke"], "enabled": True},
    {"host_names": ["maya"], "enabled": False},
]


class TestIsWorkfileLockEnabled:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("profiles", "host_name", "expected"),
        [
            # Each host resolves its own profile, not the first one.
            (DISABLED_FIRST, "maya", True),
            (DISABLED_FIRST, "nuke", False),
            (ENABLED_FIRST, "maya", False),
            (ENABLED_FIRST, "nuke", True),
        ],
    )
    def test_host_resolves_own_profile(
        self, profiles, host_name, expected
    ):
        """Profiles are filtered by 'host_names', not by list order."""
        assert is_workfile_lock_enabled(
            host_name, "test_project", _project_settings(profiles)
        ) is expected

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "profiles", [DISABLED_FIRST, ENABLED_FIRST]
    )
    def test_host_without_matching_profile(self, profiles):
        """Host without any matching profile has locking disabled."""
        assert is_workfile_lock_enabled(
            "houdini", "test_project", _project_settings(profiles)
        ) is False

    @pytest.mark.unit
    def test_no_profiles(self):
        """Empty profiles list disables locking for every host."""
        assert is_workfile_lock_enabled(
            "maya", "test_project", _project_settings([])
        ) is False

    @pytest.mark.unit
    def test_wildcard_profile_applies_to_any_host(self):
        """Profile with '*' host name matches any host."""
        profiles = [{"host_names": ["*"], "enabled": True}]
        assert is_workfile_lock_enabled(
            "houdini", "test_project", _project_settings(profiles)
        ) is True
