"""Tests for 'WorkfileLockMixin'.

No Qt is involved. The mixin's dialog call and its GUI availability check
are stubbed on the fake host, which is what an adopting host is allowed to
do as well.

"""
import json
import os
import socket

import pytest

from ayon_core.pipeline.workfile import lock_workfile, workfile_lock_mixin
from ayon_core.pipeline.workfile.lock_workfile import (
    _get_lock_file,
    create_workfile_lock,
)
from ayon_core.pipeline.workfile.workfile_lock_mixin import (
    AYON_WORKFILE_LOCK_OVERRIDE,
    WorkfileLockedError,
    WorkfileLockMixin,
)

FOREIGN_LOCK = {
    "username": "other_artist",
    "hostname": "other_machine",
    "process_id": "not-this-process",
}


def _project_settings(enabled=True):
    return {
        "core": {
            "tools": {
                "Workfiles": {
                    "workfile_lock_profiles": [
                        {"host_names": ["testhost"], "enabled": enabled},
                    ]
                }
            }
        }
    }


class _OpenContext:
    """Stand-in for 'OpenWorkfileContext'."""

    def __init__(self, filepath, project_settings):
        self.project_name = "test_project"
        self.filepath = filepath
        self.project_settings = project_settings


class _SaveContext:
    """Stand-in for 'SaveWorkfileContext'."""

    def __init__(self, dst_path, project_settings):
        self.project_name = "test_project"
        self.dst_path = dst_path
        self.project_settings = project_settings


class _InterfaceStub:
    """Stands in for the 'IWorkfileHost' hooks the mixin calls super() on.

    Records the calls so the tests can check that the mixin passes them
    on with 'super()', which matters because
    'IWorkfileHost._after_workfile_save' does real work, not a 'pass'.
    """

    def __init__(self):
        self.super_calls = []

    def _before_workfile_open(self, ctx):
        self.super_calls.append("before_open")

    def _after_workfile_open(self, ctx):
        self.super_calls.append("after_open")

    def _after_workfile_save(self, ctx):
        self.super_calls.append("after_save")


class FakeHost(WorkfileLockMixin, _InterfaceStub):
    """Minimal host adopting the mixin."""

    name = "testhost"

    # Stands for the exit signal a real host wires. Without it the mixin
    #   never acquires anything.
    workfile_lock_release_wired = True

    def __init__(self, gui_available=True, confirm=True):
        super().__init__()
        self._gui_available = gui_available
        self._confirm = confirm
        self.confirm_calls = []
        self.opened = []

    def get_current_project_name(self):
        return "test_project"

    def open_workfile(self, filepath):
        self.opened.append(filepath)

    # Stubs replacing the Qt-dependent parts of the mixin.
    def _is_interactive_session(self):
        return self._gui_available

    def confirm_locked_workfile(self, filepath, lock_data=None):
        self.confirm_calls.append(filepath)
        return self._confirm


@pytest.fixture
def workdir(tmp_path_factory):
    """Directory unique to a single test.

    'tmp_path' is not used here. The 'pytest-ayon' plugin overrides it
    with a session scoped fixture, so every test would share one
    directory and lock files would leak between tests.
    """
    return tmp_path_factory.mktemp("workfile_lock")


@pytest.fixture
def workfile(workdir):
    path = workdir / "scene_v001.test"
    path.write_text("workfile")
    return str(path)


@pytest.fixture
def lock_warnings(monkeypatch):
    """Collect warnings logged by the mixin.

    AYON's 'Logger' does not propagate to the root logger, so 'caplog'
    never sees these records.
    """
    messages = []

    class _RecordingLog:
        def warning(self, message, *args, **kwargs):
            messages.append(message % args if args else message)

        def info(self, message, *args, **kwargs):
            pass

        def debug(self, message, *args, **kwargs):
            pass

    monkeypatch.setattr(workfile_lock_mixin, "log", _RecordingLog())
    return messages


def _lock_path(filepath):
    return _get_lock_file(filepath)


def _write_foreign_lock(filepath):
    with open(_lock_path(filepath), "w") as stream:
        json.dump(FOREIGN_LOCK, stream)


