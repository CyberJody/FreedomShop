# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build a source release from committed HEAD, with a deterministic gzip header."""
import argparse
import ast
import gzip
import hashlib
import re
import subprocess
from pathlib import Path


def version_from_text(text):
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            version = ast.literal_eval(node.value)
            if isinstance(version, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.+-]*", version):
                return version
    raise ValueError("Missing or unsafe __version__")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", default="dist")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    source = subprocess.check_output(["git", "show", "HEAD:freedomshop/__version__.py"], cwd=root, text=True)
    version = version_from_text(source)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    name = f"freedomshop-{version}"
    target = output / f"{name}.tar.gz"
    with target.open("wb") as stream, gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0) as zipped:
        process = subprocess.Popen(["git", "archive", "--format=tar", f"--prefix={name}/", "HEAD"],
                                   cwd=root, stdout=subprocess.PIPE)
        try:
            while chunk := process.stdout.read(1024 * 1024):
                zipped.write(chunk)
        finally:
            process.stdout.close()
        if process.wait():
            raise RuntimeError("git archive failed")
    with target.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    (output / "SHA256SUMS").write_text(f"{digest}  {target.name}\n")
    print(target)


if __name__ == "__main__":
    main()
