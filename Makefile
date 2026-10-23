# The checks GitHub runs on a pull request to main (.github/workflows/release.yml),
# runnable here first: `make release-check` runs them all.
.PHONY: release-check check release

release-check: check release

check:
	bash scripts/gate.sh

# VERSION must be a release version X.Y.Z past main's: main's VERSION, else its latest v* tag.
release:
	git rev-parse --verify --quiet origin/main >/dev/null || { echo "release: origin/main not found; fetch it first" >&2; exit 1; }
	v=$$(cat VERSION) || exit 1; \
	echo "$$v" | grep -Eqx '[0-9]+\.[0-9]+\.[0-9]+' || { echo "release: VERSION $$v is not a release version X.Y.Z" >&2; exit 1; }; \
	base=$$(git show origin/main:VERSION 2>/dev/null) || base=$$(git describe --tags --abbrev=0 --match 'v[0-9]*' origin/main | sed 's/^v//'); \
	[ -n "$$base" ] || { echo "release: no VERSION and no v* tag on origin/main" >&2; exit 1; }; \
	[ "$$v" != "$$base" ] && [ "$$(printf '%s\n%s\n' "$$base" "$$v" | sort -V | tail -n 1)" = "$$v" ] \
	  || { echo "release: VERSION $$v does not move past main's $$base" >&2; exit 1; }; \
	echo "release: VERSION $$v moves past main's $$base"
