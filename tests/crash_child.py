"""Run one rebuild in this process and die at a named step of ``rebuild``, as a kill would.

Usage: crash_child.py <data_dir> <step>. It exits with 137 at the step, and 0 if the step never ran.
"""

import os
import sys

data_dir, step = sys.argv[1], sys.argv[2]
os.environ.update(
    {
        "DATA_DIR": data_dir,
        "VECTORDB_URL": f"{data_dir}/vectordb",
        "SETTINGS_FILEPATH": "",
        "AUTO_INIT": "false",
        "PROJECTS": '["norssit"]',
        "AUTH_SECRET": "test-secret-" + "x" * 40,
        # Never load the local model: the stubs embed nothing.
        "EMBEDDING_BACKEND": "openai",
        "EMBEDDING_API_KEY": "not-used",
    }
)

from crash_stubs import install_stubs  # noqa: E402

from sparql_llm.indexing import rebuild  # noqa: E402

install_stubs(setattr, rebuild, "new")
setattr(rebuild, step, lambda *args, **kwargs: os._exit(137))
rebuild.rebuild_index_with_alias("norssit")
