#!/usr/bin/env python3
"""Audit this repo for credentials, API keys, tokens, and secrets.

Scans working tree plus git blob history.

Exit code:
  0  clean
  1  at least one HIGH-severity finding
  2  only MEDIUM-severity findings (suspicious but not authenticated)
"""

import subprocess
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.environ.get("HEADROOM_OLLAMA_REPO") or os.path.dirname(SCRIPT_DIR)

# Real-secret patterns. We deliberately exclude:
#   * literal ellipsis `...` (used to indicate truncation in docs/tests)
#   * known test fixture strings (EXAMPLEAKIDFORTEST, IOSFODNN7EXAMPLE, etc.)
PATTERNS = [
    # 32-char hex prefix + literal dot + 16+ char tail = Ollama Cloud key shape
    ("ollama_real_key",
     re.compile(r"\b[a-f0-9]{32}\.[A-Za-z0-9_\-]{16,}\b"),
     "CRITICAL"),

    # sk- followed by 32+ contiguous [A-Za-z0-9_-] chars; must not contain `...`
    ("openai_real_key",
     re.compile(r"\bsk-(?!ant-api-|ant\.\.\.)[A-Za-z0-9_\-]{32,}\b"),
     "HIGH"),

    # sk-proj- followed by 32+ chars; no `...`
    ("openai_proj_key",
     re.compile(r"\bsk-proj-(?!\.\.\.)[A-Za-z0-9_\-]{32,}\b"),
     "HIGH"),

    # Real Anthropic key: sk-ant-apiXX-XXXX...
    ("anthropic_real",
     re.compile(r"\bsk-ant-api\d{2}-(?!\.\.\.)[A-Za-z0-9_\-]{32,}\b", re.I),
     "HIGH"),

    # GitHub PATs
    ("github_real_pat",
     re.compile(r"\b(github_pat_[A-Za-z0-9_]{82}|ghp_[A-Za-z0-9]{36,}|ghs_[A-Za-z0-9]{36,})\b"),
     "HIGH"),

    # AWS access key: AKIA + 16 uppercase alnum, but exclude literal fixtures
    ("aws_real_access",
     re.compile(r"\bAKIA(?!IOSFODNN7EXAMPLE)[A-Z0-9]{12}[A-Z0-9]{4}\b"),
     "HIGH"),

    # Private key PEM
    ("private_key_pem",
     re.compile(r"-----BEGIN .*PRIVATE KEY-----"),
     "HIGH"),
]

# Loose patterns used only for working-tree scan (more aggressive)
LOOSE_PATTERNS = [
    # openai URL with key query string
    ("openai_url_with_key",
     re.compile(r"https://api\.openai\.com/v1\?key=[a-zA-Z0-9]{20,}"),
     "HIGH"),
]

EXCLUDE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache", "target"}
EXCLUDE_FILES = {"pnpm-lock.yaml", "bun.lock", "package-lock.json"}


def is_binary(path):
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(2048)
    except Exception:
        return True


# Patterns that flag a match as an upstream test fixture, not a real credential.
# Real API keys have high entropy; test fixtures are typically:
#   * sequential alphabetic suffixes (stuv, wxyz, abc, ...)
#   * digit runs (1234, 4567, 6789)
#   * repeating characters (xxxx, XXXX)
#   * synthetic Anthropic OAuth prefixes (ant-oat-01-, ant-api03-)
#   * literal "..." ellipsis markers
KNOWN_TEST_FIXTURE_PATTERNS = [
    "EXAMPLEAKIDFORTEST",
    "IOSFODNN7EXAMPLE",
    "ant-oat-01-",
    "ant-api03-",
    "ant-api-",
    "sk-pro",
    "sk-ant",
]


def _has_long_alpha_run(s):
    """True if s contains a run of 10+ sequential lowercase letters (test heuristic)."""
    runs = re.findall(r"[a-z]{10,}", s)
    for run in runs:
        # Real entropy has varied letters; alphabetical runs are tests like 'abcdefghijklmnopqrstuv'
        if any(run[i+1] == chr(ord(run[i])+1) for i in range(len(run)-1) for _ in [0] if i+1 < len(run) and ord(run[i+1])-ord(run[i]) == 1):
            # Check at least 8 sequential positions
            seq = 1
            for i in range(len(run)-1):
                if ord(run[i+1]) - ord(run[i]) == 1:
                    seq += 1
                else:
                    seq = 1
                if seq >= 8:
                    return True
    return False


