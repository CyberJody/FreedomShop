#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scan the source tree for data that must not be published; exit 1 on any hit.

Usage: python3 tools/public_scan.py [--root DIR] [--terms FILE]

Built-in checks: private keys, token-shaped secrets, SECRET_KEY literals, IP and MAC addresses outside the
documentation ranges, email addresses outside placeholder domains, personal home/mount paths, environment files,
certificates, databases, dumps, uploads/media/backup directories and unreviewed images or documents.

Names that identify a particular shop, its hosts or its people must not be written into this file, or the scan
itself would publish them. Keep them in a private file outside the tree, one case-insensitive regular expression
per line (# starts a comment), and pass it with --terms or FREEDOMSHOP_SCAN_TERMS.
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

SKIP_DIRS = {'.git', '.venv', 'venv', '__pycache__', 'node_modules', 'dist', '.mypy_cache', '.pytest_cache'}

# Path rules: (regular expression on the repo-relative path, reason).
FORBIDDEN_PATHS = [
    (r'(^|/)\.env(\.(?!example$)[^/]+)?$', 'environment file'),
    (r'\.(pem|key|crt|p12|pfx|jks|keystore)$', 'certificate or key file'),
    (r'(^|/)(id_rsa|id_ed25519|id_ecdsa)[^/]*$|(^|/)authorized_keys$', 'SSH key material'),
    (r'\.(sqlite3?|db|dump|bak|qpb|qbw|qbb|eml|msg|mbox)$', 'database, backup or mail file'),
    (r'(^|/)(uploads|media|backups?|recovery|exports|snapshots)/', 'uploaded or backed-up data'),
    (r'(^|/)secret[-_]key[^/]*$|(^|/)[^/]*credentials[^/]*$', 'secret or credential file'),
    (r'(^|/)docs/HISTORY-upstream\.md$', 'private upstream history'),
    (r'(quickbooks|intuit|qbpos)[^/]*\.(png|jpe?g|gif|svg|ico|webp|bmp|pdf)$', 'QuickBooks artwork'),
]
# Binary artwork and documents are allowed only after review. Add new ones here deliberately.
BINARY_SUFFIXES = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.webp', '.bmp', '.tif', '.tiff', '.pdf',
                   '.doc', '.docx', '.xls', '.xlsx', '.ods', '.odt', '.zip', '.gz', '.tar', '.ttf', '.otf', '.woff',
                   '.woff2', '.mp4', '.mov', '.webm'}
REVIEWED_BINARIES = {
    'shop_settings/fixtures/demo-logo.png',  # text "Demo Home & Hardware" on a plain background
    'sales/fixtures/qbpos/items.xlsx',  # generated import fixture (same rows as items.csv)
    'sales/print_assets/fonts/DejaVuSans.ttf',
    'sales/print_assets/fonts/DejaVuSansCondensed.ttf',
    'sales/print_assets/fonts/DejaVuSansCondensed-Bold.ttf',
}
# Third-party licence texts quote their authors' addresses verbatim.
THIRD_PARTY_TEXT = {'sales/print_assets/fonts/LICENSE-DejaVu.txt', 'THIRD-PARTY-NOTICES.md', 'LICENSE'}

PLACEHOLDER_EMAIL = re.compile(
    r'@(([a-z0-9-]+\.)*(example|invalid|test|localhost)(\.[a-z]+)?|freedomshop\.app|users\.noreply\.github\.com)$',
    re.I)
SAFE_SECRET = re.compile(r'test|not-secret|insecure|unset|example|placeholder|dummy|change-?me', re.I)

