#!/usr/bin/env python3
"""registration check (voner-index CI): the rules voner-crawl applies
to a release, self-contained (python3 + git, no voner build). mirrors
crawl::check minus full RON parsing — the intentional drift budget:
file presence + name/version string checks stand in for the parse.

usage: check_record.py <record file>   exit 0 = clean, 1 = failed
"""

import os
import re
import subprocess
import sys
import tempfile

def fail(why):
    print(f"FAIL: {why}")
    sys.exit(1)

def git(args, cwd=None, check=True):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    r = subprocess.run(["git"] + args, cwd=cwd, env=env,
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        fail(r.stderr.strip() or f"git {args[0]} failed")
    return r

def tag_version(tag):
    t = tag[1:] if tag.startswith("v") else tag
    parts = t.split(".")
    if not (2 <= len(parts) <= 3):
        return None
    if all(p.isdigit() for p in parts):
        return tuple(int(p) for p in parts), t
    return None

def main():
    record = sys.argv[1]
    name = os.path.basename(record)
    text = open(record).read()
    m = re.search(r'repo:\s*"([^"]+)"', text)
    if not m:
        fail(f"{name}: no repo: url in the record")
    url = m.group(1)
    print(f"record: {name} -> {url}")

    # public readability — anonymous ls-remote (clients have no credentials)
    r = git(["ls-remote", "--tags", url], check=False)
    if r.returncode != 0:
        fail(f"{name}: the repo is not publicly readable "
             f"(private repos are unreadable by anonymous clients)")
    tags = {}
    for line in r.stdout.splitlines():
        sha, ref = line.split("\t")
        tag = ref.removeprefix("refs/tags/")
        if tag.endswith("^{}"):
            tags[tag[:-3]] = sha          # annotated: the peeled commit wins
        else:
            tags.setdefault(tag, sha)
    versions = [(tv, tag, sha) for tag, sha in tags.items()
                if (tv := tag_version(tag))]
    if not versions:
        fail(f"{name}: no version tags (need a tag like v0.1.0)")
    versions.sort()
    (ver_ints, ver_str), tag, sha = versions[-1]
    print(f"tags ok: {len(versions)} version tag(s), newest {tag}")

    # the newest release: fetch + validate the pack at that commit
    with tempfile.TemporaryDirectory() as tmp:
        git(["clone", "--depth", "1", "--branch", tag, url, tmp])
        pack_ron = os.path.join(tmp, "pack.ron")
        if not os.path.isfile(pack_ron):
            fail(f"{name}: no pack.ron at the repo root (@{tag})")
        manifest = open(pack_ron).read()
        nm = re.search(r'name:\s*"([^"]+)"', manifest)
        if not nm or nm.group(1) != name:
            fail(f"{name}: pack.ron names '{nm.group(1) if nm else '?'}', "
                 f"registered as '{name}'")
        vm = re.search(r'version:\s*Some\("([^"]+)"\)', manifest)
        if vm and vm.group(1) != ver_str:
            fail(f"{name}: pack.ron declares version {vm.group(1)}, "
                 f"the tag says {ver_str}")
    print(f"pack ok: {name} {ver_str} @ {sha[:8]}")
    print("PASS")

main()
