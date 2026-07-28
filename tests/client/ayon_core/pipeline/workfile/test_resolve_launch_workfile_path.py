"""Tests for 'resolve_launch_workfile_path'.

The two prelaunch hooks that act on the launch workfile, the lock check at
order 9 and 'AddLastWorkfileToLaunchArgs' at order 10, have to agree on
which workfile is meant, so both read it through this one function.

"""
import pytest

from ayon_core.pipeline.workfile import resolve_launch_workfile_path


@pytest.fixture
def workfile(tmp_path_factory):
    """Existing workfile.

    'tmp_path' is not used here. The 'pytest-ayon' plugin overrides it with
    a session scoped fixture, so every test would share one directory.
    """
    path = tmp_path_factory.mktemp("launch_workfile") / "scene_v001.test"
    path.write_text("workfile")
    return str(path)


@pytest.mark.unit
class TestResolveLaunchWorkfilePath:
    def test_explicit_workfile_path(self, workfile):
        assert resolve_launch_workfile_path(
            {"workfile_path": workfile}
        ) == workfile

    def test_explicit_path_wins_over_last_workfile(self, workfile):
        data = {
            "workfile_path": workfile,
            "start_last_workfile": True,
            "last_workfile_path": "/nowhere/other_v001.test",
        }

        assert resolve_launch_workfile_path(data) == workfile

    def test_last_workfile(self, workfile):
        data = {
            "start_last_workfile": True,
            "last_workfile_path": workfile,
        }

        assert resolve_launch_workfile_path(data) == workfile

    def test_last_workfile_disabled(self, workfile):
        data = {
            "start_last_workfile": False,
            "last_workfile_path": workfile,
        }

        assert resolve_launch_workfile_path(data) is None

    @pytest.mark.parametrize(
        "data",
        [
            {},
            {"start_last_workfile": True},
            {"start_last_workfile": True, "last_workfile_path": ""},
            {"workfile_path": ""},
        ],
        ids=["empty", "no_last_path", "blank_last_path", "blank_path"],
    )
    def test_nothing_to_open(self, data):
        assert resolve_launch_workfile_path(data) is None

    def test_missing_last_workfile(self):
        data = {
            "start_last_workfile": True,
            "last_workfile_path": "/nowhere/scene_v001.test",
        }

        assert resolve_launch_workfile_path(data) is None

    def test_missing_explicit_workfile(self):
        """The existence check applies to an explicit path too.

        Handing a DCC a launch argument that points nowhere is not useful,
        and both hooks have to agree that there is no workfile.
        """
        data = {"workfile_path": "/nowhere/scene_v001.test"}

        assert resolve_launch_workfile_path(data) is None
