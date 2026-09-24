"""Feed decision files through the runtime's DecisionInbox and ApprovalBroker.

Usage: python3 process_inbox.py <approvals_state> <event_log> <inbox_dir>

Prints JSON: per file results (in processing order) and the broker's view of
every request afterwards.
"""

import json
import sys

from openclaw_ecc_orchestrator.plugin.approvals import ApprovalBroker
from openclaw_ecc_orchestrator.plugin.events import JsonlEventSink
from openclaw_ecc_orchestrator.plugin.inbox import DecisionInbox


def main():
    state_path, event_log, inbox = sys.argv[1:4]
    broker = ApprovalBroker(state_path, emitter=JsonlEventSink(event_log))
    results = DecisionInbox(inbox, broker).process()
    st = broker.state()
    print(json.dumps({
        "results": [
            {"file": r.get("source_file"), "ok": r.get("ok"),
             "failed": [c["name"] for c in r.get("checks", []) if not c.get("ok")]}
            for r in results
        ],
        "requests": {rid: {"status": r["status"], "decided_by": r.get("decided_by")}
                     for rid, r in st["requests"].items()},
        "rejected_attempts": len(st.get("rejected_attempts", [])),
    }))


if __name__ == "__main__":
    main()
