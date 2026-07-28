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
call from its own exit signal, then sets
``workfile_lock_release_wired`` to say so. Without that attribute the
mixin stays inert, so a half-finished adoption cannot strand locks.

"""
from __future__ import annotations

import os
import typing
from typing import Any, Optional

from ayon_core.lib import Logger

from .lock_workfile import (
    create_workfile_lock,
    delete_workfile_lock,
    get_workfile_lock_data,
    is_stale_lock_data,
    is_workfile_lock_enabled,
    is_workfile_locked,
    remove_workfile_lock,
    # Wraps an import that cannot happen at module level, see its docstring.
    _get_process_id,
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

    # Lets type checkers see the host methods the mixin uses. At runtime
    #   the base is 'object', so the mixin adds nothing to the method
    #   resolution order of the host that adopts it.
    _MixinBase = IWorkfileHost
else:
    _MixinBase = object

log = Logger.get_logger("WorkfileLockMixin")

# Set by 'CheckWorkfileLock' when the artist chose to ignore a lock, read
#   once inside the session by 'handle_external_workfile_open'. A launch
#   hook has no other way to talk to the session it starts.
AYON_WORKFILE_LOCK_OVERRIDE = "AYON_WORKFILE_LOCK_OVERRIDE"

# Interface hooks implemented by the mixin. Used to warn about a base class
#   order that would shadow them.
_HOOK_METHOD_NAMES = (
    "_before_workfile_open",
    "_after_workfile_open",
    "_after_workfile_save",
)


def confirm_locked_workfile(
    filepath: str, lock_data: Optional[dict[str, Any]] = None
) -> bool:
    """Ask the artist whether to open a workfile locked by someone.

    Shared by :meth:`WorkfileLockMixin.confirm_locked_workfile` and by
    launch hooks, which run before a host instance exists and would
    otherwise have to reimplement the dialog.

    Args:
        filepath (str): Path to the locked workfile.
        lock_data (Optional[dict[str, Any]]): Already read content of the
            lock file. Read by the dialog when not passed.

    Returns:
        bool: Continue with opening the workfile.

    Raises:
        Exception: The dialog could not be shown. Callers decide what to
            do when the artist cannot be asked.

    """
    # Imported here to keep Qt out of the 'ayon_core.pipeline' import
    #   graph, which is imported in contexts without a GUI.
    from ayon_core.tools.workfiles.lock_dialog import WorkfileLockDialog

    dialog = WorkfileLockDialog(filepath, lock_data=lock_data)
    return bool(dialog.exec_())


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

    The mixin covers the parts that are the same for every host:

    * refusing to open a workfile locked by someone else, after asking
      the artist (``_before_workfile_open``),
    * acquiring the lock once the workfile is open
      (``_after_workfile_open``),
    * moving the lock to the new path on save-as / version-up
      (``_after_workfile_save``).

    The adopting host **must** call :meth:`release_workfile_lock` from
    whatever exit signal it has, and set ``workfile_lock_release_wired``
    to declare that it did. Core has no host teardown hook and cannot
    check this itself, so until the attribute is set no lock is acquired
    at all. A lock nobody releases would make workfiles look permanently
    taken.

    A host overriding any of the hooks above has to call ``super()`` or
    locking silently stops working.

    Notes:
        Locking is advisory. The dialog has an "Ignore lock" button, and
            an artist who chooses it takes the lock over.

        Every method is a no-op when locking is disabled by project
            settings or when no filepath is known, so callers do not need
            their own guards. No filesystem failure is ever raised to
            the caller either, because failing to lock must not stop an
            artist from opening or saving a workfile.

    """
    # Set to True by a host that calls 'release_workfile_lock' from its own
    #   exit signal. Until it does, no lock is ever acquired: a lock nobody
    #   releases is worse than no lock at all.
    workfile_lock_release_wired: bool = False

    # Is an artist sitting in front of this session? Left as None, the
    #   guess in '_is_interactive_session' decides. Hosts that run Qt
    #   headlessly have to answer this themselves.
    workfile_lock_interactive: Optional[bool] = None

    # Path of the workfile locked by this session. Declared on the class so
    #   the mixin needs no '__init__'. Hosts do not cooperatively call
    #   'super().__init__()'.
    _locked_workfile_path: Optional[str] = None

    # Set when the lock dialog could not be shown. The open continues, but
    #   the lock is left with its owner, since nobody answered.
    _lock_dialog_failed: bool = False

    def __init_subclass__(cls, **kwargs):
        """Warn about adoption mistakes that silently break locking."""
        super().__init_subclass__(**kwargs)
        try:
            _warn_on_shadowed_hooks(cls)
            _warn_on_missing_release(cls)
        except Exception:
            # A diagnostic must never break class creation.
            pass

    # --- Public API ---
    def is_workfile_locking_enabled(
        self,
        project_name: Optional[str] = None,
        project_settings: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Whether workfile locking is enabled for this host and project.

        Named differently from the module level
        ``is_workfile_lock_enabled(host_name, project_name, settings)`` on
        purpose. The same name with a different first argument invites
        passing a host name where a project name is expected.

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
                is_workfile_lock_enabled(
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
        counts as unlocked, because a broken one must not keep an artist
        out of their workfile.

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

        if not self.is_workfile_locking_enabled(
            project_name, project_settings
        ):
            return None

        try:
            # One existence check, then one read. Going through
            #   'is_workfile_locked_for_current_process' would read the
            #   lock file twice, and it usually sits on a network share.
            if not is_workfile_locked(filepath):
                return None
            lock_data = get_workfile_lock_data(filepath)
            # Indexed, not '.get()'. A sidecar without 'process_id' was not
            #   written by us and is malformed, which the 'except' below
            #   turns into "unlocked" rather than a lock nobody can clear.
            if lock_data["process_id"] == _get_process_id():
                return None
            if is_stale_lock_data(lock_data):
                # Our machine, and the session that wrote it is gone.
                #   Nothing else can clear it, since 'remove_workfile_lock'
                #   only removes locks whose uuid matches this process.
                log.info(
                    "Clearing the workfile lock of '%s' left behind by a"
                    " session that is no longer running.",
                    filepath,
                )
                delete_workfile_lock(filepath)
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

        Overwrites an existing lock. Whether to take over somebody
        else's lock is decided before this is called.

        Args:
            filepath (Optional[str]): Path to the workfile to lock.
            project_name (Optional[str]): Project name. Current project of
                the host is used when not passed.
            project_settings (Optional[dict[str, Any]]): Prepared project
                settings. Queried when not passed.

        """
        if not filepath:
            return

        if not self.workfile_lock_release_wired:
            # Nothing would ever remove this lock. See the class attribute.
            return

        if not self.is_workfile_locking_enabled(
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

    def handle_external_workfile_open(
        self,
        filepath: Optional[str],
        *,
        project_name: Optional[str] = None,
        project_settings: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Lock a workfile the host opened outside of core.

        ``open_workfile_with_context`` is not the only way a workfile
        ends up open. Hosts that get one as a launch argument open it
        natively, and Maya's File > Open opens one without going through
        core at all. Those paths never reach
        :meth:`_after_workfile_open`, so they call this instead.

        The workfile is already open by the time this runs, so it is too
        late to refuse. A lock taken by somebody else between the
        prelaunch check and now can only be reported, and it stays with
        its owner unless the artist takes it over.

        Args:
            filepath (Optional[str]): Path to the opened workfile.
            project_name (Optional[str]): Project name. Current project of
                the host is used when not passed.
            project_settings (Optional[dict[str, Any]]): Prepared project
                settings. Queried when not passed.

        Returns:
            bool: The lock is held by this session.

        """
        try:
            # Popped whether or not it is used, so an answer about one
            #   workfile can never apply to a later one in the same
            #   session.
            override_path = os.environ.pop(AYON_WORKFILE_LOCK_OVERRIDE, "")

            if not filepath:
                return False

            if not self.is_workfile_locking_enabled(
                project_name, project_settings
            ):
                return False

            answered = bool(override_path) and (
                os.path.normpath(override_path)
                == os.path.normpath(filepath)
            )
            if not answered:
                lock_data = self.get_workfile_lock_holder(
                    filepath,
                    project_name=project_name,
                    project_settings=project_settings,
                )
                if lock_data is not None and not self.confirm_locked_workfile(
                    filepath, lock_data
                ):
                    log.warning(
                        "Workfile '%s' stays locked by %s on %s.",
                        filepath,
                        lock_data.get("username"),
                        lock_data.get("hostname"),
                    )
                    return False

            self.acquire_workfile_lock(
                filepath,
                project_name=project_name,
                project_settings=project_settings,
            )
        except Exception:
            log.warning(
                "Failed to lock the opened workfile '%s'.",
                filepath,
                exc_info=True,
            )
            return False

        return self._locked_workfile_path == filepath

    def release_workfile_lock(
        self, filepath: Optional[str] = None
    ) -> None:
        """Release a workfile lock held by the current session.

        Safe to call more than once, and safe when no lock is held, so
        it can be wired to an exit signal that may fire twice. A lock
        owned by a different process is left alone.

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

    def confirm_locked_workfile(
        self, filepath: str, lock_data: Optional[dict[str, Any]] = None
    ) -> bool:
        """Ask the artist whether to open a workfile locked by someone.

        Can be overridden to change how the artist is asked, e.g. to
        always refuse as a studio policy.

        Args:
            filepath (str): Path to the locked workfile.
            lock_data (Optional[dict[str, Any]]): Already read content of
                the lock file.

        Returns:
            bool: Continue with opening the workfile.

        """
        try:
            return confirm_locked_workfile(filepath, lock_data)
        except Exception:
            # Fail open, so a broken dialog cannot keep an artist out of
            #   a workfile. The lock stays with its owner though, since
            #   nobody answered the question.
            self._lock_dialog_failed = True
            log.warning(
                "Failed to show the workfile lock dialog for '%s'."
                " Continuing with the workfile open, without taking the"
                " lock.",
                filepath,
                exc_info=True,
            )
            return True

    # --- IWorkfileHost hooks ---
    def _before_workfile_open(
        self, open_workfile_context: OpenWorkfileContext
    ) -> None:
        super()._before_workfile_open(open_workfile_context)

        self._lock_dialog_failed = False
        filepath = open_workfile_context.filepath
        lock_data = self.get_workfile_lock_holder(
            filepath,
            project_name=open_workfile_context.project_name,
            project_settings=open_workfile_context.project_settings,
        )
        if lock_data is None:
            return

        if not self._is_interactive_session():
            # A headless session cannot ask, and refusing would break
            #   farm jobs that open workfiles routinely. It opens without
            #   taking the lock, so the owner keeps it.
            log.warning(
                "Workfile '%s' is locked by %s on %s, but there is no"
                " GUI to ask about it. Continuing without a lock.",
                filepath,
                lock_data.get("username"),
                lock_data.get("hostname"),
            )
            return

        if not self.confirm_locked_workfile(filepath, lock_data):
            raise WorkfileLockedError(filepath, lock_data)

    def _after_workfile_open(
        self, open_workfile_context: OpenWorkfileContext
    ) -> None:
        super()._after_workfile_open(open_workfile_context)

        if not self._is_interactive_session():
            return

        if self._lock_dialog_failed:
            # Nobody was asked, so nobody agreed to take the lock over.
            self._lock_dialog_failed = False
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

        # Checked before releasing anything, so a session that is not going
        #   to re-acquire does not drop the lock it already holds.
        if not self._is_interactive_session():
            return

        dst_path = save_workfile_context.dst_path
        project_name = save_workfile_context.project_name
        project_settings = save_workfile_context.project_settings

        previous_path = self._locked_workfile_path
        if previous_path and previous_path != dst_path:
            # Save-as or version-up. Locks key on the exact path, so the
            #   lock has to follow the workfile.
            self.release_workfile_lock(previous_path)

        # Save-as can land on a path somebody else is working in, and no
        #   dialog was shown for it, unlike the open path, where taking
        #   a lock over is a decision the artist already made.
        lock_data = self.get_workfile_lock_holder(
            dst_path,
            project_name=project_name,
            project_settings=project_settings,
        )
        if lock_data is not None and not self.confirm_locked_workfile(
            dst_path, lock_data
        ):
            log.warning(
                "Workfile '%s' stays locked by %s on %s.",
                dst_path,
                lock_data.get("username"),
                lock_data.get("hostname"),
            )
            return

        self.acquire_workfile_lock(
            dst_path,
            project_name=project_name,
            project_settings=project_settings,
        )

    # --- Helpers ---
    def _is_interactive_session(self) -> bool:
        """Whether an artist is sitting in front of this session.

        ``workfile_lock_interactive`` answers this outright when a host
        sets it. Otherwise a running Qt application is taken as the
        signal, which is only a guess, because a farm job with an
        offscreen Qt application looks the same. Hosts that can end up in
        that state have to set the attribute, or override this.

        Returns:
            bool: An artist can be asked about a lock.

        """
        if self.workfile_lock_interactive is not None:
            return bool(self.workfile_lock_interactive)

        try:
            from qtpy import QtWidgets

            return QtWidgets.QApplication.instance() is not None
        except Exception:
            return False


def _warn_on_shadowed_hooks(cls: type) -> None:
    """Warn when a base class shadows the mixin's locking hooks.

    Placing the mixin after ``IWorkfileHost`` in the base classes makes
    the interface defaults win the method resolution order and locking
    silently does nothing. A host overriding a hook itself is fine,
    because the class docstring tells it to call ``super()``.

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


def _warn_on_missing_release(cls: type) -> None:
    """Warn when a host adopted the mixin without wiring release.

    Core cannot see the host's exit signal, so the host has to say that it
    wired one. Until it does, no lock is acquired at all. The
    alternative is locks that outlive every session and make workfiles
    look permanently taken.

    Args:
        cls (type): Class being created.

    """
    if cls.workfile_lock_release_wired:
        return

    # Intermediate base classes are not hosts yet, so they have nothing to
    #   wire. Only a class that is actually a host is missing something.
    if not any(
        method_name in vars(klass)
        for klass in cls.__mro__
        for method_name in ("install", "get_current_workfile")
    ):
        return

    log.warning(
        "Host class '%s' uses 'WorkfileLockMixin' but does not set"
        " 'workfile_lock_release_wired'. Workfile locking stays off."
        " Call 'release_workfile_lock()' from the exit signal of the"
        " host and set the attribute to True.",
        cls.__name__,
    )