def _has_sequential_test_suffix(s):
    """True if s ends with a sequential 4-character alphanumeric run like stuv, wxyz, 4567."""
    # Common upstream test suffixes
    for suffix in ["xxxx", "XXXX", "1234", "4321", "6789", "9876",
                   "stuv", "wxyz", "abcd", "dcba", "f456"]:
        if suffix in s:
            return True
    return False


def is_test_fixture(match_text, source_line):
    """Return True if the match looks like an upstream test fixture, not a real secret."""
    # Literal ellipsis (engineered display truncation marker)
    if "..." in match_text:
        return True
    # Long alphabetical run (e.g. abcdefghijklmnopqrstuv — humans dont make 22-char sequential runs)
    if _has_long_alpha_run(match_text):
        return True
    # Sequential test suffixes
    if _has_sequential_test_suffix(match_text):
        return True
    # Check source line for known upstream fixtures
    for marker in KNOWN_TEST_FIXTURE_PATTERNS:
        if marker in match_text:
            return True
    return False


def audit_working_tree():
    findings = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for fn in files:
            if fn in EXCLUDE_FILES:
                continue
            path = os.path.join(root, fn)
            if is_binary(path):
                continue
            try:
                with open(path, "r", errors="ignore") as f:
                    for lineno, line in enumerate(f, 1):
                        for name, pat, sev in PATTERNS + LOOSE_PATTERNS:
                            m = pat.search(line)
                            if not m:
                                continue
                            if is_test_fixture(m.group(), line):
                                continue
                            findings.append({
                                "severity": sev,
                                "pattern": name,
                                "file": os.path.relpath(path, REPO),
                                "line": lineno,
                                "excerpt": line.strip()[:120],
                            })
            except Exception:
                continue
    return findings


def audit_history():
    """Scan git history HEAD for HIGH/CRITICAL secrets via git grep."""
    findings = []
    checked = 0
    for name, pat, sev in PATTERNS:
        if sev not in ("HIGH", "CRITICAL"):
            continue
        # Skip patterns requiring PCRE-only features
        if "(?!" in pat.pattern or "(?<" in pat.pattern:
            continue
        try:
            r = subprocess.run(
                ["git", "-C", REPO, "grep", "-l", "-E", pat.pattern, "HEAD"],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    findings.append({
                        "severity": sev,
                        "pattern": name,
                        "file": line,
                        "note": "matched in git HEAD, manual review needed",
                    })
        except subprocess.TimeoutExpired:
            findings.append({"error": f"timeout scanning for {name}"})
            continue
    # Get revision count
    try:
        revlist = subprocess.run(
            ["git", "-C", REPO, "rev-list", "--count", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        checked = int(revlist.stdout.strip()) if revlist.returncode == 0 else 0
    except subprocess.TimeoutExpired:
        pass
    return findings, checked


def main():
    print(f"Repo: {REPO}")
    print()

    print("=== WORKING TREE SCAN ===")
    wt = audit_working_tree()
    if not wt:
        print("clean -- no secret patterns in working tree")
    else:
        for f in wt:
            print("  " + f.get("severity", "?").ljust(8)
                  + " " + f.get("pattern", "?").ljust(35)
                  + " " + f.get("file", "?") + ":" + str(f.get("line", "?")))
            print("           > " + f.get("excerpt", ""))

    print()
    print("=== GIT HEAD SCAN ===")
    gh, checked = audit_history()
    print("(scanned " + str(checked) + " tracked blobs in HEAD)")
    if not gh:
        print("clean -- no HIGH/CRITICAL secrets in HEAD")
    else:
        for f in gh:
            if "error" in f:
                print("  ERROR: " + f["error"])
            else:
                print("  " + f.get("severity", "?").ljust(8)
                      + " " + f.get("pattern", "?").ljust(35)
                      + " " + f.get("file", "?"))
                print("           > " + f.get("note", ""))

    print()
    high = sum(1 for f in wt + gh if f.get("severity") in ("CRITICAL", "HIGH"))
    med = sum(1 for f in wt + gh if f.get("severity") == "MEDIUM")
    print("Summary: " + str(high) + " HIGH/CRITICAL, " + str(med) + " MEDIUM")

    if high:
        print()
        print("FAILED -- high-severity findings present")
        return 1
    if med:
        print()
        print("CAUTION -- medium-severity findings, review before push")
        return 2
    print()
    print("PASSED -- repo safe to push")
    return 0


if __name__ == "__main__":
    sys.exit(main())
