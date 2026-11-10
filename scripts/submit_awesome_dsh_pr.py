#!/usr/bin/env python3
"""Submit tmem to awesome-dsh-plugin via GitHub REST API."""

import os
import sys
import json
import base64
import urllib.request
import urllib.error
import time

TARGET_REPO = "awesome-dsh-plugin/awesome-dsh-plugin"
TARGET_BRANCH = "main"
PLUGIN_FILE = "data/plugins/kentzjh-cmd__tmem.yml"
PLUGIN_YAML = """url: https://github.com/kentzjh-cmd/tmem
name: kentzjh-cmd/tmem
category: memory
description:
  en: Invisible backward-slicing execution trace memory compiler for DSH, compressing sessions into guarded L0/L1/L2 playbooks.
  zh: 隐式后向切片执行轨迹记忆编译器，将会话自动提炼为带守卫条件的 L0/L1/L2 执行剧本。
"""

PR_TITLE = "Add kentzjh-cmd/tmem to memory category"
PR_BODY = """- [x] I added **one file** at `data/plugins/kentzjh-cmd__tmem.yml` — that single file is the whole submission.
- [x] My repo's `package.json` declares **`dsh.bundle`** (`dsh.bundle.patch: ./cordis.patch.yml`).
- [ ] My repo is at least **1 day old** — created 2026-11-10T08:20:54Z.
- [x] `category: memory` — invisible execution trace memory and guard-verified playbook recall.
- [x] Description states what the plugin does, no superlatives.
- [x] My repo has the `dsh-plugin` topic.

### About tmem
`tmem` compiles multi-turn agent interaction traces into deterministic execution playbooks using backward dependency slicing against git ground truth. In DSH (`@deepseek-ai/cordis`), it injects pre-flight speculative memory, enforces co-change file topologies, and provides causal L2 error repairs without LLM API overhead.
"""

def get_token():
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    cred_file = os.path.expanduser("~/.git-credentials")
    if os.path.exists(cred_file):
        with open(cred_file) as f:
            for line in f:
                if "github.com" in line:
                    parts = line.strip().split("@")[0].split("://")[-1].split(":")
                    if len(parts) == 2:
                        return parts[1]
    return None

def api_request(url, method="GET", data=None, token=None):
    proxy_handler = urllib.request.ProxyHandler({
        "http": os.environ.get("http_proxy", "http://127.0.0.1:10808"),
        "https": os.environ.get("https_proxy", "http://127.0.0.1:10808"),
    })
    opener = urllib.request.build_opener(proxy_handler)
    headers = {
        "User-Agent": "tmem-submitter",
        "Accept": "application/vnd.github.v3+json",
    }
    if token:
        headers["Authorization"] = f"token {token}"
    
    payload = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    with opener.open(req) as resp:
        content = resp.read().decode("utf-8")
        return json.loads(content) if content else {}

def main():
    token = get_token()
    if not token:
        print("❌ Error: No GitHub token found in GITHUB_TOKEN or ~/.git-credentials.")
        sys.exit(1)

    print("🔑 Testing GitHub Token authentication...")
    try:
        user_info = api_request("https://api.github.com/user", token=token)
        username = user_info.get("login")
        print(f"✅ Authenticated as: {username}")
    except Exception as e:
        print(f"❌ Authentication failed: {e}")
        sys.exit(1)

    print(f"🍴 Checking or creating fork of {TARGET_REPO} under {username}...")
    try:
        fork_info = api_request(f"https://api.github.com/repos/{TARGET_REPO}/forks", method="POST", data={}, token=token)
        print(f"✅ Fork created/verified: {fork_info.get('html_url')}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        print(f"❌ Fork request returned {e.code} {e.reason}: {body}")
        print("\n🔍 Permission Diagnostic:")
        print("Current token lacks permission to fork public repositories into your account.")
        print("To fix this, update your GitHub PAT with:")
        print("  - For Classic PAT: 'public_repo' scope")
        print("  - For Fine-Grained PAT: 'All repositories' (or public repo access) + 'Contents: Read & write' + 'Pull requests: Read & write'")
        sys.exit(1)

    fork_repo = f"{username}/awesome-dsh-plugin"
    branch_name = "add-tmem-plugin"

    # Wait for fork to be ready if newly created
    print("⏳ Verifying fork repository branch status...")
    main_ref = None
    for _ in range(10):
        try:
            main_ref = api_request(f"https://api.github.com/repos/{fork_repo}/git/ref/heads/{TARGET_BRANCH}", token=token)
            if main_ref:
                break
        except Exception:
            time.sleep(2)

    if not main_ref:
        print("❌ Could not get default branch ref on fork.")
        sys.exit(1)

    base_sha = main_ref["object"]["sha"]
    print(f"✅ Fork default branch SHA: {base_sha[:8]}")

    print(f"🌿 Creating branch '{branch_name}' on fork...")
    try:
        api_request(f"https://api.github.com/repos/{fork_repo}/git/refs", method="POST", data={
            "ref": f"refs/heads/{branch_name}",
            "sha": base_sha
        }, token=token)
        print(f"✅ Branch '{branch_name}' created.")
    except urllib.error.HTTPError as e:
        if e.code == 422:
            print(f"ℹ️ Branch '{branch_name}' already exists, continuing.")
        else:
            raise

    print(f"📝 Creating entry file '{PLUGIN_FILE}'...")
    file_content_b64 = base64.b64encode(PLUGIN_YAML.encode("utf-8")).decode("utf-8")
    commit_data = {
        "message": f"feat: add {username}/tmem to memory category",
        "content": file_content_b64,
        "branch": branch_name
    }
    # Check if file exists in branch to provide sha for update if needed
    try:
        existing = api_request(f"https://api.github.com/repos/{fork_repo}/contents/{PLUGIN_FILE}?ref={branch_name}", token=token)
        commit_data["sha"] = existing["sha"]
    except Exception:
        pass

    try:
        api_request(f"https://api.github.com/repos/{fork_repo}/contents/{PLUGIN_FILE}", method="PUT", data=commit_data, token=token)
        print(f"✅ File '{PLUGIN_FILE}' committed.")
    except Exception as e:
        print(f"❌ Failed to commit file: {e}")
        sys.exit(1)

    print("🚀 Opening Pull Request to upstream...")
    pr_payload = {
        "title": PR_TITLE,
        "head": f"{username}:{branch_name}",
        "base": TARGET_BRANCH,
        "body": PR_BODY
    }
    try:
        pr_info = api_request(f"https://api.github.com/repos/{TARGET_REPO}/pulls", method="POST", data=pr_payload, token=token)
        print(f"🎉 Pull Request successfully created! URL: {pr_info.get('html_url')}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        print(f"❌ PR creation failed ({e.code}): {body}")
        sys.exit(1)

if __name__ == "__main__":
    main()
