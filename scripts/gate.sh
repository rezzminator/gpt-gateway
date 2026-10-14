#!/usr/bin/env bash
# The gate, the only definition of ready: compile, shellcheck, checks, tests — one GATE line per link, then the verdict.
set -uo pipefail; export LC_ALL=C
PY=/usr/bin/python3
CLEAN_PATH=/usr/bin:/bin:/usr/sbin:/sbin
case ${1:-} in '') ;; *) echo "usage: gate.sh" >&2; exit 2 ;; esac
P=$(cd "$(dirname "$0")/.." && pwd) || { echo "GATE ERROR cannot resolve the repository root"; exit 2; }
cd "$P" || { echo "GATE ERROR cannot cd $P"; exit 2; }
T=$(mktemp -d) || { echo "GATE ERROR mktemp failed"; exit 2; }; trap 'rm -rf "$T"' EXIT
failed=""; errored=""
report() { printf 'GATE %-10s %-5s %s\n' "$1" "$2" "$3"; case $2 in FAIL) failed="$failed $1";; ERROR) errored="$errored $1";; esac; return 0; }
git ls-files -co --exclude-standard > "$T/listed" || { echo "GATE ERROR git ls-files failed under $P"; exit 2; }
while read -r f; do [ -f "$f" ] && echo "$f"; done < "$T/listed" > "$T/all"   # tracked + new, minus deleted
if grep -q ' ' "$T/all"; then echo "GATE ERROR a path holds a space: $(grep ' ' "$T/all" | head -n 1)"; exit 2; fi

# compile: every Python file, bytecode into the scratch directory so the tree gets none
grep -E '\.py$' "$T/all" > "$T/py"
if [ ! -x "$PY" ]; then report compile ERROR "$PY missing"
elif [ ! -s "$T/py" ]; then report compile ERROR "no Python file listed"
else
  "$PY" -I -B -c '
import os, py_compile, sys
out, listing = sys.argv[1], sys.argv[2]
with open(listing) as f:
    names = [n for n in f.read().split("\n") if n]
bad = []
for i, name in enumerate(names):
    try:
        py_compile.compile(name, cfile=os.path.join(out, "%d.pyc" % i), doraise=True)
    except py_compile.PyCompileError as e:
        bad.append(name)
        print(e.msg, file=sys.stderr)
print(len(names), " ".join(bad))
sys.exit(1 if bad else 0)
' "$T" "$T/py" > "$T/compile.out" 2> "$T/compile.err"; s=$?
  read -r n bad < "$T/compile.out" 2>/dev/null || n=""
  case $s in
    0) report compile PASS "$n files" ;;
    1) cat "$T/compile.err"; report compile FAIL "$bad" ;;
    *) cat "$T/compile.err"; report compile ERROR "py_compile exited $s" ;;
  esac
fi

# lint: shellcheck over every shell script; a missing tool is an ERROR, never a skipped link
grep -E '\.sh$' "$T/all" > "$T/sh"
# One version on every host (macOS, Linux, the fence): a newer or older release reports different findings.
SHELLCHECK_VERSION=0.11.0
SC=$(command -v shellcheck) || SC=""
sc_found="of unknown version"; [ -z "$SC" ] || sc_found=$("$SC" --version 2>/dev/null | sed -n 's/^version: //p'); [ -n "$sc_found" ] || sc_found="of unknown version"
if [ -z "$SC" ]; then report shellcheck ERROR "shellcheck not found on PATH — brew install shellcheck"
elif [ "$sc_found" != "$SHELLCHECK_VERSION" ]; then
  report shellcheck ERROR "shellcheck $sc_found found, the gate pins $SHELLCHECK_VERSION — install the v$SHELLCHECK_VERSION release from github.com/koalaman/shellcheck/releases"
elif [ ! -s "$T/sh" ]; then report shellcheck ERROR "no shell script listed"
else
  # shellcheck disable=SC2046 # word splitting is the point: a path holding a space was refused above
  "$SC" $(cat "$T/sh") > "$T/sc.out" 2>&1; s=$?
  case $s in
    0) report shellcheck PASS "$(wc -l < "$T/sh" | tr -d ' ') scripts" ;;
    1) cat "$T/sc.out"; report shellcheck FAIL "$(grep -oE '^In [^ ]+' "$T/sc.out" | sed 's/^In //' | sort -u | paste -sd ' ' -)" ;;
    *) cat "$T/sc.out"; report shellcheck ERROR "shellcheck exited $s" ;;
  esac
fi

# checks: scripts/checks.sh, its lines passed through
if [ ! -f scripts/checks.sh ]; then report checks ERROR "scripts/checks.sh missing"
else
  "$BASH" scripts/checks.sh > "$T/checks.out" 2>&1; s=$?
  cat "$T/checks.out"
  n=$(grep -c '^CHECK ' "$T/checks.out")
  if [ "$n" -eq 0 ]; then report checks ERROR "no CHECK line printed (exit $s)"
  else case $s in
    0) report checks PASS "$n checks" ;;
    1) report checks FAIL "$(awk '$1=="CHECK" && $3=="FAIL"{print $2}' "$T/checks.out" | paste -sd ' ' -)" ;;
    *) report checks ERROR "$(awk '$1=="CHECK" && $3=="ERROR"{print $2}' "$T/checks.out" | paste -sd ' ' -) (exit $s)" ;;
  esac; fi
fi

# tests: the whole suite in a clean environment — no inherited variable, an empty HOME, its own temp root
mkdir "$T/home" "$T/tmp"
env -i HOME="$T/home" TMPDIR="$T/tmp" PATH="$CLEAN_PATH" LC_ALL=C \
  "$PY" -I -B -m unittest discover -s tests -t . > "$T/tests.out" 2>&1; s=$?
ran=$(grep -E '^Ran [0-9]+ tests? in ' "$T/tests.out" | tail -n 1)
count=$(echo "$ran" | awk '{print $2}')
skipped=$(grep -oE 'skipped=[0-9]+' "$T/tests.out" | tail -n 1 | cut -d= -f2)
if [ -z "$ran" ]; then tail -n 40 "$T/tests.out"; report tests ERROR "no 'Ran N tests' line (exit $s)"
elif [ "$count" -eq 0 ]; then report tests ERROR "Ran 0 tests"
elif [ "$s" -ne 0 ]; then grep -E '^(FAIL|ERROR): ' "$T/tests.out"; tail -n 40 "$T/tests.out"
  report tests FAIL "$(grep -cE '^(FAIL|ERROR): ' "$T/tests.out") of $count failed"
elif [ -n "$skipped" ]; then report tests ERROR "$skipped of $count skipped — a skipped test is a check that did not run"
else report tests PASS "${ran#Ran }"; fi

if [ -n "$errored" ]; then echo "GATE ERROR$errored${failed:+ · FAIL$failed}"; exit 2
elif [ -n "$failed" ]; then echo "GATE FAIL$failed"; exit 1
else echo "GATE PASS"; exit 0; fi
