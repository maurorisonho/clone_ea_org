#!/usr/bin/env python3
"""
#
# MIT License
# 
# Copyright (c) 2025 Mauro Risonho de Paula Assumpção
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.  
# 
# $pip install requests tqdm
# 
# Clone all repositories from the Electronic Arts GitHub organization into a local folder
# with an overall progress bar.
# 
# Usage:
#   python clone_ea_org.py [--dest electronicarts] [--token GITHUB_TOKEN] [--ssh]
#                          [--include-archived] [--workers 4] [--full] [--mirror]
# 
# Examples:
#   # Shallow clone (depth=1) all repos via HTTPS into ./electronicarts
#   python clone_ea_org.py
# 
#   # Use SSH URLs instead of HTTPS, 8 concurrent workers, include archived repos
#   python clone_ea_org.py --ssh --workers 8 --include-archived
# 
#   # Use a GitHub token from env (recommended to avoid low API rate limits)
#   python clone_ea_org.py --token $GITHUB_TOKEN
# 
"""
import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from typing import List, Dict, Optional

try:
    import requests  # type: ignore
except Exception as e:
    print("This script requires the 'requests' package. Install it with: pip install requests tqdm", file=sys.stderr)
    raise

try:
    from tqdm import tqdm  # type: ignore
except Exception as e:
    print("This script requires the 'tqdm' package. Install it with: pip install tqdm", file=sys.stderr)
    raise


ORG = "electronicarts"
API_URL = f"https://api.github.com/orgs/{ORG}/repos"