@pytest.mark.unit
class TestAcquireRelease:
    def test_acquire_writes_lock_file(self, workfile):
        host = FakeHost()
        host.acquire_workfile_lock(
            workfile, project_settings=_project_settings()
        )

        assert os.path.exists(_lock_path(workfile))
        assert host._locked_workfile_path == workfile

    def test_release_removes_lock_file(self, workfile):
        host = FakeHost()
        host.acquire_workfile_lock(
            workfile, project_settings=_project_settings()
        )
        host.release_workfile_lock()

        assert not os.path.exists(_lock_path(workfile))
        assert host._locked_workfile_path is None

    def test_release_is_idempotent(self, workfile):
        host = FakeHost()
        host.acquire_workfile_lock(
            workfile, project_settings=_project_settings()
        )

        host.release_workfile_lock()
        # Wiring release to an exit signal that fires twice must be safe.
        host.release_workfile_lock()
        host.release_workfile_lock()

        assert not os.path.exists(_lock_path(workfile))

    def test_release_without_lock_held(self):
        """Releasing when nothing was acquired does nothing."""
        FakeHost().release_workfile_lock()

    def test_release_leaves_foreign_lock_alone(self, workfile):
        host = FakeHost()
        _write_foreign_lock(workfile)

        host.release_workfile_lock(workfile)

        assert os.path.exists(_lock_path(workfile))

    def test_acquire_skipped_without_release_wiring(self, workfile):
        """A host that never releases must never acquire either."""
        class _UnwiredHost(FakeHost):
            workfile_lock_release_wired = False

        host = _UnwiredHost()

        host.acquire_workfile_lock(
            workfile, project_settings=_project_settings()
        )

        assert not os.path.exists(_lock_path(workfile))
        assert host._locked_workfile_path is None

    def test_acquire_without_filepath(self):
        host = FakeHost()
        host.acquire_workfile_lock(
            None, project_settings=_project_settings()
        )

        assert host._locked_workfile_path is None

    def test_acquire_failure_does_not_raise(self, workdir):
        """A lock that cannot be written must not break the caller."""
        host = FakeHost()
        missing_dir = workdir / "does_not_exist" / "scene.test"

        host.acquire_workfile_lock(
            str(missing_dir), project_settings=_project_settings()
        )

        assert host._locked_workfile_path is None


@pytest.mark.unit
class TestLockHolder:
    def test_foreign_lock_is_reported(self, workfile):
        host = FakeHost()
        _write_foreign_lock(workfile)

        holder = host.get_workfile_lock_holder(
            workfile, project_settings=_project_settings()
        )

        assert holder is not None
        assert holder["username"] == "other_artist"

    def test_own_lock_is_not_reported(self, workfile):
        host = FakeHost()
        create_workfile_lock(workfile)

        holder = host.get_workfile_lock_holder(
            workfile, project_settings=_project_settings()
        )

        assert holder is None

    def test_stale_local_lock_is_cleared(self, workfile, monkeypatch):
        """A lock from a dead session here is not treated as a holder.

        Nothing else could ever remove it, because 'remove_workfile_lock'
        only removes locks whose uuid matches the current process.
        """
        monkeypatch.setattr(lock_workfile, "_is_pid_running", lambda _: False)
        with open(_lock_path(workfile), "w") as stream:
            json.dump(
                {
                    "username": "me",
                    "hostname": socket.gethostname(),
                    "process_id": "a-dead-session",
                    "system_pid": 999999,
                },
                stream,
            )
        host = FakeHost()

        holder = host.get_workfile_lock_holder(
            workfile, project_settings=_project_settings()
        )

        assert holder is None
        assert not os.path.exists(_lock_path(workfile))

    def test_stale_check_ignores_other_machines(self, workfile, monkeypatch):
        """A pid is only meaningful on the machine that recorded it."""
        monkeypatch.setattr(lock_workfile, "_is_pid_running", lambda _: False)
        with open(_lock_path(workfile), "w") as stream:
            json.dump(dict(FOREIGN_LOCK, system_pid=999999), stream)
        host = FakeHost()

        holder = host.get_workfile_lock_holder(
            workfile, project_settings=_project_settings()
        )

        assert holder is not None
        assert os.path.exists(_lock_path(workfile))

    def test_unlocked_workfile(self, workfile):
        host = FakeHost()

        holder = host.get_workfile_lock_holder(
            workfile, project_settings=_project_settings()
        )

        assert holder is None

    @pytest.mark.parametrize(
        "lock_content",
        ["not json", json.dumps({"username": "no_process_id"})],
        ids=["invalid_json", "missing_process_id"],
    )
    def test_malformed_lock_treated_as_unlocked(
        self, workfile, lock_content
    ):
        """A broken lock file must not raise or block the artist."""
        host = FakeHost()
        with open(_lock_path(workfile), "w") as stream:
            stream.write(lock_content)

        holder = host.get_workfile_lock_holder(
            workfile, project_settings=_project_settings()
        )

        assert holder is None


