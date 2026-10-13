#!/usr/bin/env bash
# Every countable law of docs/design/gpt-gateway-layout.md § Gates, one CHECK line each, against committed baselines.
set -uo pipefail; export LC_ALL=C
P=$(cd "$(dirname "$0")/.." && pwd) || { echo "CHECK setup ERROR cannot resolve the repository root" >&2; exit 2; }
BASE=$P/scripts/baselines; MODE=${1:-check}; rc=0
DESIGN=docs/design/gpt-gateway-layout.md
case $MODE in check|--measure) ;; *) echo "usage: checks.sh [--measure]" >&2; exit 2;; esac
say() { printf 'CHECK %-22s %-7s %s\n' "$1" "$2" "$3"; case $2 in FAIL) [ $rc -ge 1 ] || rc=1;; ERROR) rc=2;; esac; return 0; }
cd "$P" || { say setup ERROR "cannot cd $P"; exit 2; }
T=$(mktemp -d) || { say setup ERROR "mktemp failed"; exit 2; }; trap 'rm -rf "$T"' EXIT; mkdir -p "$BASE"
listed() { git ls-files -co --exclude-standard | while read -r f; do [ -f "$f" ] && echo "$f"; done; }   # tracked + new, minus deleted
all() { cat "$T/all"; }
src() { all | grep -E '\.(py|sh)$' | grep -vE '^tests/'; }
# shellcheck disable=SC2329,SC2317 # called indirectly, as the lister gin and facade name
pys() { src | grep -E '\.py$'; }
shs() { src | grep -E '\.sh$'; }
tests_py() { all | grep -E '^tests/.*\.py$'; }
# shellcheck disable=SC2329,SC2317 # called indirectly, as the lister gin names
not_design() { all | grep -vxF "$DESIGN"; }
listed > "$T/all" || { say setup ERROR "git ls-files failed under $P"; exit 2; }
[ "$(src | wc -l)" -gt 0 ] || { say setup ERROR "no sources listed under $P"; exit 2; }
if grep -q ' ' "$T/all"; then say setup ERROR "a path holds a space: $(grep ' ' "$T/all" | head -n 1)"; exit 2; fi
while read -r f; do [ -r "$f" ] || echo "$f"; done < "$T/all" > "$T/unreadable"
[ -s "$T/unreadable" ] && { say setup ERROR "unreadable: $(paste -sd ' ' "$T/unreadable")"; exit 2; }
# gin <out> <lister> <grep args…>: grep over the files a lister prints; grep status 2 = could not read = ERROR, never an empty result
# shellcheck disable=SC2046 # word splitting is the point: setup refused any path holding a space
gin() { local out=$1 list=$2; shift 2; grep "$@" $($list) /dev/null > "$out"; [ $? -le 1 ]; }
by_file() { cut -d: -f1 | sort | uniq -c | awk '{print $2" "$1}'; }

# list ratchet: <id> <baseline name> <file of current offenders> — FAIL on a line the baseline lacks
ratchet() { local id=$1 b=$BASE/$2.txt cur=$3 new gone note=""; sort -u "$cur" -o "$cur"
  if [ "$MODE" = --measure ]; then
    if [ -f "$b" ]; then comm -12 "$b" "$cur" > "$T/m" && mv "$T/m" "$b"; else cp "$cur" "$b"; fi
    say "$id" MEASURE "$(wc -l < "$b" | tr -d ' ') entries"; return; fi
  [ -f "$b" ] || { say "$id" ERROR "baseline $b missing — cannot tell new from old"; return; }
  new=$(comm -13 "$b" "$cur"); gone=$(comm -23 "$b" "$cur" | wc -l | tr -d ' '); [ "$gone" -gt 0 ] && note="; $gone fixed — run --measure to lock the shrink"
  if [ -n "$new" ]; then say "$id" FAIL "new: $(echo "$new" | paste -sd ' ' -)"; else say "$id" PASS "$(wc -l < "$cur" | tr -d ' ') baselined, 0 new$note"; fi; }
# count ratchet: <id> <baseline name> <file of "<key> <n>" lines> [slack] — FAIL on a new key or n above baseline+slack; measure lowers n, never raises
ratchet_counts() { local id=$1 b=$BASE/$2.txt cur=$3 slack=${4:-0} over; sort -u "$cur" -o "$cur"
  if [ "$MODE" = --measure ]; then
    if [ -f "$b" ]; then awk 'FILENAME==ARGV[1]{base[$1]=$2; next} ($1 in base){print $1, ($2<base[$1] ? $2 : base[$1])}' "$b" "$cur" | sort -u > "$T/m" && mv "$T/m" "$b"; else cp "$cur" "$b"; fi
    say "$id" MEASURE "$(awk '{s+=$2} END{print s+0}' "$b") in $(wc -l < "$b" | tr -d ' ') keys"; return; fi
  [ -f "$b" ] || { say "$id" ERROR "baseline $b missing — cannot tell new from old"; return; }
  over=$(awk -v s="$slack" 'FILENAME==ARGV[1]{base[$1]=$2; next} !($1 in base){print $1" (new "$2")"; next} $2>base[$1]+s{print $1" ("base[$1]"->"$2")"}' "$b" "$cur")
  if [ -n "$over" ]; then say "$id" FAIL "$(echo "$over" | paste -sd ' ' -)"; else say "$id" PASS "$(awk '{s+=$2} END{print s+0}' "$cur") in $(wc -l < "$cur" | tr -d ' ') keys, none above baseline"; fi; }
