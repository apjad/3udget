#!/usr/bin/env python3
"""Local web editor for budgetItems.json — no external dependencies, stdlib only.

Same architecture as the madbank/sub3udget editors: HTTP Basic Auth, every
mutation scoped to a single entry, and an auto-sync to GitHub after every
save. Runs on its own port so it can run alongside the other two editors.

Credentials come from 3udget-editor-credentials.local (sibling of this
repo, one level up from agentclaude/3udget-pages).
"""
import base64
import json
import os
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG_PATH = os.path.join(REPO_DIR, "budgetItems.json")
CREDS_PATH = os.path.expanduser("~/agentclaude/3udget-editor-credentials.local")
GROK_BIN = os.path.expanduser("~/.grok/bin/grok")
PORT = 8422

FILE_LOCK = threading.Lock()
VALID_CATEGORIES = {"fixedExpense", "income"}
VALID_CYCLES = {"monthly", "quarterly", "yearly"}
# Which QuickStartWizardView answer(s) in the app surface this entry as a suggestion — "always"
# means "regardless of the other answers" (e.g. El, Internet).
VALID_QUICKSTART_TAGS = {"job", "rent", "own", "car", "always", "noJob"}


def load_credentials():
    users = {}
    try:
        with open(CREDS_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                username, sep, password = line.partition(":")
                if sep and username and password:
                    users[username] = password
    except FileNotFoundError:
        pass
    if not users:
        sys.exit(f"Mangler login i {CREDS_PATH} — kan ikke starte serveren uden.")
    return users


AUTH_USERS = load_credentials()
INDEX_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")


def clean_entry(entry, index_label):
    if not isinstance(entry, dict):
        raise ValueError(f"{index_label} er ikke et gyldigt objekt")
    name = str(entry.get("name", "")).strip()
    if not name:
        raise ValueError(f"{index_label} mangler et navn")
    domain = str(entry.get("domain", "")).strip()
    icon = str(entry.get("icon", "")).strip()
    category = str(entry.get("category", "")).strip()
    if category not in VALID_CATEGORIES:
        raise ValueError(f'"{name}" skal have "fixedExpense" eller "income" som type')
    try:
        price = float(entry.get("price", 0))
    except (TypeError, ValueError):
        raise ValueError(f'"{name}" har en ugyldig pris')
    if price < 0:
        raise ValueError(f'"{name}" kan ikke have en negativ pris')
    cycle = str(entry.get("billingCycle", "")).strip()
    if cycle not in VALID_CYCLES:
        raise ValueError(f'"{name}" skal have "monthly", "quarterly" eller "yearly" som gentagelse')
    raw_tags = entry.get("quickStartTags") or []
    if not isinstance(raw_tags, list):
        raise ValueError(f'"{name}" har ugyldige quick-start tags')
    tags = sorted({str(t).strip() for t in raw_tags if str(t).strip()})
    invalid = [t for t in tags if t not in VALID_QUICKSTART_TAGS]
    if invalid:
        raise ValueError(f'"{name}" har ukendte quick-start tags: {", ".join(invalid)}')
    cleaned = {"name": name, "category": category, "billingCycle": cycle, "price": price}
    if domain:
        cleaned["domain"] = domain
    if icon:
        cleaned["icon"] = icon
    if tags:
        cleaned["quickStartTags"] = tags
    return cleaned


def load_catalog_unlocked():
    with open(CATALOG_PATH, encoding="utf-8") as f:
        return json.load(f)["budgetItems"]


def save_catalog_unlocked(entries):
    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump({"budgetItems": entries}, f, indent=2, ensure_ascii=False)
        f.write("\n")


class Handler(BaseHTTPRequestHandler):
    server_version = "ThreeUdgetEditor/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _check_auth(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, _, password = decoded.partition(":")
        except Exception:
            return False
        return AUTH_USERS.get(user) == password

    def _require_auth(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="3udget"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        return json.loads(raw)

    def _name_from_path(self, prefix):
        if not self.path.startswith(prefix):
            return None
        return urllib.parse.unquote(self.path[len(prefix):])

    def do_GET(self):
        if not self._check_auth():
            return self._require_auth()
        if self.path == "/":
            with open(INDEX_HTML_PATH, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/budgetItems":
            with FILE_LOCK:
                entries = load_catalog_unlocked()
            self._send_json(200, {"budgetItems": entries})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if not self._check_auth():
            return self._require_auth()
        if self.path == "/api/budgetItems":
            return self._add_entry()
        if self.path == "/api/sync":
            return self._send_json(200, self._run_sync())
        if self.path == "/api/suggest-price":
            try:
                body = self._read_json_body()
                name = body.get("name", "").strip()
            except json.JSONDecodeError:
                name = ""
            if not name:
                return self._send_json(400, {"error": "Mangler navn"})
            return self._send_json(200, self._suggest_price(name))
        self.send_response(404)
        self.end_headers()

    def do_PUT(self):
        if not self._check_auth():
            return self._require_auth()
        original_name = self._name_from_path("/api/budgetItems/")
        if original_name is None:
            self.send_response(404)
            self.end_headers()
            return
        self._edit_entry(original_name)

    def do_DELETE(self):
        if not self._check_auth():
            return self._require_auth()
        original_name = self._name_from_path("/api/budgetItems/")
        if original_name is None:
            self.send_response(404)
            self.end_headers()
            return
        self._delete_entry(original_name)

    def _add_entry(self):
        try:
            new_entry = clean_entry(self._read_json_body(), "Posten")
        except (json.JSONDecodeError, ValueError) as e:
            return self._send_json(400, {"error": str(e)})
        with FILE_LOCK:
            entries = load_catalog_unlocked()
            if any(e["name"].lower() == new_entry["name"].lower() for e in entries):
                return self._send_json(409, {"error": f'"{new_entry["name"]}" findes allerede'})
            entries.append(new_entry)
            save_catalog_unlocked(entries)
        self._send_json(200, {"ok": True, "budgetItems": entries, "sync": self._run_sync()})

    def _edit_entry(self, original_name):
        try:
            updated = clean_entry(self._read_json_body(), "Posten")
        except (json.JSONDecodeError, ValueError) as e:
            return self._send_json(400, {"error": str(e)})
        with FILE_LOCK:
            entries = load_catalog_unlocked()
            index = next((i for i, e in enumerate(entries) if e["name"].lower() == original_name.lower()), None)
            if index is None:
                return self._send_json(404, {
                    "error": f'"{original_name}" findes ikke længere — nogen har nok allerede ændret den. Genindlæs listen.'
                })
            renamed = updated["name"].lower() != original_name.lower()
            if renamed and any(i != index and e["name"].lower() == updated["name"].lower() for i, e in enumerate(entries)):
                return self._send_json(409, {"error": f'"{updated["name"]}" findes allerede'})
            entries[index] = updated
            save_catalog_unlocked(entries)
        self._send_json(200, {"ok": True, "budgetItems": entries, "sync": self._run_sync()})

    def _delete_entry(self, original_name):
        with FILE_LOCK:
            entries = load_catalog_unlocked()
            filtered = [e for e in entries if e["name"].lower() != original_name.lower()]
            if len(filtered) != len(entries):
                save_catalog_unlocked(filtered)
            entries = filtered
        self._send_json(200, {"ok": True, "budgetItems": entries, "sync": self._run_sync()})

    def _suggest_price(self, name):
        prompt = (
            f"Look up a realistic, current (2026) average Danish household figure in Danish "
            f"kroner (DKK) for \"{name}\" — this could be an expense or an income source for a "
            f"typical Danish household. Use real statistics or typical market/government rates "
            f"(Danmarks Statistik, typical bank/forsikringsselskab prices, Udbetaling Danmark "
            f"satser, etc.) where relevant, not a guess. Report the amount per occurrence of "
            f"whichever billing cycle is the natural/standard one for this item (most household "
            f"bills are monthly, but heating/water aconto and things like børnetilskud are "
            f"typically quarterly, and some are yearly) — report the raw per-occurrence amount, "
            f"not already averaged to monthly."
        )
        schema = json.dumps({
            "type": "object",
            "properties": {
                "price": {"type": "number"},
                "billingCycle": {"type": "string", "enum": ["monthly", "quarterly", "yearly"]},
                "category": {"type": "string", "enum": ["fixedExpense", "income"]},
            },
            "required": ["price", "billingCycle", "category"],
        })
        try:
            result = subprocess.run(
                [GROK_BIN, "-p", prompt, "--json-schema", schema],
                capture_output=True, text=True, timeout=150,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return {"ok": False, "error": str(e)}
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip() or "grok fejlede"}
        try:
            data = json.loads(result.stdout)["structuredOutput"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return {"ok": False, "error": "Kunne ikke aflæse svar fra AI"}
        return {
            "ok": True,
            "price": data["price"],
            "billingCycle": data["billingCycle"],
            "category": data["category"],
        }

    def _run_sync(self):
        def run(*args):
            return subprocess.run(
                args, cwd=REPO_DIR, capture_output=True, text=True, timeout=30
            )

        with FILE_LOCK:
            pull = run("git", "pull", "origin", "main", "--quiet", "--no-edit")
            if pull.returncode != 0:
                return {"ok": False, "step": "pull", "log": pull.stderr}

            status = run("git", "status", "--porcelain", "budgetItems.json")
            if not status.stdout.strip():
                return {"ok": True, "changed": False, "log": "Ingen ændringer at synkronisere."}

            run("git", "add", "budgetItems.json")
            commit = run("git", "commit", "-q", "-m", "Opdater katalog via web-editor")
            if commit.returncode != 0:
                return {"ok": False, "step": "commit", "log": commit.stderr}

            push = run("git", "push", "origin", "main", "--quiet")
            if push.returncode != 0:
                return {"ok": False, "step": "push", "log": push.stderr}

        return {"ok": True, "changed": True, "log": "Sendt til GitHub — live om et øjeblik."}


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"3udget-editor kører på http://localhost:{PORT}  ({len(AUTH_USERS)} bruger(e), se {CREDS_PATH})")
    print("Tryk Ctrl+C for at stoppe.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
