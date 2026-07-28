import os
import json
import socket
from ayon_core.lib import Logger, filter_profiles
from ayon_core.lib.ayon_info import get_workstation_info
from ayon_core.settings import get_project_settings


def _get_process_id():
    """Process id of the current session.

    Imported on demand. This module is imported while
    'ayon_core.pipeline' is still initializing, so 'get_process_id' cannot
    be imported at module level.

    Returns:
        str: Process id.

    """
    from ayon_core.pipeline import get_process_id

    return get_process_id()


def _read_lock_file(lock_filepath):
    if not os.path.exists(lock_filepath):
        log = Logger.get_logger("_read_lock_file")
        log.debug("lock file is not created or readable as expected!")
    with open(lock_filepath, "r") as stream:
        data = json.load(stream)
    return data


def _get_lock_file(filepath):
    return filepath + ".oplock"


def is_workfile_locked(filepath):
    lock_filepath = _get_lock_file(filepath)
    if not os.path.exists(lock_filepath):
        return False
    return True


def get_workfile_lock_data(filepath):
    lock_filepath = _get_lock_file(filepath)
    return _read_lock_file(lock_filepath)


def is_workfile_locked_for_current_process(filepath):
    if not is_workfile_locked(filepath):
        return False

    lock_filepath = _get_lock_file(filepath)
    data = _read_lock_file(lock_filepath)
    return data["process_id"] == _get_process_id()


def delete_workfile_lock(filepath):
    lock_filepath = _get_lock_file(filepath)
    if os.path.exists(lock_filepath):
        os.remove(lock_filepath)


def create_workfile_lock(filepath):
    lock_filepath = _get_lock_file(filepath)
    info = get_workstation_info()
    info["process_id"] = _get_process_id()
    # 'process_id' is a uuid, so it cannot tell whether the session that
    #   wrote the lock is still alive. The operating system pid can, but
    #   only on the machine that wrote it, so both are stored.
    info["system_pid"] = os.getpid()
    with open(lock_filepath, "w") as stream:
        json.dump(info, stream)


def _is_pid_running(pid):
    """Whether a process id is alive on this machine.

    Args:
        pid (int): Operating system process id.

    Returns:
        bool: Process is running. ``True`` when there is no way to tell,
            so an unknown state never clears somebody's lock.

    """
    try:
        import psutil
    except ImportError:
        return True

    try:
        return psutil.pid_exists(pid)
    except Exception:
        return True


def is_stale_lock_data(lock_data):
    """Whether lock data was left behind by a dead session.

    Only answerable for locks written on this workstation, because a pid
    from another machine means nothing here. Anything uncertain counts as
    not stale, so a live session never has its lock cleared underneath it.

    Args:
        lock_data (dict[str, Any]): Content of a lock file.

    Returns:
        bool: The session holding the lock is gone.

    """
    if not isinstance(lock_data, dict):
        return False

    if lock_data.get("hostname") != socket.gethostname():
        return False

    system_pid = lock_data.get("system_pid")
    # Locks written before 'system_pid' was stored, and anything that is
    #   not a plain pid, stay untouched.
    if not isinstance(system_pid, int):
        return False

    if system_pid == os.getpid():
        return False

    return not _is_pid_running(system_pid)


def remove_workfile_lock(filepath):
    if is_workfile_locked_for_current_process(filepath):
        delete_workfile_lock(filepath)


def is_workfile_lock_enabled(host_name, project_name, project_setting=None):
    if project_setting is None:
        project_setting = get_project_settings(project_name)
    workfile_lock_profiles = (
        project_setting
        ["core"]
        ["tools"]
        ["Workfiles"]
        ["workfile_lock_profiles"])
    profile = filter_profiles(
        workfile_lock_profiles, {"host_names": host_name}
    )
    if not profile:
        return False
    return profile["enabled"]
