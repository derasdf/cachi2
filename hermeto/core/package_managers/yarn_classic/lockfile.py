import io
import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Pattern, Union

from hermeto.core.errors import BaseError

logger = logging.getLogger(__name__)

UNQUOTED_STRING_RE = re.compile(r"[a-zA-Z/.-][^\s\n,:]*")

V1_VERSION_COMMENT = "# yarn lockfile v1"


class Package:
    """Represents a single entry (package) in a yarn.lock file."""

    def __init__(
        self,
        name: str,
        version: str,
        url: str = "",
        checksum: Optional[str] = None,
        path: Optional[str] = None,
        relpath: Optional[str] = None,
        dependencies: Optional[Dict[str, str]] = None,
        alias: Optional[str] = None,
    ) -> None:
        """Initialize package metadata parsed from yarn.lock."""
        if not name:
            raise ValueError("Package name was not provided")

        if not version:
            raise ValueError("Package version was not provided")

        self.name = name
        self.version = version
        self.url = url
        self.checksum = checksum
        self.path = path if path is not None else relpath
        self.dependencies = dependencies or {}
        self.alias = alias

    @property
    def relpath(self) -> Optional[str]:
        """
        Return the path to the package.

        This is strictly kept for backwards compatibility and path should be used directly
        instead. The path is not always relative and may be absolute.
        """
        return self.path

    @relpath.setter
    def relpath(self, path: Optional[str]) -> None:
        """
        Set the path to the package.

        This is strictly kept for backwards compatibility and path should be used directly
        instead. The path is not always relative and may be absolute.
        """
        self.path = path

    @classmethod
    def from_dict(cls, raw_name: str, data: Dict[str, Any]) -> "Package":
        """Create a Package instance from a yarn.lock dictionary entry."""
        name_at_version = re.compile(r"(?P<name>@?[^@]+)(?:@(?P<version>[^,]*))?")

        name, _version = _must_match(name_at_version, raw_name).groups()
        alias = None
        path = None

        if _version and _version.startswith("npm:"):
            alias = name
            name, _version = _must_match(name_at_version, _remove_prefix(_version, "npm:")).groups()

        if _version:
            path = cls.get_path_from_version_specifier(_version)

        version = data.get("version")
        if not version:
            raise ValueError("Package version was not provided")

        return cls(
            name=name,
            version=version,
            url=data.get("resolved", ""),
            checksum=data.get("integrity"),
            path=path,
            dependencies=data.get("dependencies", {}),
            alias=alias,
        )

    @staticmethod
    def get_path_from_version_specifier(version: str) -> Optional[str]:
        """Return the path from a package.json file dependency version specifier."""
        version_path = Path(version)

        if version.startswith("file:"):
            return _remove_prefix(version, "file:")
        elif version.startswith("link:"):
            return _remove_prefix(version, "link:")
        elif version_path.is_absolute() or version.startswith(("./", "../")):
            return str(version_path)
        else:
            # Some non-path version specifier, (e.g. "1.0.0" or a web link)
            # See https://docs.npmjs.com/cli/v10/configuring-npm/package-json#dependencies
            return None


def _remove_prefix(s: str, prefix: str) -> str:
    return s[len(prefix) :]


def _must_match(pat: Pattern, s: str) -> re.Match:
    match = pat.match(s)
    if not match:
        raise ValueError(f"Unexpected format: {s!r} (does not match {pat.pattern})")
    return match


