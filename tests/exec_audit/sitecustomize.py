# Loaded by Python at start-up when this folder is on PYTHONPATH (tests only).
# Records the program behind every subprocess diskmap starts, so tests can prove it never runs
# Apple's placeholder developer tools (/usr/bin/git, /usr/bin/python3, …) on a Mac without them.
import os
import shutil
import subprocess

LOG = os.environ.get("DISKMAP_EXEC_LOG")
if LOG:
    _orig = subprocess.Popen.__init__

    def _logged_init(self, args, *a, **k):
        try:
            prog = args if isinstance(args, str) else args[0]
            prog = str(prog)
            if not os.path.isabs(prog) and not (isinstance(args, str) or k.get("shell")):
                env = k.get("env") or os.environ
                prog = shutil.which(prog, path=env.get("PATH")) or prog
            with open(LOG, "a") as f:
                f.write(("SHELL " if isinstance(args, str) or k.get("shell") else "") + prog + "\n")
        except Exception:
            pass
        return _orig(self, args, *a, **k)

    subprocess.Popen.__init__ = _logged_init