# facade <name> <module> <pattern>: the primitive's call pattern in Python sources outside its façade, counted per file
facade() { local id=facade-$1 module=$2 pat=$3
  if gin "$T/raw" pys -nE "$pat"; then grep -v "^$module:" "$T/raw" | by_file > "$T/prim"; ratchet_counts "$id" "$id" "$T/prim"
  else say "$id" ERROR "grep could not read sources"; fi; }
# census <id> <lister> <grep args…>: a concept's rejected spellings, counted per file
census() { local id=$1 list=$2; shift 2
  if gin "$T/raw" "$list" "$@"; then by_file < "$T/raw" > "$T/sp"; ratchet_counts "$id" "$id" "$T/sp"
  else say "$id" ERROR "grep could not read files"; fi; }
# test_home <source>: X/Y.py and X/Y.sh are tested in tests/X/test_Y.py; a root file in tests/test_Y.py
test_home() { local d=${1%/*} stem=${1##*/}; stem=${stem%.*}; [ "$d" = "$1" ] && d=.
  if [ "$d" = . ]; then echo "tests/test_$stem.py"; else echo "tests/$d/test_$stem.py"; fi; }

# shellcheck disable=SC2046 # setup refused any path holding a space
wc -l $(src) /dev/null | awk '$2!="total" && $2!="/dev/null" && $1>400 {print $2" "$1}' > "$T/size"; ratchet_counts size-src ceiling-src "$T/size" 5
# shellcheck disable=SC2046 # setup refused any path holding a space
wc -l $(tests_py) /dev/null | awk '$2!="total" && $2!="/dev/null" && $1>600 {print $2" "$1}' > "$T/tsize"; ratchet_counts size-test ceiling-test "$T/tsize" 5
awk -F/ '{p=""; for(i=1;i<NF;i++){p=p (i>1?"/":"") $i; print p}}' "$T/all" | sort -u | grep -iE '(^|/)([^/]*util[^/]*|helpers|common|misc|shared)$' > "$T/neg"; ratchet negation-dirs negation-dirs "$T/neg"
src | while read -r f; do case $f in
  *.py) head -n 1 "$f" | grep -qE '^[rR]?"""' || echo "$f" ;;
  *.sh) sed -n 2p "$f" | grep -qE '^# [^ ]' || echo "$f" ;;
esac; done > "$T/purpose"; ratchet module-purpose no-purpose "$T/purpose"
src | grep -vE '(^|/)__init__\.py$' | while read -r f; do h=$(test_home "$f"); [ -e "$h" ] || echo "$f"; done > "$T/mirror"; ratchet test-mirror untested-sources "$T/mirror"
if gin "$T/raw" tests_py -nE '\btempfile\b|mkdtemp|mkstemp|TemporaryDirectory|NamedTemporaryFile'; then
  grep -v '^tests/jail\.py:' "$T/raw" | by_file > "$T/jail"; ratchet_counts test-jail test-jail "$T/jail"
else say test-jail ERROR "grep could not read tests"; fi
grep -E '(^|/)(run\.[^/]*|[^/]*\.sh)$' "$T/all" | grep -vE '^(scripts|service)/' > "$T/run"; ratchet stray-runners stray-runners "$T/run"

facade env gpt_gateway/env.py '\bos\.environ\b|\bgetenv\(|from os import .*environ'
facade clock gpt_gateway/clock.py '\btime\.(time|sleep|strftime|localtime|gmtime|monotonic|perf_counter)\(|^[[:space:]]*from time import'
facade log gpt_gateway/log.py '^[[:space:]]*(import|from) logging\b|\blogging\.|open\([^)]*["'"'"']a[b+]?["'"'"']'
facade settings gpt_gateway/settings.py '["'"'"']settings\.json["'"'"']'
facade upstream gpt_gateway/upstream.py '\bHTTPS?Connection\(|create_default_context\(|\bcreate_connection\('
facade capture gpt_gateway/capture.py '[1-5]-(request|response)(\.sent)?\.(json|body)'
facade db gpt_gateway/db.py '^[[:space:]]*(import|from) sqlite3\b|\bsqlite3\.'

