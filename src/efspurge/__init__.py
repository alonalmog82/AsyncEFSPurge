"""AsyncEFSPurge - High-performance async file purger for AWS EFS."""

try:
    from importlib.metadata import PackageNotFoundError, version
except ImportError:
    # Python < 3.8
    from importlib_metadata import PackageNotFoundError, version

try:
    __version__ = version("efspurge")
except PackageNotFoundError:
    # Package not installed, fallback to reading from pyproject.toml
    try:
        import tomllib
        from pathlib import Path

        pyproject_path = Path(__file__).parent.parent.parent / "pyproject.toml"
        if pyproject_path.exists():
            with open(pyproject_path, "rb") as f:
                pyproject = tomllib.load(f)
                __version__ = pyproject["project"]["version"]
        else:
            __version__ = "unknown"
    except Exception:
        __version__ = "unknown"
