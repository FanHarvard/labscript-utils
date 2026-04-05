from pathlib import Path
try:
    import importlib.metadata as importlib_metadata
except ImportError:
    import importlib_metadata

try:
    __version__ = importlib_metadata.version(__package__)
except importlib_metadata.PackageNotFoundError:
    root = Path(__file__).parent.parent
    if (root / '.git').is_dir():
        try:
            from setuptools_scm import get_version

            VERSION_SCHEME = {
                "version_scheme": "release-branch-semver",
                "local_scheme": "node-and-date",
            }
            __version__ = get_version(root, **VERSION_SCHEME)
        except ImportError:
            __version__ = None
    else:
        __version__ = None
