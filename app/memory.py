"""Account memory and call history (SQLite). What happened last time is injected before the next dial."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "coach.db"
SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, company TEXT NOT NULL, industry TEXT, dm_name TEXT, dm_first TEXT, gk_first TEXT,
  phone TEXT, email TEXT, renewal_month TEXT, notes TEXT, created REAL);
CREATE TABLE IF NOT EXISTS brokers (id TEXT PRIMARY KEY, name TEXT, first TEXT, agency TEXT);
CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, account_id TEXT, broker_id TEXT, started REAL, ended REAL, outcome TEXT, summary TEXT,
  debrief TEXT, synthetic INTEGER DEFAULT 0, mode TEXT);
CREATE TABLE IF NOT EXISTS turns (call_id TEXT, seq INTEGER, speaker TEXT, text TEXT, t0 REAL, t1 REAL);
CREATE TABLE IF NOT EXISTS cues (call_id TEXT, seq INTEGER, situation TEXT, line_id TEXT, text TEXT, kind TEXT, source TEXT, latency_ms REAL, t REAL, turn_seq INTEGER);
CREATE TABLE IF NOT EXISTS objection_candidates (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, call_id TEXT, seen INTEGER DEFAULT 1, status TEXT DEFAULT 'unreviewed');
"""


class Memory:
    def __init__(self, path: Path | str = DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ---- accounts / brokers ----------------------------------------------------------------
    def upsert_account(self, **a: Any) -> str:
        a.setdefault("id", a.get("company", "acct").lower().replace(" ", "-")[:40])
        if not a.get("dm_first") and a.get("dm_name"):
            a["dm_first"] = a["dm_name"].split()[0]
        cols = ["id", "company", "industry", "dm_name", "dm_first", "gk_first", "phone", "email", "renewal_month", "notes"]
        row = [a.get(c) for c in cols]
        self.db.execute(f"INSERT INTO accounts ({','.join(cols)},created) VALUES ({','.join('?'*len(cols))},?) ON CONFLICT(id) DO UPDATE SET "
                        + ",".join(f"{c}=COALESCE(excluded.{c},accounts.{c})" for c in cols[1:]), row + [time.time()])
        self.db.commit()
        return a["id"]

    def update_account_facts(self, account_id: str, facts: dict) -> None:
        allowed = {"dm_first", "gk_first", "email", "renewal_month", "industry", "dm_name"}
        for k, v in facts.items():
            if k in allowed and v:
                self.db.execute(f"UPDATE accounts SET {k}=? WHERE id=? AND ({k} IS NULL OR {k}='')", (v, account_id))
        self.db.commit()

    def upsert_broker(self, id: str, name: str, agency: str) -> str:
        self.db.execute("INSERT INTO brokers (id,name,first,agency) VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, first=excluded.first, agency=excluded.agency",
                        (id, name, name.split()[0], agency))
        self.db.commit()
        return id

    def account(self, account_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return dict(r) if r else None

    def accounts(self) -> list[dict]:
        rows = self.db.execute("SELECT a.*, (SELECT COUNT(*) FROM calls c WHERE c.account_id=a.id AND c.ended IS NOT NULL) AS n_calls,"
                               " (SELECT outcome FROM calls c WHERE c.account_id=a.id AND c.ended IS NOT NULL ORDER BY ended DESC LIMIT 1) AS last_outcome"
                               " FROM accounts a ORDER BY company").fetchall()
        return [dict(r) for r in rows]

    def brokers(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM brokers ORDER BY name").fetchall()]

    def broker(self, broker_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM brokers WHERE id=?", (broker_id,)).fetchone()
        return dict(r) if r else None

    # ---- calls ------------------------------------------------------------------------------
    def start_call(self, account_id: str, broker_id: str, mode: str, synthetic: bool = False, call_id: str | None = None) -> str:
        call_id = call_id or "call-" + uuid.uuid4().hex[:8]
        self.db.execute("INSERT INTO calls (id,account_id,broker_id,started,synthetic,mode) VALUES (?,?,?,?,?,?)", (call_id, account_id, broker_id, time.time(), int(synthetic), mode))
        self.db.commit()
        return call_id

    def add_turn(self, call_id: str, seq: int, speaker: str, text: str, t0: float = 0.0, t1: float = 0.0) -> None:
        self.db.execute("INSERT INTO turns VALUES (?,?,?,?,?,?)", (call_id, seq, speaker, text, t0, t1))
        self.db.commit()

    def add_cue(self, call_id: str, cue: dict) -> None:
        self.db.execute("INSERT INTO cues VALUES (?,?,?,?,?,?,?,?,?,?)", (call_id, cue["seq"], cue["situation"], cue["line_id"], cue["text"], cue["kind"], cue["source"], cue["latency_ms"], cue["t"], cue.get("turn_seq")))
        self.db.commit()

    def end_call(self, call_id: str, outcome: str, summary: str, debrief: dict, ended: float | None = None) -> None:
        self.db.execute("UPDATE calls SET ended=?, outcome=?, summary=?, debrief=? WHERE id=?", (ended or time.time(), outcome, summary, json.dumps(debrief), call_id))
        for cand in debrief.get("new_objection_candidates") or []:
            if isinstance(cand, str) and cand.strip():
                row = self.db.execute("SELECT id, seen FROM objection_candidates WHERE lower(text)=lower(?)", (cand.strip(),)).fetchone()
                if row:
                    self.db.execute("UPDATE objection_candidates SET seen=seen+1 WHERE id=?", (row["id"],))
                else:
                    self.db.execute("INSERT INTO objection_candidates (text, call_id) VALUES (?,?)", (cand.strip(), call_id))
        self.db.commit()

    def call(self, call_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["debrief"] = json.loads(d["debrief"]) if d.get("debrief") else None
        d["turns"] = [dict(x) for x in self.db.execute("SELECT * FROM turns WHERE call_id=? ORDER BY seq", (call_id,)).fetchall()]
        d["cues"] = [dict(x) for x in self.db.execute("SELECT * FROM cues WHERE call_id=? ORDER BY seq", (call_id,)).fetchall()]
        return d

    def calls(self, account_id: str | None = None, limit: int = 50) -> list[dict]:
        q = "SELECT c.*, a.company, b.name AS broker_name FROM calls c LEFT JOIN accounts a ON a.id=c.account_id LEFT JOIN brokers b ON b.id=c.broker_id WHERE c.ended IS NOT NULL"
        args: list[Any] = []
        if account_id:
            q += " AND c.account_id=?"
            args.append(account_id)
        q += " ORDER BY c.ended DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in self.db.execute(q, args).fetchall():
            d = dict(r)
            d["debrief"] = json.loads(d["debrief"]) if d.get("debrief") else None
            out.append(d)
        return out

    # ---- what the coach needs before the dial ------------------------------------------------
    def brief(self, account_id: str) -> dict[str, Any]:
        acct = self.account(account_id) or {}
        past = [c for c in self.calls(account_id, limit=8) if c.get("outcome") != "aborted"][:5]
        avoid: set[str] = set()
        lines = []
        for c in past:
            d = c.get("debrief") or {}
            avoid.update(d.get("failed_lines") or [])
            when = datetime.fromtimestamp(c["ended"]).strftime("%b %-d")
            lines.append(f"{when}: {c.get('summary') or c.get('outcome')}")
        facts = {k: acct.get(k) for k in ("company", "dm_first", "gk_first", "renewal_month", "industry", "email") if acct.get(k)}
        advice = []
        if past:
            last = past[0].get("debrief") or {}
            if last.get("next_time"):
                nt = str(last["next_time"]).strip()
                advice.append(nt if len(nt) <= 140 else nt[:137].rsplit(" ", 1)[0] + "…")
            elif past[0].get("outcome") == "gatekeeper_block":
                advice.append(f"Ask for {facts['dm_first']} by name; don't accept 'send an email'." if facts.get("dm_first") else "Get the decision maker's name before anything else.")
            elif past[0].get("outcome") in ("objection_unresolved", "no_outcome"):
                advice.append("Tie the ask to the renewal date; offer two times.")
        text = " · ".join(lines[:2] + advice[:1]) if past else "First contact — no history."
        return {"facts": facts, "avoid": sorted(avoid), "history": past, "text": text, "n_calls": len(past)}

    # ---- learning across calls ------------------------------------------------------------------
    def insights(self) -> dict[str, Any]:
        calls = [c for c in self.calls(limit=1000) if c.get("outcome") != "aborted"]
        n = len(calls)
        booked = {"meeting_booked", "meeting_soft_yes"}
        by_outcome = {}
        for c in calls:
            by_outcome[c["outcome"]] = by_outcome.get(c["outcome"], 0) + 1
        # openers: first 'say' cue of each call vs outcome
        openers: dict[str, dict] = {}
        objections: dict[str, dict] = {}
        fail_stage: dict[str, int] = {}
        segments: dict[str, dict] = {}
        brokers: dict[str, dict] = {}
        for c in calls:
            d = c.get("debrief") or {}
            cues = self.db.execute("SELECT line_id, situation, kind FROM cues WHERE call_id=? ORDER BY seq", (c["id"],)).fetchall()
            first = next((x for x in cues if x["kind"] == "say"), None)
            ok = c["outcome"] in booked
            if first:
                o = openers.setdefault(first["line_id"], {"n": 0, "booked": 0})
                o["n"] += 1; o["booked"] += int(ok)
            for x in cues:
                if x["situation"].startswith("obj_") or x["situation"].startswith("gk_"):
                    o = objections.setdefault(x["line_id"], {"situation": x["situation"], "n": 0, "worked": 0})
                    o["n"] += 1
                    o["worked"] += int(x["line_id"] in (d.get("worked_lines") or []))
            if d.get("failed_stage"):
                fail_stage[d["failed_stage"]] = fail_stage.get(d["failed_stage"], 0) + 1
            acct = self.account(c["account_id"]) or {}
            seg = acct.get("industry") or "unknown"
            s = segments.setdefault(seg, {"n": 0, "booked": 0}); s["n"] += 1; s["booked"] += int(ok)
            b = brokers.setdefault(c.get("broker_name") or c.get("broker_id") or "?", {"n": 0, "booked": 0, "talk": 0.0, "fillers": 0, "asked": 0})
            b["n"] += 1; b["booked"] += int(ok); b["talk"] += float(d.get("talk_ratio") or 0); b["fillers"] += int(d.get("fillers") or 0)
            b["asked"] += int(bool(d.get("meeting_asked", c["outcome"] in booked or d.get("stage_reached") == "close")))
        for b in brokers.values():
            b["talk_ratio"] = round(b["talk"] / b["n"], 2) if b["n"] else 0
            b["fillers_per_call"] = round(b["fillers"] / b["n"], 1) if b["n"] else 0
            b["book_rate"] = round(b["booked"] / b["n"], 2) if b["n"] else 0
            b["ask_rate"] = round(b["asked"] / b["n"], 2) if b["n"] else 0
        rate = lambda d: sorted(({"key": k, **v, "rate": round(v.get("booked", v.get("worked", 0)) / v["n"], 2)} for k, v in d.items()), key=lambda x: (-x["rate"], -x["n"]))
        cands = [dict(r) for r in self.db.execute("SELECT * FROM objection_candidates ORDER BY seen DESC, id DESC LIMIT 20").fetchall()]
        synthetic = sum(1 for c in calls if c.get("synthetic"))
        return {"n_calls": n, "synthetic_calls": synthetic, "outcomes": by_outcome, "openers": rate(openers), "objection_lines": rate(objections),
                "fail_stage": fail_stage, "segments": rate(segments), "brokers": brokers, "objection_candidates": cands}
