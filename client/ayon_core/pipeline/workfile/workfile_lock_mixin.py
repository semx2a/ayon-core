"""Opt-in workfile locking for host integrations.

Workfile locking is deliberately not part of ``IWorkfileHost``. The core
API can acquire, inspect and migrate a lock, but it has no host teardown
hook, so it cannot release one. Releasing depends on a signal only the
host integration knows about, e.g. Maya's ``kMayaExiting`` or a launcher
noticing that the application process is gone.

If locking were automatic for every host, hosts without release wiring
would start leaving stale ``.oplock`` files behind. So it is opt-in: a
host adds :class:`WorkfileLockMixin` to its bases and completes the
feature with a single :meth:`WorkfileLockMixin.release_workfile_lock`
call from its own exit signal.

"""
from __future__ import annotations

import typing
from typing import Any, Optional

from ayon_core.lib import Logger

from .lock_workfile import (
    create_workfile_lock,
    get_workfile_lock_data,
    is_workfile_locked,
    remove_workfile_lock,
    # Wraps an import that cannot happen at module level, see its docstring.
    _get_process_id,
    # Aliased because the mixin exposes a method with the same name.
    is_workfile_lock_enabled as _host_lock_enabled,
)

if typing.TYPE_CHECKING:
    # Type-only imports. Importing 'ayon_core.host' at runtime would create
    #   an import cycle, and this module has to stay importable from
    #   'ayon_core.pipeline'.
    from ayon_core.host.interfaces import (
        IWorkfileHost,
        OpenWorkfileContext,
        SaveWorkfileContext,
    )

    # Gives type checkers the host methods the mixin relies on ('name',
    #   'get_current_project_name') and the hooks it calls 'super()' on.
    #   At runtime the base is 'object', so the mixin adds nothing to the
    #   method resolution order of the host that adopts it.
    _MixinBase = IWorkfileHost
else:
    _MixinBase = object

log = Logger.get_logger("WorkfileLockMixin")

# Interface hooks implemented by the mixin. Used to warn about a base class
#   order that would shadow them.
_HOOK_METHOD_NAMES = (
    "_before_workfile_open",
    "_after_workfile_open",
    "_after_workfile_save",
)


class WorkfileLockedError(RuntimeError):
    """Opening a workfile locked by another session was refused.

    Attributes:
        filepath (str): Path to the locked workfile.
        lock_data (dict[str, Any]): Information about the lock holder.

    """

    def __init__(
        self, filepath: str, lock_data: Optional[dict[str, Any]] = None
    ):
        self.filepath = filepath
        self.lock_data = lock_data or {}
        username = self.lock_data.get("username") or "Another user"
        hostname = self.lock_data.get("hostname")
        holder = username
        if hostname:
            holder = f"{username} on {hostname}"
        super().__init__(
            f"{holder} is working on the workfile '{filepath}'."
        )


