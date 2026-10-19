"""Panels and rotation (TEAM-DELIVERY-v2 sections 3.1 and 3.3).

Eligibility comes from the existing selector (`routing.selection.select_runner`
with role "review"): certified, unexpired, allowed by policy, serving the
capability class, permitted for the risk, not excluded. On top of that a
role's panel order and a persisted cursor decide who plays the role, with the
pack size, cost cap and share cap as extra filters.
"""

import json
import os

from ..runs.fsutil import atomic_write_json


class Rotation:
    def __init__(self, path):
        self.path = path
        self.state = {"cursors": {}, "turns": {}}
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    self.state["cursors"].update(loaded.get("cursors") or {})
                    self.state["turns"].update(loaded.get("turns") or {})
            except (OSError, ValueError):
                pass

    def _save(self):
        if self.path:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            atomic_write_json(self.path, self.state)

    def turns(self, role):
        return dict(self.state["turns"].get(role) or {})

    def choose(self, role, panel, candidates, *, exclude=(), pack_tokens=0, max_pack=None, cost_cap=None,
               share_cap=None, require_kind=None, kinds=None):
        """Pick a provider for `role`. `candidates` are select_runner
        candidates (dicts with runner, model, estimated_cost_usd). Returns
        (choice or None, skipped) where skipped lists (provider, reason)."""
        by_runner = {c["runner"]: c for c in candidates}
        start = int(self.state["cursors"].get(role, 0)) % len(panel) if panel else 0
        order = panel[start:] + panel[:start]
        skipped, eligible = [], []
        for provider in order:
            cand = by_runner.get(provider)
            if cand is None:
                skipped.append((provider, "not eligible for this role and class"))
            elif provider in exclude:
                skipped.append((provider, "excluded for independence"))
            elif require_kind and (kinds or {}).get(provider) != require_kind:
                skipped.append((provider, f"needs a {require_kind} runner"))
            elif max_pack and pack_tokens > int((max_pack or {}).get(provider, 10 ** 9)):
                skipped.append((provider, f"evidence pack too large ({pack_tokens} tokens)"))
            elif cost_cap is not None and cand.get("cost_source") == "catalog" \
                    and isinstance(cand.get("estimated_cost_usd"), (int, float)) \
                    and cand["estimated_cost_usd"] > float(cost_cap):
                # Only per-token prices (catalog) are dollars; configured
                # runner_costs for subscription CLIs are routing weights.
                skipped.append((provider, "over the per-turn cost cap"))
            else:
                eligible.append(cand)
        if share_cap and len({c["runner"] for c in eligible}) >= 3:
            counts = self.turns(role)
            total = sum(counts.values())
            under = [c for c in eligible if (counts.get(c["runner"], 0) + 1) / (total + 1) <= float(share_cap)]
            for c in eligible:
                if c not in under:
                    skipped.append((c["runner"], "share cap reached for this milestone"))
            eligible = under or eligible
        if not eligible:
            return None, skipped
        choice = eligible[0]
        self.state["cursors"][role] = (panel.index(choice["runner"]) + 1) % len(panel)
        role_turns = self.state["turns"].setdefault(role, {})
        role_turns[choice["runner"]] = role_turns.get(choice["runner"], 0) + 1
        self._save()
        return dict(choice), skipped