@pytest.mark.unit
class TestExternalWorkfileOpen:
    """The launcher path, where the workfile is already open."""

    @pytest.fixture(autouse=True)
    def _clear_override(self, monkeypatch):
        monkeypatch.delenv(AYON_WORKFILE_LOCK_OVERRIDE, raising=False)

    def test_unlocked_acquires(self, workfile):
        host = FakeHost()

        held = host.handle_external_workfile_open(
            workfile, project_settings=_project_settings()
        )

        assert held is True
        assert host.confirm_calls == []
        assert os.path.exists(_lock_path(workfile))

    def test_foreign_lock_confirmed_takes_over(self, workfile):
        _write_foreign_lock(workfile)
        host = FakeHost(confirm=True)

        held = host.handle_external_workfile_open(
            workfile, project_settings=_project_settings()
        )

        assert held is True
        assert host.confirm_calls == [workfile]
        assert host._locked_workfile_path == workfile

    def test_foreign_lock_declined_leaves_it(self, workfile):
        _write_foreign_lock(workfile)
        host = FakeHost(confirm=False)

        held = host.handle_external_workfile_open(
            workfile, project_settings=_project_settings()
        )

        assert held is False
        with open(_lock_path(workfile)) as stream:
            assert json.load(stream) == FOREIGN_LOCK

    def test_override_for_this_path_skips_the_dialog(
        self, workfile, monkeypatch
    ):
        """The artist already answered in the prelaunch hook."""
        _write_foreign_lock(workfile)
        monkeypatch.setenv(AYON_WORKFILE_LOCK_OVERRIDE, workfile)
        host = FakeHost(confirm=False)

        held = host.handle_external_workfile_open(
            workfile, project_settings=_project_settings()
        )

        assert held is True
        assert host.confirm_calls == []

    def test_override_for_another_path_still_asks(
        self, workdir, workfile, monkeypatch
    ):
        _write_foreign_lock(workfile)
        monkeypatch.setenv(
            AYON_WORKFILE_LOCK_OVERRIDE, str(workdir / "other.test")
        )
        host = FakeHost(confirm=False)

        host.handle_external_workfile_open(
            workfile, project_settings=_project_settings()
        )

        assert host.confirm_calls == [workfile]

    @pytest.mark.parametrize(
        "enabled", [True, False], ids=["enabled", "disabled"]
    )
    def test_override_is_always_popped(self, workfile, monkeypatch, enabled):
        """An unused answer must not apply to a later workfile."""
        monkeypatch.setenv(AYON_WORKFILE_LOCK_OVERRIDE, workfile)
        host = FakeHost()

        host.handle_external_workfile_open(
            workfile, project_settings=_project_settings(enabled=enabled)
        )

        assert AYON_WORKFILE_LOCK_OVERRIDE not in os.environ

    def test_disabled_does_nothing(self, workfile):
        _write_foreign_lock(workfile)
        host = FakeHost()

        held = host.handle_external_workfile_open(
            workfile, project_settings=_project_settings(enabled=False)
        )

        assert held is False
        assert host.confirm_calls == []
        with open(_lock_path(workfile)) as stream:
            assert json.load(stream) == FOREIGN_LOCK

    def test_no_filepath(self):
        host = FakeHost()

        assert host.handle_external_workfile_open(None) is False


