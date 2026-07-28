from __future__ import annotations

from ayon_applications import PreLaunchHook, LaunchTypes

from ayon_core.pipeline.workfile import (
    confirm_locked_workfile,
    delete_workfile_lock,
    get_workfile_lock_data,
    is_stale_lock_data,
    is_workfile_lock_enabled,
    is_workfile_locked,
    resolve_launch_workfile_path,
)
from ayon_core.pipeline.workfile.workfile_lock_mixin import (
    AYON_WORKFILE_LOCK_OVERRIDE,
)


class CheckWorkfileLock(PreLaunchHook):
    """Check the workfile lock before the application opens the workfile.

    Hosts in 'AddLastWorkfileToLaunchArgs' receive their workfile as a
    launch argument and open it natively. That skips
    'open_workfile_with_context', and with it the lock check that
    'WorkfileLockMixin' runs there, so this hook is that check for the
    launcher path.

    A locked workfile is dropped from the launch context rather than
    aborting the launch, so the application still starts, just without a
    workfile. The artist can then pick a different one in the Workfiles
    tool.

    Order 9 puts this before every hook that turns the workfile into a
    launch argument: 'AddLastWorkfileToLaunchArgs' at order 10, and host
    addon hooks that build their own launch args later. They all decide
    from 'workfile_path' and 'start_last_workfile', so clearing those two
    keys is enough to keep the workfile out of the launch, as long as this
    runs first. The data it reads is ready well before, since
    'GlobalHostDataHook' is order -100 and 'CopyTemplateWorkfile' order 0.

    """

    # Hosts whose host class adopts 'WorkfileLockMixin'. Widen as more
    #   do. A host without it never takes or releases a lock inside the
    #   session, so a check here would only get in the way.
    app_groups = {"aftereffects"}

    # Has to beat every hook that turns the workfile into a launch
    #   argument. See the class docstring.
    order = 9
    launch_types = {LaunchTypes.local}

    def execute(self):
        try:
            self._inner_execute()
        except Exception:
            # A lock must never be the reason a launch fails.
            self.log.warning(
                "Workfile lock check failed, launching anyway.",
                exc_info=True,
            )

    def _inner_execute(self) -> None:
        workfile_path = resolve_launch_workfile_path(self.data)
        if not workfile_path:
            return

        project_name = self.data.get("project_name")
        if not project_name:
            return

        if not is_workfile_lock_enabled(
            self.host_name,
            project_name,
            self.data.get("project_settings"),
        ):
            return

        if not is_workfile_locked(workfile_path):
            return

        try:
            lock_data = get_workfile_lock_data(workfile_path)
        except Exception:
            # A broken sidecar must not keep an artist out of a workfile.
            self.log.warning(
                "Could not read the workfile lock of '%s'."
                " Treating the workfile as unlocked.",
                workfile_path,
                exc_info=True,
            )
            return

        if is_stale_lock_data(lock_data):
            # Left behind by a session on this machine that is gone.
            #   Locks are keyed on a uuid, so nothing else clears it.
            self.log.info(
                "Clearing the workfile lock of '%s' left behind by a"
                " session that is no longer running.",
                workfile_path,
            )
            delete_workfile_lock(workfile_path)
            return

        if self._confirm_locked_workfile(workfile_path, lock_data):
            # The lock file is left alone, since its owner may still be
            #   running. The answer travels to the session instead, which
            #   overwrites the lock and does not ask a second time.
            self.launch_context.env[AYON_WORKFILE_LOCK_OVERRIDE] = (
                workfile_path
            )
            return

        self.log.info(
            "Workfile '%s' is locked by %s on %s."
            " Starting the application without it.",
            workfile_path,
            lock_data.get("username"),
            lock_data.get("hostname"),
        )
        self.data["workfile_path"] = None
        self.data["start_last_workfile"] = False

    def _confirm_locked_workfile(
        self, workfile_path: str, lock_data: dict
    ) -> bool:
        """Ask the artist whether to open the locked workfile.

        Shows the same dialog as the mixin, through the shared helper,
        so studio policy does not depend on which path a workfile was
        opened from. It cannot call the mixin *method*, because there is
        no host instance in the launcher process.

        Args:
            workfile_path (str): Path to the locked workfile.
            lock_data (dict): Already read content of the lock file.

        Returns:
            bool: Launch the application with the workfile.

        """
        from qtpy import QtWidgets

        if QtWidgets.QApplication.instance() is None:
            # No display, no artist to ask. Creating a QApplication here
            #   would be a Qt fatal on a headless machine. That aborts
            #   the process outright and 'execute' could not catch it.
            self.log.info(
                "No Qt application to ask about the workfile lock of"
                " '%s'. Starting the application without it.",
                workfile_path,
            )
            return False

        return confirm_locked_workfile(workfile_path, lock_data)