class WorkfileLockMixin(_MixinBase):
    """Opt-in workfile locking for hosts implementing ``IWorkfileHost``.

    Add the mixin **before** ``IWorkfileHost`` in the host bases, so that
    the hooks implemented here win the method resolution order::

        class MyHost(HostBase, WorkfileLockMixin, IWorkfileHost):
            ...

    The mixin covers what is generic:

    * refusing to open a workfile locked by someone else, after asking
      the artist (``_before_workfile_open``),
    * acquiring the lock once the workfile is open
      (``_after_workfile_open``),
    * moving the lock to the new path on save-as / version-up
      (``_after_workfile_save``).

    The adopting host **must** call :meth:`release_workfile_lock` from
    whatever exit signal it has. Core has no host teardown hook, so a host
    that adopts the mixin without wiring release will leave stale
    ``.oplock`` files behind.

    A host overriding any of the hooks above has to call ``super()`` or
    locking silently stops working.

    Notes:
        Locking is advisory. The dialog offers an "Ignore lock" button and
            an artist who ignores a lock takes it over, because they do
            have the workfile open at that point.

        Every method is a no-op when locking is disabled by project
            settings or when no filepath is known, so callers do not need
            their own guards. No filesystem failure is ever raised to the
            caller either - failing to lock must not stop an artist from
            opening or saving a workfile.

    """
    # Path of the workfile locked by this session. Declared on the class so
    #   the mixin needs no '__init__' - hosts do not cooperatively call
    #   'super().__init__()'.
    _locked_workfile_path: Optional[str] = None

    def __init_subclass__(cls, **kwargs):
        """Warn when base class order shadows the locking hooks."""
        super().__init_subclass__(**kwargs)
        try:
            _warn_on_shadowed_hooks(cls)
        except Exception:
            # A diagnostic must never break class creation.
            pass

    # --- Public API ---
    def is_workfile_lock_enabled(
        self,
        project_name: Optional[str] = None,
        project_settings: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Whether workfile locking is enabled for this host and project.

        Args:
            project_name (Optional[str]): Project name. Current project of
                the host is used when not passed.
            project_settings (Optional[dict[str, Any]]): Prepared project
                settings. Queried when not passed.

        Returns:
            bool: Locking is enabled.

        """
        try:
            if project_name is None:
                project_name = self.get_current_project_name()
            if not project_name:
                return False
            return bool(
                _host_lock_enabled(
                    self.name, project_name, project_settings
                )
            )
        except Exception:
            log.warning(
                "Failed to resolve workfile lock settings."
                " Locking is treated as disabled.",
                exc_info=True,
            )
            return False

    def get_workfile_lock_holder(
        self,
        filepath: Optional[str],
        *,
        project_name: Optional[str] = None,
        project_settings: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """Information about a foreign session holding the workfile lock.

        A lock owned by the current process is not a foreign lock, so
        ``None`` is returned for it. An unreadable or corrupted lock file
        counts as unlocked - a broken sidecar must not block an artist.

        Args:
            filepath (Optional[str]): Path to the workfile.
            project_name (Optional[str]): Project name. Current project of
                the host is used when not passed.
            project_settings (Optional[dict[str, Any]]): Prepared project
                settings. Queried when not passed.

        Returns:
            Optional[dict[str, Any]]: Lock holder information, or ``None``
                when the workfile is not locked by another session.

        """
        if not filepath:
            return None

        if not self.is_workfile_lock_enabled(
            project_name, project_settings
        ):
            return None

        try:
            # Cheap early out for the common case, then a single read.
            #   Chaining 'is_workfile_locked_for_current_process' and
            #   'get_workfile_lock_data' would parse the sidecar twice,
            #   and these live next to the workfile on a network share.
            if not is_workfile_locked(filepath):
                return None
            lock_data = get_workfile_lock_data(filepath)
            # Indexed, not '.get()'. A sidecar without 'process_id' was not
            #   written by us and is malformed, which the 'except' below
            #   turns into "unlocked" rather than a lock nobody can clear.
            if lock_data["process_id"] == _get_process_id():
                return None
            return lock_data
        except Exception:
            log.warning(
                "Failed to read workfile lock of '%s'."
                " Treating the workfile as unlocked.",
                filepath,
                exc_info=True,
            )
            return None

    def acquire_workfile_lock(
        self,
        filepath: Optional[str],
        *,
        project_name: Optional[str] = None,
        project_settings: Optional[dict[str, Any]] = None,
    ) -> None:
        """Lock the workfile for the current session.

        Overwrites an existing lock. Opening a workfile that someone else
        has locked is a decision made before this is called.

        Args:
            filepath (Optional[str]): Path to the workfile to lock.
            project_name (Optional[str]): Project name. Current project of
                the host is used when not passed.
            project_settings (Optional[dict[str, Any]]): Prepared project
                settings. Queried when not passed.

        """
        if not filepath:
            return

        if not self.is_workfile_lock_enabled(
            project_name, project_settings
        ):
            return

        try:
            create_workfile_lock(filepath)
        except Exception:
            log.warning(
                "Failed to create workfile lock for '%s'.",
                filepath,
                exc_info=True,
            )
            return

        self._locked_workfile_path = filepath

    def release_workfile_lock(
        self, filepath: Optional[str] = None
    ) -> None:
        """Release a workfile lock held by the current session.

        Idempotent and safe to call when no lock is held, so it can be
        wired to an exit signal that may fire more than once. A lock owned
        by a different process is left alone.

        Args:
            filepath (Optional[str]): Path to the workfile to unlock. The
                workfile locked by this session is used when not passed.

        """
        if filepath is None:
            filepath = self._locked_workfile_path

        if not filepath:
            return

        try:
            remove_workfile_lock(filepath)
        except Exception:
            log.warning(
                "Failed to remove workfile lock of '%s'.",
                filepath,
                exc_info=True,
            )
        finally:
            if filepath == self._locked_workfile_path:
                self._locked_workfile_path = None

    def confirm_locked_workfile(self, filepath: str) -> bool:
        """Ask the artist whether to open a workfile locked by someone.

        Can be overridden to change how the artist is asked, e.g. to
        always refuse as a studio policy.

        Args:
            filepath (str): Path to the locked workfile.

        Returns:
            bool: Continue with opening the workfile.

        """
        # Imported here to keep Qt out of the 'ayon_core.pipeline' import
        #   graph - 'pipeline' is imported in contexts without a GUI.
        try:
            from ayon_core.tools.workfiles.lock_dialog import (
                WorkfileLockDialog,
            )

            dialog = WorkfileLockDialog(filepath)
            return bool(dialog.exec_())
        except Exception:
            log.warning(
                "Failed to show the workfile lock dialog for '%s'."
                " Continuing with the workfile open.",
                filepath,
                exc_info=True,
            )
            return True

    # --- IWorkfileHost hooks ---
    def _before_workfile_open(
        self, open_workfile_context: OpenWorkfileContext
    ) -> None:
        super()._before_workfile_open(open_workfile_context)

        filepath = open_workfile_context.filepath
        lock_data = self.get_workfile_lock_holder(
            filepath,
            project_name=open_workfile_context.project_name,
            project_settings=open_workfile_context.project_settings,
        )
        if lock_data is None:
            return

        if not self._is_interactive_session():
            # Deliberate: a headless session cannot ask the artist.
            #   Refusing here would break farm jobs and automated
            #   publishes, which open workfiles routinely. The open
            #   continues, and '_after_workfile_open' does not acquire a
            #   lock, so the interactive owner keeps theirs.
            log.warning(
                "Workfile '%s' is locked by %s on %s, but there is no"
                " GUI to ask about it. Continuing without a lock.",
                filepath,
                lock_data.get("username"),
                lock_data.get("hostname"),
            )
            return

        if not self.confirm_locked_workfile(filepath):
            raise WorkfileLockedError(filepath, lock_data)

    def _after_workfile_open(
        self, open_workfile_context: OpenWorkfileContext
    ) -> None:
        super()._after_workfile_open(open_workfile_context)

        if not self._is_interactive_session():
            return

        self.acquire_workfile_lock(
            open_workfile_context.filepath,
            project_name=open_workfile_context.project_name,
            project_settings=open_workfile_context.project_settings,
        )

    def _after_workfile_save(
        self, save_workfile_context: SaveWorkfileContext
    ) -> None:
        super()._after_workfile_save(save_workfile_context)

        dst_path = save_workfile_context.dst_path
        previous_path = self._locked_workfile_path
        if previous_path and previous_path != dst_path:
            # Save-as or version-up. Locks key on the exact path, so the
            #   lock has to follow the workfile.
            self.release_workfile_lock(previous_path)

        if not self._is_interactive_session():
            return

        self.acquire_workfile_lock(
            dst_path,
            project_name=save_workfile_context.project_name,
            project_settings=save_workfile_context.project_settings,
        )

    # --- Helpers ---
    def _is_interactive_session(self) -> bool:
        """Whether an artist is sitting in front of this session.

        A running Qt application is used as the signal. It answers both
        questions locking depends on: whether the lock dialog can be shown
        at all, and whether this is the kind of session that has an exit
        signal wired to release the lock. A headless session has neither,
        so locking is skipped entirely rather than leaving a stale lock
        nothing will ever clean up.

        Can be overridden by a host that knows better.

        Returns:
            bool: A Qt application instance exists.

        """
        try:
            from qtpy import QtWidgets

            return QtWidgets.QApplication.instance() is not None
        except Exception:
            return False


def _warn_on_shadowed_hooks(cls: type) -> None:
    """Warn when a base class shadows the mixin's locking hooks.

    Placing the mixin after ``IWorkfileHost`` in the base classes makes
    the interface defaults win the method resolution order and locking
    silently does nothing. An override on the host itself is fine - the
    docstring asks those to call ``super()``.

    Args:
        cls (type): Class being created.

    """
    for method_name in _HOOK_METHOD_NAMES:
        # The mixin defines every hook in '_HOOK_METHOD_NAMES', so there is
        #   always a winner. A winner that is the mixin, or a host class
        #   deriving from it, is what we want.
        winner = next(
            klass for klass in cls.__mro__ if method_name in vars(klass)
        )
        if issubclass(winner, WorkfileLockMixin):
            continue

        log.warning(
            "Class '%s' defines '%s' before '%s' in the method"
            " resolution order, which disables workfile locking."
            " Add 'WorkfileLockMixin' before '%s' in the bases of '%s'.",
            winner.__name__,
            method_name,
            WorkfileLockMixin.__name__,
            winner.__name__,
            cls.__name__,
        )
