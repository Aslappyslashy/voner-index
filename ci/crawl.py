#!/usr/bin/env python3
"""the crawl (voner-index CI): a self-contained port of voner-crawl's
rules (the Rust crawler is the reference implementation; the engine
repo isn't on GitHub, so the index carries its own).

per registered repo: ls-remote the tags, diff against the recorded
pins — the index entries ARE the state. new tag → clone that commit,
validate (pack.ron's name is the registered name; a declared version
equals the tag), derive description/role/depends, append the pinned
IndexVersion. a re-tag is flagged, never applied; a deleted tag
leaves the entry alone; suspended records are skipped.

usage: crawl.py [checkout]   exit 0 always (flags are data, not errors)
"""

import os
import re
import subprocess
import sys
import tempfile

DOMAINS = {"materials", "nodes", "affordances", "states", "liquids",
           "odors", "modifiers", "damage", "text"}
SPECIAL = {"pack", "attention", "fragment"} | DOMAINS

def git(args, cwd=None, check=True):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    r = subprocess.run(["git"] + args, cwd=cwd, env=env,
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"git {args[0]} failed")
    return r

def tag_version(tag):
    t = tag[1:] if tag.startswith("v") else tag
    parts = t.split(".")
    if 2 <= len(parts) <= 3 and all(p.isdigit() for p in parts):
        return t
    return None

def esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')

def read_records(repos_dir):
    out = []
    if not os.path.isdir(repos_dir):
        return out
    for shard in sorted(os.listdir(repos_dir)):
        d = os.path.join(repos_dir, shard)
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            text = open(os.path.join(d, f)).read()
            url = re.search(r'repo:\s*"([^"]+)"', text)
            suspended = "Suspended" in text
            out.append((f, url.group(1) if url else None, suspended))
    return out

def read_entry(path):
    """(version, sha256) pairs already pinned, in order"""
    if not os.path.isfile(path):
        return []
    text = open(path).read()
    pins = []
    for block in re.split(r"\(\s*version:", text)[1:]:
        v = re.search(r'"([^"]+)"', block)
        s = re.search(r'sha256:\s*"([^"]+)"', block)
        if v and s:
            pins.append((v.group(1), s.group(1)))
    return pins

def ls_tags(url):
    r = git(["ls-remote", "--tags", url], check=False)
    if r.returncode != 0:
        return None
    tags = {}
    for line in r.stdout.splitlines():
        sha, ref = line.split("\t")
        tag = ref.removeprefix("refs/tags/")
        if tag.endswith("^{}"):
            tags[tag[:-3]] = sha
        else:
            tags.setdefault(tag, sha)
    return tags

def manifest_field(text, field):
    m = re.search(field + r':\s*Some\("((?:[^"\\]|\\.)*)"\)', text)
    return m.group(1).replace('\\"', '"').replace("\\\\", "\\") if m else None

def manifest_depends(text):
    m = re.search(r"depends:\s*\[(.*?)\]", text, re.S)
    if not m:
        return []
    body = m.group(1)
    # structured entries first — the pattern swallows Some("...")'s
    # parens, so bare-name extraction afterwards sees none of it
    # structured entries first — the pattern swallows Some("...")'s
    # parens and the pretty-printer's trailing comma, so bare-name
    # extraction afterwards sees none of it
    structured = (r'\(\s*name:\s*"([^"]+)"'
                  r'(?:\s*,\s*constraint:\s*Some\("([^"]+)"\))?\s*,?\s*\)')
    out = [(name, constraint or "*")
           for name, constraint in re.findall(structured, body)]
    bare = re.sub(structured, " ", body)
    for name in re.findall(r'"([^"]+)"', bare):
        out.append((name, "*"))
    return out

def infer_role(root_files, declared):
    if declared:
        return declared.lower()
    stems = {f[:-4] for f in root_files if f.endswith(".ron")}
    scenes = stems - SPECIAL
    if scenes:
        return "world"
    if "text" in stems and not (stems & (DOMAINS - {"text"})):
        return "voice"
    return "library"

