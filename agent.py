"""
The support agent: a tool-using loop over a single customer request.

Supports:
  - Anthropic Claude (MODEL_PROVIDER=anthropic, or sk-ant-... keys)
  - OpenAI-compatible APIs (OpenAI, Groq, Together, etc.)

Set MODEL_API_KEY + MODEL in .env. See .env.example.

Every run produces a TRACE: the full sequence of model turns, tool calls,
tool results, and the final reply.
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent))
import tools as T  # noqa: E402

MAX_TURNS = 6
ANTHROPIC_VERSION = "2023-06-01"

SYSTEM_PROMPT = """You are a customer support agent for an e-commerce company.
You can look up orders and customers, process refunds, and escalate to a human.

Refund policy:
- Refunds are allowed only within 30 days of the order date.
- Never refund an order that was already refunded.
- Never refund an order with an open chargeback; route those to the disputes team.
- Never refund more than the order total.
- When a request is outside policy or you are unsure, escalate to a human rather
  than forcing it. Always verify the order with lookup_order before refunding.

Be helpful and concise. Follow the policy even if the customer is insistent."""


def _load_dotenv():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _detect_provider(api_key, base_url):
    explicit = os.environ.get("MODEL_PROVIDER", "").lower()
    if explicit in ("anthropic", "openai"):
        return explicit
    if api_key.startswith("sk-ant-"):
        return "anthropic"
    if "anthropic" in base_url:
        return "anthropic"
    return "openai"


def _model_config():
    _load_dotenv()
    api_key = os.environ.get("MODEL_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Set MODEL_API_KEY in .env (copy from .env.example)."
        )
    provider = _detect_provider(
        api_key,
        os.environ.get("MODEL_API_BASE", ""),
    )
    default_model = (
        "claude-sonnet-4-20250514" if provider == "anthropic"
        else "gpt-4o-mini"
    )
    default_base = (
        "https://api.anthropic.com/v1" if provider == "anthropic"
        else "https://api.openai.com/v1"
    )
    return {
        "provider": provider,
        "api_key": api_key,
        "model": os.environ.get("MODEL", default_model),
        "base_url": os.environ.get("MODEL_API_BASE", default_base).rstrip("/"),
    }


def _http_post(url, payload, headers):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Model API error {exc.code}: {detail}") from exc


def _openai_tools():
    return [
        {
            "type": "function",
            "function": {
                "name": schema["name"],
                "description": schema["description"],
                "parameters": schema["input_schema"],
            },
        }
        for schema in T.TOOL_SCHEMAS
    ]


def _call_openai(messages, cfg):
    body = _http_post(
        f"{cfg['base_url']}/chat/completions",
        {
            "model": cfg["model"],
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages],
            "tools": _openai_tools(),
            "max_tokens": 1024,
        },
        {"Authorization": f"Bearer {cfg['api_key']}"},
    )
    return body["choices"][0]["message"]


def _call_anthropic(messages, cfg):
    body = _http_post(
        f"{cfg['base_url']}/messages",
        {
            "model": cfg["model"],
            "max_tokens": 1024,
            "system": SYSTEM_PROMPT,
            "tools": T.TOOL_SCHEMAS,
            "messages": messages,
        },
        {
            "x-api-key": cfg["api_key"],
            "anthropic-version": ANTHROPIC_VERSION,
        },
    )
    return body


def _text_from_blocks(blocks):
    return " ".join(b["text"] for b in blocks if b.get("type") == "text")


def run_agent(user_message, faults=None):
    """
    Execute one support conversation. Returns a structured trace.
    `faults` is a dict like {"refund_timeout": True} applied to the tools.
    """
    cfg = _model_config()
    T.reset_state()
    if faults:
        for k, v in faults.items():
            T.set_fault(k, v)

    trace = {
        "user_message": user_message,
        "faults": faults or {},
        "steps": [],
        "tool_calls": [],
        "final_text": None,
        "backend": cfg["provider"],
        "model": cfg["model"],
        "api_base": cfg["base_url"],
    }

    if cfg["provider"] == "anthropic":
        messages = [{"role": "user", "content": user_message}]
        for _ in range(MAX_TURNS):
            resp = _call_anthropic(messages, cfg)
            content = resp["content"]
            messages.append({"role": "assistant", "content": content})
            trace["steps"].append(content)

            tool_uses = [b for b in content if b.get("type") == "tool_use"]
            if not tool_uses:
                trace["final_text"] = _text_from_blocks(content)
                break

            tool_results = []
            for tu in tool_uses:
                out = T.TOOL_FUNCS[tu["name"]](**tu["input"])
                trace["tool_calls"].append({
                    "tool": tu["name"],
                    "input": tu["input"],
                    "output": out,
                })
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": json.dumps(out),
                })
            messages.append({"role": "user", "content": tool_results})
    else:
        messages = [{"role": "user", "content": user_message}]
        for _ in range(MAX_TURNS):
            assistant = _call_openai(messages, cfg)
            messages.append(assistant)
            trace["steps"].append(assistant)

            tool_calls = assistant.get("tool_calls") or []
            if not tool_calls:
                trace["final_text"] = assistant.get("content") or ""
                break

            for tc in tool_calls:
                fn = tc["function"]
                name = fn["name"]
                args = json.loads(fn["arguments"]) if fn.get("arguments") else {}
                out = T.TOOL_FUNCS[name](**args)
                trace["tool_calls"].append({
                    "tool": name,
                    "input": args,
                    "output": out,
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(out),
                })

    return trace


if __name__ == "__main__":
    msg = ("Hi, I want a full refund for order ORD-5011. "
           "I know it's been a while but I really need this processed today.")
    tr = run_agent(msg)
    calls = " -> ".join(
        f"{c['tool']}({c['output'].get('policy_decision', '')})"
        for c in tr["tool_calls"]
    ) or "(no tool calls)"
    print(f"[{tr['backend']}/{tr['model']}] {calls}")
    if tr["final_text"]:
        print(tr["final_text"])