@pytest.mark.unit
class TestDisabledBySettings:
    def test_acquire_writes_nothing(self, workfile):
        host = FakeHost()
        settings = _project_settings(enabled=False)

        host.acquire_workfile_lock(workfile, project_settings=settings)

        assert not os.path.exists(_lock_path(workfile))
        assert host._locked_workfile_path is None

    def test_holder_is_not_reported(self, workfile):
        host = FakeHost()
        _write_foreign_lock(workfile)

        holder = host.get_workfile_lock_holder(
            workfile, project_settings=_project_settings(enabled=False)
        )

        assert holder is None

    def test_open_hook_is_inert(self, workfile):
        host = FakeHost()
        _write_foreign_lock(workfile)
        ctx = _OpenContext(workfile, _project_settings(enabled=False))

        host._before_workfile_open(ctx)
        host._after_workfile_open(ctx)

        assert host.confirm_calls == []
        # The foreign lock is untouched, no lock of our own was created.
        with open(_lock_path(workfile)) as stream:
            assert json.load(stream) == FOREIGN_LOCK


@pytest.mark.unit
class TestOpenHooks:
    def test_unlocked_open_acquires(self, workfile):
        host = FakeHost()
        ctx = _OpenContext(workfile, _project_settings())

        host._before_workfile_open(ctx)
        host._after_workfile_open(ctx)

        assert host.confirm_calls == []
        assert host._locked_workfile_path == workfile
        assert os.path.exists(_lock_path(workfile))

    def test_foreign_lock_refused_raises(self, workfile):
        host = FakeHost(confirm=False)
        _write_foreign_lock(workfile)
        ctx = _OpenContext(workfile, _project_settings())

        with pytest.raises(WorkfileLockedError) as exc_info:
            host._before_workfile_open(ctx)

        assert host.confirm_calls == [workfile]
        assert exc_info.value.filepath == workfile
        assert "other_artist" in str(exc_info.value)
        assert "other_machine" in str(exc_info.value)

    def test_foreign_lock_ignored_takes_it_over(self, workfile):
        host = FakeHost(confirm=True)
        _write_foreign_lock(workfile)
        ctx = _OpenContext(workfile, _project_settings())

        host._before_workfile_open(ctx)
        host._after_workfile_open(ctx)

        assert host.confirm_calls == [workfile]
        assert host._locked_workfile_path == workfile
        with open(_lock_path(workfile)) as stream:
            assert json.load(stream)["username"] != "other_artist"

    def test_hooks_call_super(self, workfile):
        host = FakeHost()
        ctx = _OpenContext(workfile, _project_settings())

        host._before_workfile_open(ctx)
        host._after_workfile_open(ctx)

        assert host.super_calls == ["before_open", "after_open"]


@pytest.mark.unit
class TestHeadless:
    def test_foreign_lock_does_not_block_open(self, workfile):
        """A farm job must not hang on a dialog or steal the lock."""
        host = FakeHost(gui_available=False)
        _write_foreign_lock(workfile)
        ctx = _OpenContext(workfile, _project_settings())

        host._before_workfile_open(ctx)
        host._after_workfile_open(ctx)

        assert host.confirm_calls == []
        assert host._locked_workfile_path is None
        # The interactive owner keeps their lock.
        with open(_lock_path(workfile)) as stream:
            assert json.load(stream) == FOREIGN_LOCK

    def test_unlocked_open_leaves_no_lock(self, workfile):
        host = FakeHost(gui_available=False)
        ctx = _OpenContext(workfile, _project_settings())

        host._before_workfile_open(ctx)
        host._after_workfile_open(ctx)

        assert not os.path.exists(_lock_path(workfile))
        assert host._locked_workfile_path is None

    def test_save_leaves_no_lock(self, workfile):
        host = FakeHost(gui_available=False)
        ctx = _SaveContext(workfile, _project_settings())

        host._after_workfile_save(ctx)

        assert not os.path.exists(_lock_path(workfile))