def fetch_all_repos(token: Optional[str], include_archived: bool) -> List[Dict]:
    """Fetch all repos from the org, handling pagination."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token.strip()}"

    repos: List[Dict] = []
    page = 1
    per_page = 100

    with tqdm(desc="Listing repositories from GitHub", unit="page") as bar:
        while True:
            params = {
                "per_page": per_page,
                "page": page,
                "type": "all",  # includes public and private if token has scope
                "sort": "full_name",
                "direction": "asc",
            }
            resp = requests.get(API_URL, headers=headers, params=params, timeout=60)
            if resp.status_code == 403 and "rate limit" in resp.text.lower():
                reset = resp.headers.get("X-RateLimit-Reset", "")
                wait_s = 60
                try:
                    if reset:
                        wait_s = max(1, int(reset) - int(time.time()))
                except Exception:
                    pass
                tqdm.write(f"Hit rate limit. Sleeping {wait_s} seconds...")
                time.sleep(wait_s)
                continue

            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise RuntimeError(f"Unexpected response: {json.dumps(data, indent=2)[:4000]}")

            if not data:
                break

            for repo in data:
                if not include_archived and repo.get("archived"):
                    continue
                repos.append(repo)

            page += 1
            bar.update(1)

    return repos


def run_git_cmd(args: List[str], cwd: Optional[str] = None) -> int:
    """Run a git command and stream output minimally."""
    # We won't parse progress from git; tqdm tracks per-repo completion.
    proc = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        # Print a trimmed line occasionally to keep users informed without flooding output.
        if line.strip():
            # Use tqdm.write so it doesn't break the progress bar rendering.
            tqdm.write(line.rstrip())
    return proc.wait()


def clone_one(repo: Dict, dest_dir: str, use_ssh: bool, shallow: bool, mirror: bool, retries: int = 2) -> str:
    name = repo["name"]
    url = repo["ssh_url"] if use_ssh else repo["clone_url"]

    # Destination path selection
    target = os.path.join(dest_dir, name + (".git" if mirror else ""))

    # If exists, try to update instead of reclone (unless mirror requested)
    if os.path.exists(target):
        try:
            if mirror:
                # For mirror, do a remote update --prune
                code = run_git_cmd(["git", "remote", "update", "--prune"], cwd=target)
            else:
                # Normal repo: fetch + fast-forward default branch if possible
                code = run_git_cmd(["git", "fetch", "--all", "--prune"], cwd=target)
                if code == 0:
                    _ = run_git_cmd(["git", "pull", "--ff-only"], cwd=target)
            if code == 0:
                return f"updated: {name}"
        except Exception:
            pass  # Fall through to re-clone on failure

    # Build clone command
    cmd = ["git", "clone"]
    if mirror:
        cmd.append("--mirror")
    if shallow and not mirror:
        cmd += ["--depth", "1", "--no-single-branch"]
    cmd += [url, target]

    attempt = 0
    delay = 5
    while attempt <= retries:
        code = run_git_cmd(cmd)
        if code == 0:
            return f"cloned: {name}"
        attempt += 1
        if attempt <= retries:
            tqdm.write(f"[{name}] clone failed with exit code {code}. Retrying in {delay}s (attempt {attempt}/{retries})...")
            time.sleep(delay)
            delay *= 2  # exponential backoff

    return f"failed: {name}"


def main():
    parser = argparse.ArgumentParser(description=f"Clone all repositories from the '{ORG}' GitHub organization with a progress bar.")
    parser.add_argument("--dest", default="electronicarts", help="Destination folder to hold all repos (default: electronicarts)")
    parser.add_argument("--token", default=os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN"), help="GitHub token to increase API limits (env: GITHUB_TOKEN or GH_TOKEN)")
    parser.add_argument("--ssh", action="store_true", help="Use SSH URLs instead of HTTPS (requires your SSH keys configured)")
    parser.add_argument("--include-archived", action="store_true", help="Include archived repositories (default: exclude)")
    parser.add_argument("--workers", type=int, default=min(8, (os.cpu_count() or 4)), help="Number of concurrent clones (default: up to 8)")
    parser.add_argument("--full", action="store_true", help="Do a full clone instead of shallow depth=1")
    parser.add_argument("--mirror", action="store_true", help="Use --mirror (bare) clone (implies full clone)")

    args = parser.parse_args()
    dest_dir = os.path.abspath(args.dest)
    os.makedirs(dest_dir, exist_ok=True)

    try:
        repos = fetch_all_repos(token=args.token, include_archived=args.include_archived)
    except Exception as e:
        print(f"Error fetching repositories list: {e}", file=sys.stderr)
        sys.exit(2)

    if not repos:
        print("No repositories found. (Maybe the org is empty or your token lacks access?)")
        return

    # Choose URL type ahead of time to avoid per-thread branching
    selected = []
    for r in repos:
        selected.append({
            "name": r.get("name"),
            "clone_url": r.get("clone_url"),
            "ssh_url": r.get("ssh_url"),
        })

    total = len(selected)
    print(f"Found {total} repositories to process (dest: {dest_dir})")

    shallow = not args.full and not args.mirror

    results: List[str] = []
    # Use a thread-safe tqdm progress bar
    with tqdm(total=total, desc="Cloning repositories", unit="repo") as pbar:
        def task(r: Dict) -> str:
            out = clone_one(r, dest_dir=dest_dir, use_ssh=args.ssh, shallow=shallow, mirror=args.mirror)
            pbar.update(1)
            return out

        # Limit workers to a sane number for git/IO
        workers = max(1, min(args.workers, 16))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(task, r) for r in selected]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    res = fut.result()
                    results.append(res)
                except Exception as e:
                    results.append(f"failed: {e}")

    # Summary
    ok = sum(1 for r in results if r.startswith(("cloned:", "updated:")))
    failed = [r for r in results if r.startswith("failed:")]
    print("\nSummary:")
    print(f"  Success: {ok}/{total}")
    if failed:
        print("  Failures:")
        for item in failed:
            print(f"    - {item}")

    # Write a machine-readable log
    log_path = os.path.join(dest_dir, f"clone_summary_{int(time.time())}.log")
    with open(log_path, "w", encoding="utf-8") as f:
        for line in results:
            f.write(line + "\n")
    print(f"\nDetailed log saved to: {log_path}")


if __name__ == "__main__":
    main()
