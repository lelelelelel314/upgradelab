from __future__ import annotations

import html
import json
from dataclasses import asdict
from pathlib import Path

from .store import SQLiteRunStore


def write_html_report(store: SQLiteRunStore, run_id: str, output: str | Path) -> Path:
    run = store.get_run(run_id)
    events = store.events(run_id)
    checkpoint = store.latest_checkpoint(run_id)
    public_run = asdict(run)
    if public_run["task"].get("acceptance_command") is not None:
        public_run["task"]["acceptance_command"] = "[withheld]"
    public_checkpoint = asdict(checkpoint) if checkpoint else None
    if public_checkpoint is not None:
        acceptance = public_checkpoint["payload"].get("acceptance")
        if acceptance is not None:
            acceptance["stdout"] = "[withheld]"
            acceptance["stderr"] = "[withheld]"
    payload = {
        "run": public_run,
        "checkpoint": public_checkpoint,
        "events": events,
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    event_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(event['sequence']))}</td>"
        f"<td>{html.escape(event['type'])}</td>"
        f"<td><code>{html.escape(json.dumps(event['payload'], ensure_ascii=False))}</code></td>"
        "</tr>"
        for event in events
    )
    result = run.result or {}
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>UpgradeLab · {html.escape(run.id)}</title>
<style>
:root {{ color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
body {{ margin:0; background:#090d12; color:#dce7f3; }}
main {{ max-width:1080px; margin:auto; padding:48px 24px; }}
.eyebrow {{ color:#64d8cb; letter-spacing:.14em; text-transform:uppercase; font-size:12px; }}
h1 {{ font-size:clamp(32px,6vw,68px); margin:.2em 0; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:12px; }}
.card {{ background:#111923; border:1px solid #253242; border-radius:14px; padding:18px; }}
.label {{ color:#8494a7; font-size:12px; text-transform:uppercase; }}
.value {{ font-size:18px; margin-top:8px; overflow-wrap:anywhere; }}
table {{ width:100%; border-collapse:collapse; margin-top:24px; }}
th,td {{ border-bottom:1px solid #253242; padding:12px; text-align:left; vertical-align:top; }}
code {{ color:#a9e7df; white-space:pre-wrap; overflow-wrap:anywhere; }}
details {{ margin-top:28px; }} pre {{ overflow:auto; background:#05080c; padding:18px; border-radius:12px; }}
</style></head><body><main>
<div class="eyebrow">Evidence-based dependency repair</div>
<h1>UpgradeLab run</h1>
<section class="grid">
<div class="card"><div class="label">Status</div><div class="value">{html.escape(run.status.value)}</div></div>
<div class="card"><div class="label">Dependency</div><div class="value">{html.escape(run.task.target_dependency)} → {html.escape(run.task.target_version)}</div></div>
<div class="card"><div class="label">Visible tests</div><div class="value">exit {html.escape(str(result.get('test_exit_code', '—')))}</div></div>
<div class="card"><div class="label">Independent acceptance</div><div class="value">exit {html.escape(str(result.get('acceptance_exit_code', '—')))}</div></div>
</section>
<h2>Audit timeline</h2>
<table><thead><tr><th>#</th><th>Event</th><th>Evidence</th></tr></thead><tbody>{event_rows}</tbody></table>
<details><summary>Machine-readable evidence</summary><pre>{html.escape(serialized)}</pre></details>
</main></body></html>"""
    destination = Path(output).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(document, encoding="utf-8")
    return destination