class Lockfile:
    """Represents a parsed yarn.lock file (version and data map)."""

    def __init__(self, version: str, data: Dict[str, Any]) -> None:
        """Initialize with lockfile version and the data dictionary."""
        self.version = version
        self.data = data

        if self.version == "unknown":
            logger.warning("Unknown Yarn version. Was this lockfile manually edited?")

        elif self.version != "1":
            raise ValueError(f"Unsupported yarn.lockfile version: {version}")

    def to_json(self) -> str:
        """Serialize the lock data to a canonical JSON string."""
        return json.dumps(self.data, sort_keys=True, indent=4)

    def packages(self) -> List[Package]:
        """Return a list of Package instances for every entry in the lockfile."""
        packages = []
        for name, pkg_data in self.data.items():
            pkg = Package.from_dict(name, pkg_data)
            packages.append(pkg)
        return packages

    @classmethod
    def from_file(cls, path: Union[str, Path]) -> "Lockfile":
        """Read a yarn.lock file from disk and parse it."""
        with open(path) as lockfile:
            lockfile_str = lockfile.read()
        return Lockfile.from_str(lockfile_str)

    @classmethod
    def parse_yarn_lock(cls, lockfile_str: str) -> Dict[str, Any]:
        """Use Node.js + @yarnpkg/lockfile to parse into a Python dict."""
        # A JS program that:
        #  1. loads the @yarnpkg/lockfile parser
        #  2. reads all text from stdin into `s`
        #  3. when stdin ends, parses it and writes JSON to stdout
        js = """
        const lock = require('@yarnpkg/lockfile');
        let s = '';
        process.stdin.setEncoding('utf8');

        process.stdin.on('data', chunk => {
        s += chunk;
        });

        process.stdin.on('end', () => {
        const parsed = lock.parse(s);
        process.stdout.write(JSON.stringify(parsed));
        });
        """
        node_path = shutil.which("node")
        if node_path is None:
            raise BaseError(
                "'node' executable not found in PATH",
                solution=(
                    "Please make sure that 'node' is installed and available in your PATH."
                    " If you are running inside a container, ensure Node.js is included in the image."
                ),
            )
        try:
            result = subprocess.run(
                [node_path, "-e", js],
                input=lockfile_str,
                text=True,
                capture_output=True,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            logger.error(
                f"Yarn parser failed (code {e.returncode})\nSTDOUT:\n{e.stdout}\nSTDERR:\n{e.stderr}"
            )  # :contentReference[oaicite:0]{index=0}
            raise ValueError(f"Can't parse yarn.lock: {e.stderr!r}") from e

        return json.loads(result.stdout)

    @classmethod
    def from_str(cls, lockfile_str: str) -> "Lockfile":
        """Parse lockfile content from a string, raising on invalid or empty."""
        try:
            parsed = cls.parse_yarn_lock(lockfile_str)
        except Exception:
            raise ValueError("Can't parse the yarn.lock file.")

        data = parsed.get("object", {})

        if not data:
            raise ValueError("The yarn.lock file must not be empty")

        return cls("1", data)

    def to_file(self, path: Union[str, Path]) -> None:
        """Write the lockfile back out to disk in yarn.lock format."""
        with open(path, "w") as lockfile:
            self._dump(lockfile)

    def to_str(self) -> str:
        """Return the lockfile formatted text as a string."""
        buffer = io.StringIO()
        self._dump(buffer)
        return buffer.getvalue()

    def _dump(self, outfile: io.TextIOBase) -> None:
        # Does not preserve any comments, but this one is required
        outfile.write(V1_VERSION_COMMENT)
        outfile.write("\n")
        for key, val in self.data.items():
            # Separate top-level keyvals by newline
            outfile.write("\n")
            _dump_keyval(key, val, outfile, 0)


def _dump_keyval(key: str, value: Any, outfile: io.TextIOBase, indent_level: int) -> None:
    outfile.write(" " * indent_level * 2)
    outfile.write(_quote_key_if_needed(key))

    if isinstance(value, dict):
        outfile.write(":\n")
        for k, v in value.items():
            _dump_keyval(k, v, outfile, indent_level + 1)
        # No newline here, _dump_keyval has already added one (recursion always ends
        # with a string, integer or boolean - the grammar does not allow empty dicts)
    else:
        outfile.write(" ")
        if isinstance(value, str):
            # Always quote string values
            # TODO: use json.dump to quote the value instead
            #   (the lexer would also have to interpret strings using json.load)
            outfile.write(f'"{value}"')
        else:
            json.dump(value, outfile)
        outfile.write("\n")


def _quote_key_if_needed(key: str) -> str:
    # The key may be a comma-separated list of keys
    keys = map(str.strip, key.split(","))
    # TODO: quote keys properly, see TODO about quoting values
    return ", ".join(f'"{k}"' if _needs_quoting(k) else k for k in keys)


def _needs_quoting(s: str) -> bool:
    if s.startswith("true") or s.startswith("false"):
        # If a string starts with a boolean, it must be quoted no matter what
        #   (otherwise, the string would be tokenized as BOOLEAN STRING)
        return True
    return UNQUOTED_STRING_RE.fullmatch(s) is None
