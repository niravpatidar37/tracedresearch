"""Mutation check for the security controls and budgets.

Each mutant disables one guarantee; at least one test must fail (KILLED). Results -> results/mutation_check.txt
Run: .venv/Scripts/python.exe scripts/mutation_check.py            (security + budgets -> mutation_check.txt)
     .venv/Scripts/python.exe scripts/mutation_check.py deploy     (deploy controls -> mutation_check_deploy.txt)
     .venv/Scripts/python.exe scripts/mutation_check.py hardening  (prod hardening -> mutation_check_hardening.txt)
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PY = sys.executable
S = "src/research_assistant/"
MUTANTS = [
    (
        S + "fetch/ssrf.py",
        "SSRF: treat every IP as public",
        "    return bool(ip.is_global) and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)",
        "    return True",
    ),
    (S + "fetch/ssrf.py", "SSRF: allow any scheme", "    if scheme not in ALLOWED_SCHEMES:", "    if False:"),
    (S + "fetch/ssrf.py", "SSRF: allow any port", "    if port not in ALLOWED_PORTS:", "    if False:"),
    (
        S + "fetch/fetcher.py",
        "SSRF: skip IP check (no DNS pinning validation)",
        "                ip = resolve_public(target.host, target.port, self.resolver)",
        "                ip = self.resolver(target.host, target.port)[0][4][0]",
    ),
    (
        S + "fetch/fetcher.py",
        "fetch: drop content-type allowlist",
        "            if ctype not in ALLOWED_CONTENT_TYPES:",
        "            if False:",
    ),
    (
        S + "fetch/fetcher.py",
        "fetch: drop streamed size cap",
        "                if len(buf) > self.max_bytes:",
        "                if False:",
    ),
    (S + "report.py", "render: keep URLs in model text", '    t = _URLISH.sub("(link removed)", t)', "    t = t"),
    (
        S + "report.py",
        "render: allow raw HTML",
        '    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # no raw HTML can survive',
        "    t = t",
    ),
    (
        S + "report.py",
        "render: keep paragraphs without valid citations",
        "            if not m:\n                dropped += 1\n                continue",
        "            pass",
    ),
    (S + "report.py", "validator: ignore undefined footnotes", "            and n in defined\n", ""),
    (
        S + "agents/researcher.py",
        "researcher: trust model snippet (no verbatim check)",
        "        verbatim = find_verbatim(ef.snippet[:600], passage)",
        "        verbatim = ef.snippet[:600]",
    ),
    (
        S + "text.py",
        "prompt: do not neutralise forged delimiters",
        '    return _DELIM.sub(lambda m: "‹‹‹" if m.group(1) == "<<<" else "›››", strip_controls(text))',
        "    return strip_controls(text)",
    ),
    (
        S + "agents/supervisor.py",
        "budget: unlimited send-backs",
        "            sq.send_backs < b.max_send_backs_per_subq\n",
        "            True\n",
    ),
    (S + "agents/planner.py", "budget: no sub-question cap", "    subs = subs[:max_sub_questions]\n", ""),
    (
        S + "agents/researcher.py",
        "budget: no per-subq search cap",
        "        if sq.searches_used >= b.max_searches_per_subq:",
        "        if False:",
    ),
    (
        S + "runtime.py",
        "budget: no token guard",
        "        if self.tokens_used + est_in + max_tokens > token_limit:",
        "        if False:",
    ),
    (
        S + "runtime.py",
        "budget: no run USD guard",
        "        if self.usd_spent + est_usd > self.budgets.max_run_usd:",
        "        if False:",
    ),
    (
        S + "spend.py",
        "ledger: no project cap",
        "    if p + estimate_usd > project_cap:",
        "    if False:",
    ),
    (
        S + "graph.py",
        "durability: no idempotent node cache",
        "            if cached is not None:",
        "            if False:",
    ),
    (
        S + "api.py",
        "api: no ownership check",
        "        if st is None or not hmac.compare_digest(st.owner, kid):",
        "        if st is None:",
    ),
    (
        S + "api.py",
        "api: no rate limit",
        "        if not svc.store.hit_rate_limit(bucket, window, limit):",
        "        if False:",
    ),
]


DEPLOY_MUTANTS = [
    (
        S + "runtime.py",
        "kill switch/drain: no preempt check before LLM/tool calls",
        "        if reason:\n            raise Preempted(reason)\n        if self.clock() > self.deadline:",
        "        if self.clock() > self.deadline:",
    ),
    (
        S + "graph.py",
        "kill switch/drain: no preempt check at node boundary",
        "            if reason:  # node boundary: everything up to here is committed\n                raise Preempted(reason)",
        "            pass",
    ),
    (
        S + "graph.py",
        "drain: preempted run not requeued",
        "            self.store.requeue(run_id)\n",
        "            pass\n",
    ),
    (
        S + "api.py",
        "kill switch: POST still accepted",
        "        if svc.kill_switch():\n            raise HTTPException(503",
        "        if False:\n            raise HTTPException(503",
    ),
    (
        S + "api.py",
        "kill switch: worker keeps consuming the queue",
        "                if self.kill_switch() or not self.has_capacity():",
        "                if not self.has_capacity():",
    ),
    (
        S + "api.py",
        "shutdown: unstarted runs not handed back",
        "            if self.uses_queue:\n                self.store.requeue(rid)",
        "            pass",
    ),
    (
        S + "api.py",
        "readyz: ignore schema version",
        "        if version < migrations.REQUIRED_VERSION:",
        "        if False:",
    ),
    (
        S + "api.py",
        "readyz: ready while draining",
        '        if svc.draining.is_set():\n            raise HTTPException(503, "draining")',
        "        pass",
    ),
    (
        S + "api.py",
        "resume loop: steals queued runs before the grace period",
        '                if st.status == "queued" and age < self.settings.orphan_grace_s:',
        "                if False:",
    ),
    (
        S + "migrations/__init__.py",
        "migrations: no lock",
        "    if not r.set(LOCK_KEY, str(time.time()), nx=True, ex=lock_ttl_s):",
        "    if False:",
    ),
    (
        S + "logging_utils.py",
        "logs: exception text not redacted",
        '            entry["exception"] = redact(self.formatException(record.exc_info))',
        '            entry["exception"] = self.formatException(record.exc_info)',
    ),
    (
        S + "store.py",
        "redis: TLS server cert not verified",
        '        kwargs["ssl_cert_reqs"] = "required"',
        '        kwargs["ssl_cert_reqs"] = "none"',
    ),
    (
        S + "spend.py",
        "redis ledger: per-instance (not shared) totals",
        "        g, p = cast(list[str | None], self.client.mget(list(self._keys())))",
        "        g, p = None, None",
    ),
]

HARDENING_MUTANTS = [
    (
        S + "config.py",
        "fake LLM: Settings accepts LLM_PROVIDER=fake in prod",
        '        if self.llm_provider == "fake" and self.env not in FAKE_LLM_ENVS:',
        "        if False:",
    ),
    (
        S + "llm/factory.py",
        "fake LLM: make_llm skips the ENV re-check",
        "        if settings.env not in FAKE_LLM_ENVS:",
        "        if False:",
    ),
    (
        S + "search/providers.py",
        "fixture fetch: skip the SSRF guard",
        "            resolve_public(target.host, target.port, fixture_resolver)",
        "            pass",
    ),
    (S + "http_security.py", "body limit: ignore Content-Length", "            if too_big:", "            if False:"),
    (
        S + "http_security.py",
        "body limit: no cap on chunked bodies",
        "            if size > self.max_bytes:",
        "            if False:",
    ),
    (
        S + "http_security.py",
        "headers: security headers not added",
        '                message["headers"] = headers',
        "                pass",
    ),
    (S + "api.py", "errors: no JSON 500 handler", "    @app.exception_handler(Exception)\n", ""),
    (S + "report.py", "degradation: fallback not labelled", "        fallback_reason=reason_code(reason),\n", ""),
    (
        S + "scaler.py",
        "scaler: scale in without the one-step limit",
        "        target = max(target, current - 1)",
        "        pass",
    ),
    (
        S + "scaler.py",
        "scaler: scales up while the kill switch is on",
        "    if kill_switch and want > current:",
        "    if False:",
    ),
]


def main() -> int:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VIRTUAL_ENV", "PYTHONPATH", "UV_"))}
    which = sys.argv[1] if len(sys.argv) > 1 else "security"
    mutants = {"security": MUTANTS, "deploy": DEPLOY_MUTANTS, "hardening": HARDENING_MUTANTS}[which]
    rows = []
    for rel, label, old, new in mutants:
        p = ROOT / rel
        orig = p.read_text(encoding="utf-8")
        assert orig.count(old) == 1, f"{label}: old text found {orig.count(old)} times"
        p.write_text(orig.replace(old, new), encoding="utf-8", newline="\n")
        try:
            r = subprocess.run(
                [PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                env=env,
                timeout=600,
            )
            out = r.stdout + r.stderr
            failed = [ln for ln in out.splitlines() if ln.startswith("FAILED")]
            status = "KILLED" if r.returncode and failed else ("ERROR" if r.returncode else "SURVIVED")
            rows.append(f"{status:8} {label} :: {failed[0][7:110] if failed else out.strip().splitlines()[-1]}")
            print(rows[-1], flush=True)
        finally:
            p.write_text(orig, encoding="utf-8", newline="\n")
    killed = sum(r.startswith("KILLED") for r in rows)
    summary = f"{killed}/{len(rows)} mutants killed"
    out_name = {"security": "mutation_check.txt"}.get(which, f"mutation_check_{which}.txt")
    (ROOT / "results" / out_name).write_text("\n".join([*rows, summary]) + "\n", encoding="utf-8")
    print(summary)
    return 0 if killed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
