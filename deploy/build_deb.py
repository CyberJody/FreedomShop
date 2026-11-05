# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build a Debian/Ubuntu package around a source release: python3 deploy/build_deb.py dist/freedomshop-VERSION.tar.gz

The package carries the verified release archive under /usr/share/freedomshop. Its postinst runs the same steps as
install.sh (deploy/install.py --no-apt; apt has already installed the dependencies) on a new server, and the same
steps as `freedomshop upgrade` when FreedomShop is already installed. Set FREEDOMSHOP_ALLOWED_HOSTS for the first
install (sudo FREEDOMSHOP_ALLOWED_HOSTS=shop.example.com apt install ./freedomshop_VERSION_all.deb); it defaults to
the server's host name.
"""
import argparse
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

if __package__:
    from .release import version_from_text
else:
    from release import version_from_text

DEPENDS = ("python3 (>= 3.12), python3-venv, postgresql, postgresql-client, nginx, curl, ca-certificates, tar, "
           "systemd")
CONTROL = """Package: freedomshop
Version: {deb_version}
Section: web
Priority: optional
Architecture: all
Depends: {depends}
Suggests: libzbar0, certbot, python3-certbot-nginx
Maintainer: FreedomShop <noreply@freedomshop.app>
Homepage: https://freedomshop.app
Description: browser-based point of sale and inventory for independent shops
 FreedomShop runs on a server; registers open it in a web browser. This
 package installs it the same way as install.sh: PostgreSQL, gunicorn under
 systemd and nginx, with releases under /opt/freedomshop and data under
 /var/lib/freedomshop.
"""
POSTINST = """#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
set -e
[ "$1" = configure ] || exit 0
VERSION='{version}'
ARCHIVE="/usr/share/freedomshop/freedomshop-$VERSION.tar.gz"
if [ -L /opt/freedomshop/current ]; then
    if [ -d "/opt/freedomshop/releases/$VERSION" ]; then
        echo "FreedomShop $VERSION is already installed."
        exit 0
    fi
    /usr/local/bin/freedomshop upgrade "$ARCHIVE"
else
    WORK=$(mktemp -d)
    trap 'rm -rf "$WORK"' EXIT
    tar -xzf "$ARCHIVE" -C "$WORK"
    python3 "$WORK/freedomshop-$VERSION/deploy/install.py" --no-apt \\
        --allowed-hosts "${{FREEDOMSHOP_ALLOWED_HOSTS:-$(hostname)}}"
fi
"""


def debian_version(version):
    return version.replace("-", "~")  # 0.1.0-dev sorts before 0.1.0


def read_version(archive):
    name = archive.name.removesuffix(".tar.gz")
    member = f"{name}/freedomshop/__version__.py"
    text = subprocess.check_output(["tar", "-xzOf", str(archive), member], text=True)
    version = version_from_text(text)
    if name != f"freedomshop-{version}":
        raise ValueError("Archive name and release version disagree")
    return version


def build(archive, output):
    archive = archive.resolve(strict=True)
    version = read_version(archive)
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"freedomshop_{debian_version(version)}_all.deb"
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder) / "package"
        share = root / "usr/share/freedomshop"
        share.mkdir(parents=True)
        shutil.copyfile(archive, share / archive.name)
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        (share / "SHA256SUMS").write_text(f"{digest}  {archive.name}\n")
        docs = root / "usr/share/doc/freedomshop"
        docs.mkdir(parents=True)
        subprocess.run(["tar", "-xzf", str(archive), "-C", str(docs), "--strip-components=1",
                        f"freedomshop-{version}/LICENSE"], check=True)
        (docs / "LICENSE").rename(docs / "copyright")
        control = root / "DEBIAN"
        control.mkdir(mode=0o755)
        (control / "control").write_text(CONTROL.format(deb_version=debian_version(version), depends=DEPENDS))
        postinst = control / "postinst"
        postinst.write_text(POSTINST.format(version=version))
        postinst.chmod(0o755)
        for path in [root, *root.rglob("*")]:
            if path != postinst:
                path.chmod(0o755 if path.is_dir() else 0o644)
        subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", str(root), str(target)], check=True,
                       stdout=subprocess.DEVNULL)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archive", type=Path, help="dist/freedomshop-VERSION.tar.gz from release.sh")
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    print(build(args.archive, args.output))


if __name__ == "__main__":
    main()
