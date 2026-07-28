from ayon_applications import PreLaunchHook, LaunchTypes

from ayon_core.pipeline.workfile import resolve_launch_workfile_path


class AddLastWorkfileToLaunchArgs(PreLaunchHook):
    """Add last workfile path to launch arguments.

    This is not possible to do for all applications the same way.
    Checks 'start_last_workfile', if set to False, it will not open last
    workfile. This property is set explicitly in Launcher.
    """

    # Execute after workfile template copy
    order = 10
    app_groups = {
        "3dsmax", "adsk_3dsmax",
        "maya",
        "nuke",
        "nukex",
        "hiero",
        "houdini",
        "nukestudio",
        "fusion",
        "blender",
        "photoshop",
        "tvpaint",
        "substancepainter",
        "substancedesigner",
        "aftereffects",
        "wrap",
        "openrv",
        "cinema4d",
        "silhouette",
        "gaffer",
        "loki",
        "marvelousdesigner",
    }
    launch_types = {LaunchTypes.local}

    def execute(self):
        # Shared with 'CheckWorkfileLock' (order 9), which decides from the
        #   same two keys whether this hook gets to see a workfile at all.
        workfile_path = resolve_launch_workfile_path(self.data)
        if not workfile_path:
            self.log.info("No workfile to open on launch.")
            return

        # Add path to workfile to arguments
        self.launch_context.launch_args.append(workfile_path)
