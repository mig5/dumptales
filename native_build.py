"""Compile the scanner as part of Poetry's wheel build."""
from pathlib import Path
import sysconfig

from setuptools import Distribution, Extension
from setuptools.command.build_ext import build_ext


def build(setup_kwargs=None) -> None:
    # Debian Bookworm ships poetry-core 1.4, which calls build(setup_kwargs)
    # from its generated setup.py. Modern Poetry executes this file directly.
    if setup_kwargs is not None:
        setup_kwargs["ext_modules"] = [Extension("_dumptales_native", ["native.c"])]
        return
    # Poetry includes matching extension files found in the source directory.
    # A prior build may have left a different Python ABI's .so here.
    suffix = sysconfig.get_config_var("EXT_SUFFIX")
    if not suffix:
        raise RuntimeError("cannot determine this Python's extension suffix")
    module = Path("_dumptales_native" + suffix)
    for stale in (*Path(".").glob("_dumptales_native*.so"),
                  *Path(".").glob("_dumptales_native*.pyd")):
        if stale != module:
            stale.unlink()
    distribution = Distribution({
        "name": "dumptales",
        "ext_modules": [Extension("_dumptales_native", ["native.c"])],
    })
    command = build_ext(distribution)
    command.ensure_finalized()
    command.inplace = True
    command.run()
    if not module.is_file():
        raise RuntimeError(f"expected compiled extension {module}")


if __name__ == "__main__":
    build()
