#!/usr/bin/env python3
"""An OpenAI-style /v1/chat/completions endpoint that answers with `claude -p` (the Claude Code subscription).

The Round 4 kit calls its model only over HTTP (POST <base>/chat/completions) and reads the answer as text.
`claude -p` is a command, not an endpoint, so this small server sits between them. It is infrastructure, not one of
the agent's tools: the agent never sees it, and agent.json points at it with

    "llm": {"base": "http://127.0.0.1:8790/v1", "model": "<claude model id or alias>", "key_env": "CLAUDE_SHIM_KEY"}

    python claude_shim.py --dir submissions/25520014/round-4 [--port 8790] [--claude ~/.local/bin/claude]

- Listens on 127.0.0.1 only. The key is not checked (the kit sends one; any value works).
- Each request is one fresh `claude -p` call: system messages become --system-prompt, the rest becomes one
  transcript on stdin. Claude Code's own tools are off (--tools ""), and claude runs in an empty directory so no
  CLAUDE.md is picked up.
- Every call is logged to logs/claude-shim.jsonl (seconds, tokens, num_turns, error).
- A usage-limit answer starts a cooldown: calls get 503 at once instead of starting claude again.
- One phrase of the kit's system prompt is reworded on the way (REWORD). The kit's answer format line
  {"thought": "<your reasoning>", ...} is refused by the Claude safeguards for both Sonnet 5.5 and Opus 5.5
  (tested 2026-11-21: the two-line format header alone is refused; the rest of the prompt passes). Asking for
  "why this move, one short sentence" passes. The field is still called thought, so the kit parses and logs it as usual.
"""
import argparse
import json
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FLAGS = ["--output-format", "json", "--tools", "", "--exclude-dynamic-system-prompt-sections",
         "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
CALL_TIMEOUT = 170          # the kit gives up on a model call at 180 s
COOLDOWN = 600              # seconds of 503 after a usage-limit answer
LIMIT_WORDS = ("usage limit", "rate limit", "limit reached", "too many requests")
REWORD = {'"thought": "<your reasoning>"': '"thought": "<why this move, one short sentence>"'}


def transcript(messages):
    """System text, and the rest of the conversation as one prompt."""
    system = "\n\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")
    for old, new in REWORD.items():
        system = system.replace(old, new)
    rest = [m for m in messages if m.get("role") != "system"]
    if len(rest) == 1 and rest[0].get("role") == "user":
        return system, str(rest[0].get("content") or "")
    parts = [f"[{m.get('role')}]\n{m.get('content') or ''}" for m in rest]
    parts.append("(Write only your next message as the assistant.)")
    return system, "\n\n".join(parts)


class Shim:
    def __init__(self, root, claude):
        self.root, self.claude = Path(root), claude
        self.workdir = tempfile.mkdtemp(prefix="claude-shim-")
        self.cool_until = 0.0
        self.lock = threading.Lock()
        (self.root / "logs").mkdir(parents=True, exist_ok=True)

    def log(self, row):
        with self.lock, open(self.root / "logs" / "claude-shim.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(row, ts=int(time.time())), ensure_ascii=False) + "\n")

    def complete(self, body):
        """(status, payload) for one chat completion request."""
        if time.time() < self.cool_until:
            return 503, {"error": {"message": f"claude usage limit; cooling down for {int(self.cool_until - time.time())} s"}}
        model = str(body.get("model") or "")
        system, prompt = transcript(body.get("messages") or [])
        cmd = [self.claude, "-p"] + FLAGS + (["--system-prompt", system] if system else []) \
            + (["--model", model] if model else [])
        t0 = time.time()
        row = {"model": model, "prompt_chars": len(prompt) + len(system)}
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=CALL_TIMEOUT,
                                  cwd=self.workdir)
            out = json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else None
            if out is None or out.get("is_error") or proc.returncode != 0:
                why = str((out or {}).get("result") or proc.stderr or proc.stdout)[:300]
                if any(w in why.lower() for w in LIMIT_WORDS):
                    self.cool_until = time.time() + COOLDOWN
                raise RuntimeError(f"claude -p exited {proc.returncode}: {why}")
        except (subprocess.TimeoutExpired, RuntimeError, ValueError, OSError) as e:
            self.log(dict(row, secs=round(time.time() - t0, 1), error=f"{type(e).__name__}: {e}"[:300]))
            return 502, {"error": {"message": f"{type(e).__name__}: {e}"[:500]}}
        u = out.get("usage") or {}
        tokens_in = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
        self.log(dict(row, secs=round(time.time() - t0, 1), tokens_in=tokens_in, tokens_out=u.get("output_tokens", 0),
                      num_turns=out.get("num_turns"), cost_usd=out.get("total_cost_usd")))
        return 200, {"id": f"shim-{int(t0 * 1000)}", "object": "chat.completion", "created": int(t0), "model": model,
                     "choices": [{"index": 0, "finish_reason": "stop",
                                  "message": {"role": "assistant", "content": out.get("result") or ""}}],
                     "usage": {"prompt_tokens": tokens_in, "completion_tokens": u.get("output_tokens", 0)}}


def handler(shim):
    class H(BaseHTTPRequestHandler):
        def send(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.send(200, {"ok": True, "cooling_down": time.time() < shim.cool_until})

        def do_POST(self):
            if not self.path.rstrip("/").endswith("/chat/completions"):
                return self.send(404, {"error": {"message": "only POST /v1/chat/completions"}})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
            except ValueError:
                return self.send(400, {"error": {"message": "the body is not JSON"}})
            self.send(*shim.complete(body))

        def log_message(self, *a):          # calls are logged to claude-shim.jsonl instead
            pass
    return H


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dir", default=".")
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--claude", default="claude")
    a = p.parse_args()
    shim = Shim(a.dir, str(Path(a.claude).expanduser()))
    print(f"[shim] http://127.0.0.1:{a.port}/v1 -> {a.claude} -p", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), handler(shim)).serve_forever()


if __name__ == "__main__":
    main()