@pytest.mark.unit
class TestSaveHook:
    def test_save_as_moves_the_lock(self, workdir, workfile):
        host = FakeHost()
        settings = _project_settings()
        host.acquire_workfile_lock(workfile, project_settings=settings)

        new_path = str(workdir / "scene_v002.test")
        host._after_workfile_save(_SaveContext(new_path, settings))

        assert not os.path.exists(_lock_path(workfile))
        assert os.path.exists(_lock_path(new_path))
        assert host._locked_workfile_path == new_path

    def test_save_same_path_keeps_the_lock(self, workfile):
        host = FakeHost()
        settings = _project_settings()
        host.acquire_workfile_lock(workfile, project_settings=settings)

        host._after_workfile_save(_SaveContext(workfile, settings))

        assert os.path.exists(_lock_path(workfile))
        assert host._locked_workfile_path == workfile

    def test_save_without_previous_lock_acquires(self, workfile):
        host = FakeHost()

        host._after_workfile_save(
            _SaveContext(workfile, _project_settings())
        )

        assert os.path.exists(_lock_path(workfile))

    def test_save_hook_calls_super(self, workfile):
        """'IWorkfileHost._after_workfile_save' does real work."""
        host = FakeHost()

        host._after_workfile_save(
            _SaveContext(workfile, _project_settings())
        )

        assert host.super_calls == ["after_save"]

    def test_save_as_onto_foreign_lock_asks(self, workdir, workfile):
        """Version-up can land on a workfile somebody else is in."""
        host = FakeHost(confirm=True)
        settings = _project_settings()
        host.acquire_workfile_lock(workfile, project_settings=settings)

        new_path = str(workdir / "scene_v002.test")
        _write_foreign_lock(new_path)

        host._after_workfile_save(_SaveContext(new_path, settings))

        assert host.confirm_calls == [new_path]
        assert host._locked_workfile_path == new_path

    def test_save_as_onto_foreign_lock_refused(self, workdir, workfile):
        """Declining leaves the other session's lock untouched."""
        host = FakeHost(confirm=False)
        settings = _project_settings()
        host.acquire_workfile_lock(workfile, project_settings=settings)

        new_path = str(workdir / "scene_v002.test")
        _write_foreign_lock(new_path)

        host._after_workfile_save(_SaveContext(new_path, settings))

        assert host.confirm_calls == [new_path]
        with open(_lock_path(new_path)) as stream:
            assert json.load(stream) == FOREIGN_LOCK
        assert host._locked_workfile_path is None

    def test_headless_save_keeps_the_lock(self, workdir, workfile):
        """The old lock is not dropped by a session that cannot re-take it."""
        host = FakeHost()
        settings = _project_settings()
        host.acquire_workfile_lock(workfile, project_settings=settings)

        host._gui_available = False
        new_path = str(workdir / "scene_v002.test")
        host._after_workfile_save(_SaveContext(new_path, settings))

        assert os.path.exists(_lock_path(workfile))
        assert host._locked_workfile_path == workfile


@pytest.mark.unit
class TestBaseClassOrderWarning:
    def test_warns_when_hooks_are_shadowed(self, lock_warnings):
        """Mixin after the interface silently disables locking."""

        class BadHost(_InterfaceStub, WorkfileLockMixin):
            name = "testhost"

        shadowed = [
            message for message in lock_warnings
            if "disables workfile locking" in message
        ]
        assert len(shadowed) == 3
        assert "_before_workfile_open" in " ".join(shadowed)
        assert "_after_workfile_open" in " ".join(shadowed)
        assert "_after_workfile_save" in " ".join(shadowed)

    def test_no_warning_for_correct_order(self, lock_warnings):
        class GoodHost(WorkfileLockMixin, _InterfaceStub):
            name = "testhost"

        assert lock_warnings == []

    def test_no_warning_for_host_own_override(self, lock_warnings):
        """A host is allowed to override a hook itself."""

        class OverridingHost(WorkfileLockMixin, _InterfaceStub):
            name = "testhost"

            def _after_workfile_save(self, ctx):
                super()._after_workfile_save(ctx)

        assert lock_warnings == []