CONTENT_RULES = [
    ('private key', re.compile(r'-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----')),
    ('token-shaped secret', re.compile(
        r'\b(ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}|\bAKIA[0-9A-Z]{16}\b'
        r'|\bxox[abprs]-[A-Za-z0-9-]{10,}|\bsk-[A-Za-z0-9_-]{20,}|\bglpat-[A-Za-z0-9_-]{20,}')),
    ('personal path', re.compile(r'/home/(?!user\b|freedomshop\b|runner\b)[a-z][a-z0-9_-]*/|/mnt/(Data|Rust)\w*'
                                 r'|/Users/(?!Shared\b)[A-Za-z][\w.-]*/')),
    ('Tailscale name or address', re.compile(r'\b[a-z0-9-]+\.ts\.net\b|\bfd7a:115c:a1e0:', re.I)),
    ('MAC address', re.compile(r'\b(?!00:00:00:00:00:00\b|00:00:5E:00:53:)(?:[0-9A-F]{2}:){5}[0-9A-F]{2}\b', re.I)),
]
SECRET_ASSIGNMENT = re.compile(r'''\bSECRET_KEY\s*=\s*(?:[rbuf]?)(['"])(.*?)\1''')
IPV4 = re.compile(r'(?<![\w.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?!\w|\.\d)')
EMAIL = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')


def ip_allowed(octets):
    a, b, c, d = octets
    if any(n > 255 for n in octets):
        return True  # not an address (a version string or a dotted number)
    return (a == 127 or (a, b, c, d) == (0, 0, 0, 0) or a == 255  # loopback, any, netmasks
            or (a, b, c) in {(192, 0, 2), (198, 51, 100), (203, 0, 113)})  # RFC 5737 documentation ranges


def list_files(root):
    try:
        out = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                             check=True, capture_output=True).stdout
        names = sorted({n for n in out.decode().split('\0') if n})
        return [n for n in names if (root / n).is_file()]
    except (OSError, subprocess.CalledProcessError):
        names = []
        for directory, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                names.append(str((Path(directory) / name).relative_to(root)))
        return sorted(names)


def load_terms(path):
    terms = []
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.split('#', 1)[0].strip() if not line.lstrip().startswith('\\#') else line.strip()
        if line:
            terms.append((f'private term {number}', re.compile(line, re.I)))
    return terms


def scan(root, terms):
    hits = []
    files = list_files(root)
    for name in files:
        if any(part in SKIP_DIRS for part in Path(name).parts[:-1]):
            continue
        for pattern, reason in FORBIDDEN_PATHS:
            if re.search(pattern, name, re.I):
                hits.append((name, 0, reason, name))
        for label, rule in terms:
            if rule.search(name):
                hits.append((name, 0, label + ' in file name', name))
        suffix = Path(name).suffix.lower()
        data = (root / name).read_bytes()
        if suffix in BINARY_SUFFIXES or b'\0' in data[:8192]:
            if name not in REVIEWED_BINARIES:
                hits.append((name, 0, 'unreviewed binary, image or document', name))
            continue
        text = data.decode('utf-8', errors='replace')
        for number, line in enumerate(text.splitlines(), 1):
            for label, rule in CONTENT_RULES + terms:
                match = rule.search(line)
                if match:
                    hits.append((name, number, label, match.group(0)))
            for match in SECRET_ASSIGNMENT.finditer(line):
                if not SAFE_SECRET.search(match.group(2)):
                    hits.append((name, number, 'SECRET_KEY literal', match.group(0)))
            for match in IPV4.finditer(line):
                if not ip_allowed(tuple(int(g) for g in match.groups())):
                    hits.append((name, number, 'IP address', match.group(0)))
            if name not in THIRD_PARTY_TEXT:
                for match in EMAIL.finditer(line):
                    if not PLACEHOLDER_EMAIL.search(match.group(0)):
                        hits.append((name, number, 'email address', match.group(0)))
    return files, hits


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--root', default=Path(__file__).resolve().parent.parent, type=Path)
    parser.add_argument('--terms', default=os.environ.get('FREEDOMSHOP_SCAN_TERMS'),
                        help='private file of extra case-insensitive regular expressions, kept outside the tree')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    terms = load_terms(args.terms) if args.terms else []
    files, hits = scan(root, terms)
    for name, number, reason, found in hits:
        # Private-term matches are reported by position only, so the output can be shared.
        shown = '' if reason.startswith('private term') else f': {found[:80]}'
        print(f'{name}:{number}: {reason}{shown}')
    print(f'public_scan: {len(files)} files, {len(terms)} private terms'
          f'{"" if terms else " (none loaded; pass --terms FILE)"}, {len(hits)} hits')
    return 1 if hits else 0


if __name__ == '__main__':
    sys.exit(main())