census spelling-predecessor all -noiE 'adv[i]sor[-_ ]?(cache[-_ ]?)?gateway'
census spelling-old-name all -noiE 'cache[-_ ]?gateway'
# The former name survives on one line per file: the install migration's LEGACY_NAME and its test's.
# shellcheck disable=SC2329,SC2317 # called indirectly, as the lister gin names
migration() { all | grep -xE 'service/install\.sh|tests/service/test_install\.py'; }
if gin "$T/raw" not_design -noiE '(model|gpt)[^a-z0-9]?gateway' \
  && gin "$T/legacy" migration -nE '^LEGACY_NAME ?= ?"?model[-]gateway"?$'; then
  grep -vE ':(gpt-gateway|gpt_gateway|GPT_GATEWAY)$' "$T/raw" \
    | awk -F: 'NR==FNR{keep[$1":"$2]; next} !(($1":"$2) in keep)' "$T/legacy" - | by_file > "$T/sp"
  ratchet_counts spelling-product spelling-product "$T/sp"
else say spelling-product ERROR "grep could not read files"; fi
{ shs | while read -r f; do grep -oE '\$\{[A-Za-z_][A-Za-z0-9_]*:-' "$f" | sed -E 's/^\$\{//; s/:-$//'; done
  grep -E '^[A-Z_][A-Z0-9_]* = "[A-Z_][A-Z0-9_]*"$' gpt_gateway/env.py | sed -E 's/.*= "//; s/"$//'; } | sort -u > "$T/knobs"
if [ ! -s "$T/knobs" ]; then say spelling-env-prefix ERROR "no knob names parsed from service/*.sh or gpt_gateway/env.py"
# HOME and CODEX_HOME: read, never defined.
else grep -vE '^(GPT_GATEWAY_[A-Z0-9_]+|HOME|CODEX_HOME|XDG_CONFIG_HOME)$' "$T/knobs" > "$T/env"; ratchet spelling-env-prefix spelling-env-prefix "$T/env"; fi
census spelling-kind src -nE 'conv[=]'

if gin "$T/raw" src -nE '^def [A-Za-z_][A-Za-z0-9_]*|^[a-z_][a-z0-9_]*\(\) *\{'; then
  sed -E 's/^([^:]+):[0-9]+:(def )?([A-Za-z_][A-Za-z0-9_]*).*/\3 \1/' "$T/raw" | awk '{print tolower($1)" "$2}' \
    | grep -vE '^(main|on_request|on_event|on_response_end|health|cli|check) ' | sort -u \
    | awk '{f[$1]=f[$1]" "$2; c[$1]++} END{for(n in c) if(c[n]>1) print n":"f[n]}' > "$T/dup"; ratchet dup-functions dup-functions "$T/dup"
else say dup-functions ERROR "grep could not read sources"; fi

: > "$T/disp"; : > "$T/use"
shs | grep '^service/' | while read -r f; do s=$(basename "$f")
  grep -E '^[[:space:]]+[a-z-][a-z|-]*\)' "$f" | sed -E 's/^[[:space:]]+//; s/\).*//' | tr '|' '\n' | sed "s|^|$s:|" >> "$T/disp"
  grep -oE 'usage: [^"]*' "$f" | head -n 1 | sed 's/^usage: //' | tr -c 'A-Za-z0-9._-' '\n' | grep -v '^$' | sed "s|^|$s:|" >> "$T/use"
done
sort -u "$T/disp" -o "$T/disp"; sort -u "$T/use" -o "$T/use"
if [ ! -s "$T/disp" ]; then say registry-parity ERROR "no dispatched case arm parsed from service/*.sh"
elif [ ! -s "$T/use" ]; then say registry-parity ERROR "no usage line parsed from service/*.sh"
else comm -23 "$T/disp" "$T/use" > "$T/par"; ratchet registry-parity undocumented "$T/par"; fi

if [ ! -f README.md ]; then say orientation-truth ERROR "README.md missing"
else
  docs=$(echo README.md; grep -E '(^|/)CLAUDE\.md$' "$T/all")
  # shellcheck disable=SC2086,SC2016 # $docs is a newline list of space-free paths; the backticks are matched literally
  grep -ohE '`[^`]+`' $docs | tr -d '`' | awk '{print $1}' | sort -u > "$T/cited"
  if [ ! -s "$T/cited" ]; then say orientation-truth ERROR "no backticked citation parsed from $(echo "$docs" | tr "\n" " ")"
  else
    knob='^GPT_GATEWAY_[A-Z0-9_]+$' path='^[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_.-]+)*/?$'
    while read -r n; do
      if [[ $n =~ $knob ]]; then
        # shellcheck disable=SC2046 # setup refused any path holding a space
        grep -qwF "$n" $(src) || echo "$n"
      elif [[ $n =~ $path ]]; then
        case $n in
          */*) [ -e "$n" ] || echo "$n" ;;
          *.py|*.sh|*.md) [ -e "$n" ] || [ -e "service/$n" ] || echo "$n" ;;  # the installed copy is flat
        esac
      fi
    done < "$T/cited" > "$T/dang"; ratchet orientation-truth dangling "$T/dang"
  fi
fi
exit $rc
