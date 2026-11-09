#!/usr/bin/env bash
# Keep the status board's probe history on its own branch (status-data), so
# main's history isn't filled with a commit every two hours.
#
#   scripts/status-data.sh checkout DIR   # DIR = worktree of status-data (created if missing)
#   scripts/status-data.sh save DIR       # commit DIR/probes.jsonl and push it
set -euo pipefail

cmd=${1:?usage: status-data.sh checkout|save DIR}
dir=${2:?usage: status-data.sh checkout|save DIR}
branch=${STATUS_BRANCH:-status-data}

case "$cmd" in
  checkout)
    if git fetch --quiet origin "$branch" 2>/dev/null; then
      git worktree add --quiet --force --detach "$dir" FETCH_HEAD
    else
      echo "no $branch branch yet; starting an empty history"
      git worktree add --quiet --force --detach "$dir"
      git -C "$dir" checkout --quiet --orphan "$branch"
      git -C "$dir" rm -rf --quiet .
    fi
    touch "$dir/probes.jsonl"
    ;;
  save)
    git -C "$dir" add probes.jsonl
    if git -C "$dir" diff --cached --quiet; then
      echo "no new probes to save"
      exit 0
    fi
    git -C "$dir" \
      -c user.name="llmgate-status" \
      -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
      commit --quiet -m "probe $(date -u +%Y-%m-%dT%H:%MZ)"
    git -C "$dir" push --quiet origin "HEAD:refs/heads/$branch"
    echo "saved $(wc -l < "$dir/probes.jsonl") probes to $branch"
    ;;
  *)
    echo "unknown command: $cmd" >&2
    exit 2
    ;;
esac
