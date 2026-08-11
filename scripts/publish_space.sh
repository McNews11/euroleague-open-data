#!/usr/bin/env bash
# Build and push the HuggingFace Space.
#
# The Space is a deployment artefact, not a second copy of the source. It is generated
# into build/space from the current tree, so refreshing it later is a rerun rather than a
# merge -- and the HF-specific README frontmatter never has to live in the GitHub README.
#
# Usage:  scripts/publish_space.sh <owner>/<space-name>
# Example: scripts/publish_space.sh deividas/euroleague-open-data

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="$REPO_ROOT/build/space"
TARGET="${1:-}"

if [[ -z "$TARGET" ]]; then
    echo "usage: $0 <owner>/<space-name>" >&2
    exit 2
fi

OWNER="${TARGET%%/*}"
SPACE="${TARGET##*/}"
REMOTE="https://huggingface.co/spaces/$OWNER/$SPACE"
PUBLIC_HOST="$(echo "$OWNER-$SPACE" | tr '[:upper:]' '[:lower:]').hf.space"

DB="$REPO_ROOT/data/euroleague.duckdb"

# Shipping an empty warehouse would produce a Space that builds, starts, reports
# unhealthy, and tells you nothing about why. Check before spending a build.
if [[ ! -f "$DB" ]]; then
    echo "error: $DB does not exist. Run the ETL first." >&2
    exit 1
fi
GAMES=$(python3 -c "
import duckdb, sys
con = duckdb.connect('$DB', read_only=True)
print(con.execute('SELECT count(*) FROM games').fetchone()[0])
" 2>/dev/null || echo 0)
if [[ "$GAMES" -lt 1 ]]; then
    echo "error: warehouse has no games. Refusing to publish an empty dataset." >&2
    exit 1
fi
echo "warehouse: $GAMES games, $(du -h "$DB" | cut -f1)"

rm -rf "$STAGE"
mkdir -p "$STAGE/data"

cp "$REPO_ROOT/pyproject.toml" "$REPO_ROOT/Dockerfile" "$STAGE/"
cp "$REPO_ROOT/LICENSE" "$REPO_ROOT/DISCLAIMER.md" "$STAGE/"
cp -R "$REPO_ROOT/src" "$REPO_ROOT/web" "$STAGE/"
cp "$DB" "$STAGE/data/euroleague.duckdb"

# The landing page advertises the endpoint, so it has to know its own hostname.
sed -i '' "s|YOUR-DEPLOYMENT|$PUBLIC_HOST|g" "$STAGE/web/index.html"

# HuggingFace reads Space configuration from README frontmatter.
{
    cat <<EOF
---
title: EuroLeague Open Data
emoji: 🏀
colorFrom: gray
colorTo: orange
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Unofficial EuroLeague/EuroCup MCP server and open dataset
---

EOF
    cat "$REPO_ROOT/README.md"
} > "$STAGE/README.md"

cat > "$STAGE/.gitattributes" <<'EOF'
data/euroleague.duckdb filter=lfs diff=lfs merge=lfs -text
EOF

cd "$STAGE"
if [[ ! -d .git ]]; then
    git init -q -b main
    git remote add origin "$REMOTE"
fi
git lfs install --local >/dev/null
git add -A
git commit -q -m "Deploy euroleague-open-data ($GAMES games)" || echo "nothing changed"

echo
echo "staged at $STAGE"
echo "remote:   $REMOTE"
echo "host:     $PUBLIC_HOST"
echo
echo "Push with:  git -C '$STAGE' push --force origin main"
echo "Then set PUBLIC_HOST=$PUBLIC_HOST in the Space's Settings -> Variables."