def write_entry(path, name, description, role, versions):
    """versions: [(version, url, sha, engine, depends, yanked)]"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = ["(", f'    name: "{esc(name)}",']
    lines.append(f'    description: Some("{esc(description)}"),' if description
                 else "    description: None,")
    lines.append(f'    role: Some("{role}"),' if role else "    role: None,")
    lines.append("    versions: [")
    for (v, url, sha, engine, depends, yanked) in versions:
        dep = ", ".join(f'("{esc(n)}", "{esc(c)}")' for n, c in depends)
        eng = f'Some("{esc(engine)}")' if engine else "None"
        lines.append(f'        (version: "{esc(v)}", url: "{esc(url)}", '
                     f'sha256: "{sha}", engine: {eng}, depends: [{dep}], '
                     f'yanked: {"true" if yanked else "false"}),')
    lines.append("    ],")
    lines.append(")")
    open(path, "w").write("\n".join(lines) + "\n")

def crawl(checkout):
    updated, flagged, skipped = [], [], []
    for name, url, suspended in read_records(os.path.join(checkout, "repos")):
        if suspended:
            skipped.append(f"{name}: suspended")
            continue
        if not url:
            flagged.append(f"{name}: no repo: url in the record")
            continue
        tags = ls_tags(url)
        if tags is None:
            flagged.append(f"{name}: repo not publicly readable")
            continue
        entry_path = os.path.join(checkout, "index", name[:2], name)
        pins = read_entry(entry_path)
        known = dict(pins)
        new_versions = []
        description = role = None
        for tag, sha in sorted(tags.items()):
            version = tag_version(tag)
            if not version:
                skipped.append(f"{name}: tag '{tag}' is not a version")
                continue
            if version in known:
                if known[version] != sha:
                    flagged.append(f"{name} {version}: re-tag — the index keeps {known[version][:8]}")
                continue
            # a new release: fetch + validate at the pinned commit
            with tempfile.TemporaryDirectory() as tmp:
                r = git(["clone", "--depth", "1", "--branch", tag, url, tmp],
                        check=False)
                if r.returncode != 0:
                    flagged.append(f"{name} {version}: clone failed: {r.stderr.strip()}")
                    continue
                pack_ron = os.path.join(tmp, "pack.ron")
                if not os.path.isfile(pack_ron):
                    flagged.append(f"{name} {version}: no pack.ron at the repo root")
                    continue
                manifest = open(pack_ron).read()
                nm = re.search(r'name:\s*"([^"]+)"', manifest)
                if not nm or nm.group(1) != name:
                    flagged.append(f"{name} {version}: pack.ron names "
                                   f"'{nm.group(1) if nm else '?'}', registered as '{name}'")
                    continue
                vm = manifest_field(manifest, "version")
                if vm and vm != version:
                    flagged.append(f"{name} {version}: pack.ron declares version {vm}, "
                                   f"the tag says {version}")
                    continue
                description = manifest_field(manifest, "description") or description
                engine = manifest_field(manifest, "engine")
                depends = manifest_depends(manifest)
                root_files = os.listdir(tmp)
                role = infer_role(root_files, manifest_field(manifest, "role"))
                new_versions.append((version, url, sha, engine, depends, False))
                updated.append((name, version))
        if new_versions:
            # merge: keep old pins (all fields), append the new
            old_full = []
            # old pins' full tuples aren't recoverable from the summary
            # parse — re-parse the entry file fully
            if os.path.isfile(entry_path):
                old_full = parse_full_versions(open(entry_path).read())
            write_entry(entry_path, name, description, role,
                        old_full + new_versions)
    report = {"updated": updated, "flagged": flagged, "skipped": skipped}
    with open(os.path.join(checkout, "crawl.ron"), "w") as f:
        f.write("(\n    updated: [%s],\n    flagged: [%s],\n    skipped: [%s],\n)\n" % (
            ", ".join(f'("{n}", "{v}")' for n, v in updated),
            ", ".join(f'"{esc(x)}"' for x in flagged),
            ", ".join(f'"{esc(x)}"' for x in skipped)))
    print(f"crawl: {len(updated)} updated, {len(flagged)} flagged, {len(skipped)} skipped")
    for x in flagged:
        print(f"FLAGGED: {x}")

def parse_full_versions(text):
    """full version tuples from an existing entry (for the merge)"""
    out = []
    for block in re.split(r"\(\s*version:", text)[1:]:
        block = block.rsplit(")", 1)[0]
        g = lambda pat: (re.search(pat, block) or [None])[1] if re.search(pat, block) else None
        v = g(r'"([^"]+)"')
        url = g(r'url:\s*"([^"]+)"')
        sha = g(r'sha256:\s*"([^"]+)"')
        engine = g(r'engine:\s*Some\("([^"]+)"\)')
        deps = re.findall(r'\("([^"]+)",\s*"([^"]+)"\)', block)
        yanked = "yanked: true" in block
        out.append((v, url, sha, engine, deps, yanked))
    return out

if __name__ == "__main__":
    crawl(sys.argv[1] if len(sys.argv) > 1 else ".")
