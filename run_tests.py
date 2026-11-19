"""Checks all files of the repo and runs all tests. Started by the GitHub workflow "Tests" after every upload.

Exit code 0 = everything fine, 1 = something is wrong (the red lines above say what).
You can also run it on a computer:  python run_tests.py
"""
import glob
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
problems = []


def bad(message):
    print("❌ " + message)
    problems.append(message)


def good(message):
    print("✅ " + message)


# ---- 1. Python files: a file with workflow text or a typing mistake does not compile --------------
for path in sorted(glob.glob("*.py")):
    first = next((l.strip() for l in open(path, encoding="utf-8") if l.strip()), "")
    if re.match(r"^(name|on|jobs|permissions):", first):
        bad(f"{path}: the first line is '{first[:40]}'. This looks like a workflow (.yml) text, not Python. "
            "Upload the right content under this file name.")
        continue
    try:
        py_compile.compile(path, cfile=os.path.join(tempfile.gettempdir(), "x.pyc"), doraise=True)
        good(f"{path}: valid Python")
    except py_compile.PyCompileError as exc:
        bad(f"{path}: typing mistake or wrong content: {str(exc).strip().splitlines()[-1][:150]}")

# ---- 2. Workflow files -----------------------------------------------------------------------------
try:
    import yaml
except ImportError:
    yaml = None
    print("(pyyaml is not installed: workflow files are only checked loosely)")
folder = ".github/workflows" if os.path.isdir(".github/workflows") else "."
for path in sorted(glob.glob(os.path.join(folder, "*.yml"))):
    text = open(path, encoding="utf-8").read()
    first = next((l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")), "")
    if not re.match(r"^(name|on):", first):
        bad(f"{path}: the first line is '{first[:40]}'. A workflow must start with 'name:'. "
            "Did you paste Python code into a .yml file?")
        continue
    if yaml is not None:
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            bad(f"{path}: not valid YAML ({str(exc).splitlines()[0][:120]})")
            continue
        if not isinstance(doc, dict) or "jobs" not in doc or not (True in doc or "on" in doc):
            bad(f"{path}: needs 'on:' and 'jobs:'")
            continue
    for script in re.findall(r"python3?\s+([\w./-]+\.py)", text):
        if not os.path.exists(script):
            bad(f"{path}: starts {script}, but that file is not in the repo")
    good(f"{path}: valid workflow")

# ---- 3. Settings file ------------------------------------------------------------------------------
try:
    with open("config.toml", "rb") as f:
        tomllib.load(f)
    good("config.toml: valid")
except FileNotFoundError:
    print("(no config.toml: the built-in defaults are used)")
except (tomllib.TOMLDecodeError, OSError) as exc:
    bad(f"config.toml: typing mistake ({exc}). Until it is fixed the built-in defaults are used.")

# ---- 4. The Google script (JavaScript) ------------------------------------------------------------
node = shutil.which("node")
if os.path.exists("meetup_to_telegram.gs") and node:
    tmp = os.path.join(tempfile.mkdtemp(), "check.js")
    shutil.copy("meetup_to_telegram.gs", tmp)
    r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
    good("meetup_to_telegram.gs: valid JavaScript") if r.returncode == 0 else bad(
        "meetup_to_telegram.gs: " + (r.stderr.strip().splitlines() or ["typing mistake"])[-1][:150])

# ---- 5. The tests ------------------------------------------------------------------------------------
tests = [[sys.executable, t] for t in sorted(glob.glob("test_*.py"))]
if node and os.path.exists("test_meetup.js") and os.path.exists("meetup_to_telegram.gs"):
    tests.append([node, "test_meetup.js"])
for cmd in tests:
    name = cmd[1]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        bad(f"{name}: took too long")
        continue
    if r.returncode == 0:
        good(f"{name}: passed")
    else:
        lines = [l for l in (r.stdout + r.stderr).splitlines() if l.startswith("FAIL") or "Error" in l]
        bad(f"{name}: failed")
        for l in lines[:8]:
            print("     " + l[:200])
        if not lines:
            for l in (r.stdout + r.stderr).splitlines()[-6:]:
                print("     " + l[:200])

print()
if problems:
    print(f"{len(problems)} problem(s):")
    for p in problems:
        print(" - " + p)
    sys.exit(1)
print("Everything is fine.")
