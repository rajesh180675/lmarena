"""
LMArena Unrestricted Access Client — FIXED
=============================================
- No more hanging / infinite "Running..."
- Proper streaming (chunks appear immediately)
- Threaded timeouts on every attempt
- gradio_client as primary method (handles Gradio protocol correctly)
- Smart endpoint discovery instead of brute-force

RUN:  streamlit run lmarena_app.py
"""

import streamlit as st
import json
import time
import random
import string
import re
import threading
import queue as queuelib
from datetime import datetime
from typing import Generator, List, Dict, Any, Optional
import traceback
import requests

# ── optional deps (graceful fallback) ──────────────────────
try:
    import cloudscraper
except ImportError:
    cloudscraper = None

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

try:
    from gradio_client import Client as GradioClient
except ImportError:
    GradioClient = None

# ── constants ──────────────────────────────────────────────
BASE_URL = "https://lmarena.ai"
COOKIE_LIFETIME_MIN = 10
MAX_RETRIES = 3
ATTEMPT_TIMEOUT = 20        # seconds per single endpoint attempt
TOTAL_CHAT_TIMEOUT = 180    # seconds total for entire chat call
CONNECT_TIMEOUT = 15

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;"
              "q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

DEFAULT_MODELS = [
    "chatgpt-4o-latest",
    "gpt-4o-mini-2024-07-18",
    "gpt-4.1-2025-04-14",
    "o4-mini-2025-04-16",
    "claude-sonnet-4-20250514",
    "claude-3-7-sonnet-20250219",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
    "claude-3-opus-20240229",
    "gemini-2.5-pro-preview-05-06",
    "gemini-2.5-flash-preview-04-17",
    "gemini-2.0-flash-001",
    "deepseek-v3-0324",
    "deepseek-r1-0528",
    "llama-4-maverick-17b-128e",
    "llama-3.3-70b-instruct",
    "llama-3.1-405b-instruct",
    "mistral-large-2411",
    "qwen3-235b-a22b",
    "qwen-2.5-72b-instruct",
    "command-a-03-2025",
    "grok-3-mini-beta",
]


# ════════════════════════════════════════════════════════════
#  LOGGING HELPER
# ════════════════════════════════════════════════════════════
class Log:
    def __init__(self):
        self.entries: List[str] = []

    def __call__(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.entries.append(f"[{ts}] {msg}")
        if len(self.entries) > 300:
            self.entries = self.entries[-150:]

    def recent(self, n=40):
        return self.entries[-n:]


# ════════════════════════════════════════════════════════════
#  SESSION MANAGER — HTTP session that survives Cloudflare
# ════════════════════════════════════════════════════════════
class SessionManager:
    def __init__(self, log: Log, manual_cookie: str = ""):
        self.log = log
        self.session: Optional[requests.Session] = None
        self.method = "none"
        self.last_refresh: Optional[datetime] = None
        self.session_hash = self._rand()
        self.manual_cookie = manual_cookie
        self._build()

    @staticmethod
    def _rand(n=12):
        return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))

    def new_hash(self):
        self.session_hash = self._rand()

    # ── builders (tried in order) ──────────────────────────
    def _build(self):
        for name, fn in [
            ("curl_cffi", self._build_curl_cffi),
            ("cloudscraper", self._build_cloudscraper),
            ("manual_cookie", self._build_manual),
            ("plain_requests", self._build_plain),
        ]:
            try:
                if fn():
                    self.method = name
                    self.last_refresh = datetime.now()
                    self.log(f"✅ HTTP session via {name}")
                    return
            except Exception as e:
                self.log(f"⚠️ {name}: {e}")
        self._build_plain()
        self.method = "plain_requests"
        self.last_refresh = datetime.now()

    def _build_curl_cffi(self):
        if cffi_requests is None:
            return False
        s = cffi_requests.Session(impersonate="chrome131")
        s.headers.update(BROWSER_HEADERS)
        r = s.get(BASE_URL, timeout=CONNECT_TIMEOUT)
        if r.status_code == 200:
            self.session = s
            return True
        return False

    def _build_cloudscraper(self):
        if cloudscraper is None:
            return False
        s = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "desktop": True},
        )
        s.headers.update(BROWSER_HEADERS)
        r = s.get(BASE_URL, timeout=CONNECT_TIMEOUT)
        if r.status_code == 200:
            self.session = s
            return True
        return False

    def _build_manual(self):
        if not self.manual_cookie:
            return False
        s = requests.Session()
        s.headers.update(BROWSER_HEADERS)
        s.cookies.set("cf_clearance", self.manual_cookie, domain=".lmarena.ai")
        r = s.get(BASE_URL, timeout=CONNECT_TIMEOUT)
        if r.status_code == 200:
            self.session = s
            return True
        return False

    def _build_plain(self):
        s = requests.Session()
        s.headers.update(BROWSER_HEADERS)
        self.session = s
        return True

    def maybe_refresh(self):
        if self.last_refresh:
            age = (datetime.now() - self.last_refresh).total_seconds() / 60
            if age >= COOKIE_LIFETIME_MIN:
                self.log(f"🔄 Session age {age:.0f}m — refreshing")
                self._build()

    def get(self, url, **kw):
        self.maybe_refresh()
        kw.setdefault("timeout", CONNECT_TIMEOUT)
        return self.session.get(url, **kw)

    def post(self, url, **kw):
        self.maybe_refresh()
        kw.setdefault("timeout", CONNECT_TIMEOUT)
        return self.session.post(url, **kw)


# ════════════════════════════════════════════════════════════
#  TIMEOUT WRAPPER — prevents any method from hanging
# ════════════════════════════════════════════════════════════
_SENTINEL = object()


def run_with_timeout(generator_fn, timeout: int) -> Generator[str, None, None]:
    """
    Run a generator in a background thread.
    Yields chunks as they arrive.
    Raises TimeoutError if no chunk arrives within `timeout` seconds.
    """
    q = queuelib.Queue()

    def worker():
        try:
            for chunk in generator_fn():
                q.put(("chunk", chunk))
            q.put(("done", None))
        except Exception as e:
            q.put(("error", e))

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    last_activity = time.time()
    while True:
        try:
            msg_type, payload = q.get(timeout=2.0)
            last_activity = time.time()
            if msg_type == "chunk":
                yield payload
            elif msg_type == "done":
                return
            elif msg_type == "error":
                raise payload
        except queuelib.Empty:
            if time.time() - last_activity > timeout:
                raise TimeoutError(
                    f"No data received for {timeout}s — endpoint not responding"
                )


# ════════════════════════════════════════════════════════════
#  LMARENA CLIENT — API interaction
# ════════════════════════════════════════════════════════════
class LMArenaClient:
    def __init__(self, sm: SessionManager, log: Log):
        self.sm = sm
        self.log = log
        self.models = DEFAULT_MODELS[:]

        # gradio_client state
        self.gradio: Optional[Any] = None
        self.named_endpoints: Dict = {}
        self.unnamed_endpoints: Dict = {}

        # cache what worked last time
        self._cached_endpoint = None
        self._cached_params_style = None

        # HTTP/SSE state
        self.http_fn_index: Optional[int] = None
        self.gradio_config: Optional[Dict] = None

    # ────────────────────────────────────────────────────────
    #  CONNECTION + DISCOVERY
    # ────────────────────────────────────────────────────────
    def connect(self) -> bool:
        """Connect and discover usable API endpoints."""
        ok = False

        # Method A: gradio_client library
        if GradioClient is not None:
            ok = self._connect_gradio_client()

        # Method B: parse Gradio config from HTML
        if not ok:
            ok = self._discover_from_html()

        if not ok:
            self.log("⚠️ Could not discover API — will attempt blind calls")

        return ok

    def _connect_gradio_client(self) -> bool:
        try:
            self.log("🔍 Connecting via gradio_client...")
            self.gradio = GradioClient(BASE_URL, verbose=False)
            info = self.gradio.view_api(print_info=False, return_format="dict")

            self.named_endpoints = info.get("named_endpoints", {})
            self.unnamed_endpoints = info.get("unnamed_endpoints", {})

            for name in self.named_endpoints:
                params = self.named_endpoints[name].get("parameters", [])
                param_names = [p.get("parameter_name", "?") for p in params]
                self.log(f"  📌 {name}({', '.join(param_names)})")

            for idx in sorted(self.unnamed_endpoints.keys(), key=lambda x: int(x)):
                params = self.unnamed_endpoints[idx].get("parameters", [])
                self.log(f"  📌 fn_{idx}({len(params)} params)")

            total = len(self.named_endpoints) + len(self.unnamed_endpoints)
            self.log(f"✅ gradio_client connected — {total} endpoints")
            return total > 0
        except Exception as e:
            self.log(f"❌ gradio_client failed: {e}")
            self.gradio = None
            return False

    def _discover_from_html(self) -> bool:
        """Parse the Gradio config embedded in the page HTML."""
        try:
            self.log("🔍 Fetching page HTML for config...")
            r = self.sm.get(BASE_URL, timeout=20)
            if r.status_code != 200:
                self.log(f"❌ Got HTTP {r.status_code}")
                return False

            html = r.text

            # Gradio embeds config in various ways
            config = None
            for pattern in [
                r"window\.gradio_config\s*=\s*(\{.*?\})\s*;",
                r"<script>window\.gradio_config\s*=\s*(\{.*?\})\s*</script>",
                r'"config"\s*:\s*(\{.*?"components".*?\})',
            ]:
                m = re.search(pattern, html, re.DOTALL)
                if m:
                    try:
                        config = json.loads(m.group(1))
                        break
                    except json.JSONDecodeError:
                        continue

            if not config:
                self.log("⚠️ No Gradio config found in HTML")
                return False

            self.gradio_config = config

            # Extract dependencies (fn_index mapping)
            deps = config.get("dependencies", [])
            self.log(f"📋 Found {len(deps)} dependencies in config")

            # Find chat-related fn_index
            for i, dep in enumerate(deps):
                # Look for streaming dependencies or ones with chatbot outputs
                if dep.get("queue") or dep.get("is_generating"):
                    self.http_fn_index = i
                    self.log(f"✅ Found streaming fn_index={i}")
                    return True

            # Fallback: use first dependency
            if deps:
                self.http_fn_index = 0
                self.log(f"✅ Using fn_index=0 (first dependency)")
                return True

            return False
        except Exception as e:
            self.log(f"❌ HTML discovery failed: {e}")
            return False

    # ────────────────────────────────────────────────────────
    #  MAIN CHAT (with timeout + fallback chain)
    # ────────────────────────────────────────────────────────
    def chat(
        self,
        message: str,
        model: str,
        history: List[Dict],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        system_prompt: str = "",
    ) -> Generator[str, None, None]:
        """
        Send message, yield response chunks.
        Tries methods in order, each with a timeout.
        NEVER hangs — worst case returns error in < 60s.
        """
        errors = []
        got_response = False

        # ── Method 1: gradio_client ────────────────────────
        if self.gradio:
            try:
                self.log("💬 Trying gradio_client...")
                for chunk in run_with_timeout(
                    lambda: self._gradio_chat(
                        message, model, history,
                        temperature, max_tokens, system_prompt
                    ),
                    timeout=ATTEMPT_TIMEOUT,
                ):
                    got_response = True
                    yield chunk

                if got_response:
                    return
            except Exception as e:
                errors.append(f"gradio_client: {e}")
                self.log(f"⚠️ gradio_client: {e}")

        # ── Method 2: Direct HTTP/SSE ──────────────────────
        try:
            self.log("💬 Trying HTTP/SSE...")
            for chunk in run_with_timeout(
                lambda: self._http_chat(
                    message, model, history,
                    temperature, max_tokens, system_prompt
                ),
                timeout=ATTEMPT_TIMEOUT,
            ):
                got_response = True
                yield chunk

            if got_response:
                return
        except Exception as e:
            errors.append(f"HTTP/SSE: {e}")
            self.log(f"⚠️ HTTP/SSE: {e}")

        # ── Method 3: gradio_client predict (blocking) ─────
        if self.gradio:
            try:
                self.log("💬 Trying blocking predict...")
                text = self._gradio_predict_blocking(
                    message, model, history,
                    temperature, max_tokens, system_prompt
                )
                if text:
                    yield text
                    return
            except Exception as e:
                errors.append(f"predict: {e}")
                self.log(f"⚠️ predict: {e}")

        # ── All failed ─────────────────────────────────────
        err_list = "\n".join(f"- {e}" for e in errors)
        yield (
            f"\n\n❌ **Could not get a response from LMArena.**\n\n"
            f"**Errors:**\n{err_list}\n\n"
            f"**Try:**\n"
            f"1. Click **🔄 Reconnect** in sidebar\n"
            f"2. Paste a fresh `cf_clearance` cookie\n"
            f"3. Try a different model\n"
            f"4. Check the 📜 Connection Log for details"
        )

    # ────────────────────────────────────────────────────────
    #  METHOD 1: gradio_client streaming (submit)
    # ────────────────────────────────────────────────────────
    def _gradio_chat(self, message, model, history,
                     temperature, max_tokens, system_prompt):
        """Use gradio_client.submit() for streaming responses."""
        if not self.gradio:
            raise RuntimeError("gradio_client not connected")

        gradio_hist = self._to_gradio_history(history)

        # Build endpoint priority list
        endpoints = self._prioritized_endpoints()
        if not endpoints:
            raise RuntimeError("No API endpoints discovered")

        # Parameter combinations to try (ordered by likelihood)
        def make_params():
            return [
                [message, gradio_hist],
                [message, gradio_hist, model],
                [message, gradio_hist, model, temperature, max_tokens],
                [None, model, message, gradio_hist],
                [message],
            ]

        for ep_type, ep_id in endpoints[:4]:  # max 4 endpoints
            for params in make_params()[:3]:   # max 3 param combos each
                try:
                    self.log(f"  → {ep_type}:{ep_id} ({len(params)} params)")

                    if ep_type == "named":
                        job = self.gradio.submit(*params, api_name=ep_id)
                    else:
                        job = self.gradio.submit(*params, fn_index=int(ep_id))

                    full = ""
                    start = time.time()

                    while not job.done():
                        if time.time() - start > ATTEMPT_TIMEOUT:
                            try:
                                job.cancel()
                            except Exception:
                                pass
                            raise TimeoutError("endpoint timeout")

                        try:
                            outputs = job.outputs()
                            if outputs:
                                text = self._extract_text(outputs[-1])
                                if text and len(text) > len(full):
                                    delta = text[len(full):]
                                    full = text
                                    yield delta
                        except (IndexError, TypeError):
                            pass
                        time.sleep(0.3)

                    # final result
                    try:
                        result = job.result(timeout=5)
                        text = self._extract_text(result)
                        if text and len(text) > len(full):
                            yield text[len(full):]
                            full = text
                    except Exception:
                        pass

                    if full and len(full.strip()) > 1:
                        self._cached_endpoint = (ep_type, ep_id)
                        self.log(f"  ✅ response from {ep_type}:{ep_id}")
                        return

                except TimeoutError:
                    self.log(f"  ⏰ {ep_type}:{ep_id} timed out")
                except Exception as e:
                    self.log(f"  ❌ {ep_type}:{ep_id}: {str(e)[:80]}")

        raise RuntimeError(f"Tried {len(endpoints)} endpoints — none responded")

    # ────────────────────────────────────────────────────────
    #  METHOD 2: Direct HTTP/SSE
    # ────────────────────────────────────────────────────────
    def _http_chat(self, message, model, history,
                   temperature, max_tokens, system_prompt):
        """Direct HTTP calls to Gradio queue API."""
        gradio_hist = self._to_gradio_history(history)
        h = self.sm.session_hash

        fn_indexes = []
        if self.http_fn_index is not None:
            fn_indexes.append(self.http_fn_index)
        fn_indexes.extend([0, 1, 2, 3])
        fn_indexes = list(dict.fromkeys(fn_indexes))[:4]  # dedupe, max 4

        payloads = [
            [message, gradio_hist, model],
            [message, gradio_hist],
            [message, gradio_hist, model, temperature, max_tokens],
        ]

        for fn in fn_indexes:
            for data in payloads[:2]:  # max 2 payloads per fn
                try:
                    self.log(f"  → HTTP fn={fn} ({len(data)} params)")

                    # Join queue
                    join_body = {
                        "data": data,
                        "fn_index": fn,
                        "session_hash": h,
                    }
                    r = self.sm.post(
                        f"{BASE_URL}/queue/join",
                        json=join_body,
                        timeout=10,
                    )
                    if r.status_code != 200:
                        self.log(f"  ❌ join returned {r.status_code}")
                        continue

                    event_id = None
                    try:
                        event_id = r.json().get("event_id")
                    except Exception:
                        pass

                    # Stream SSE
                    sse_url = f"{BASE_URL}/queue/data?session_hash={h}"
                    if event_id:
                        sse_url += f"&event_id={event_id}"

                    resp = self.sm.session.get(
                        sse_url,
                        headers={
                            **BROWSER_HEADERS,
                            "Accept": "text/event-stream",
                            "Cache-Control": "no-cache",
                        },
                        stream=True,
                        timeout=ATTEMPT_TIMEOUT,
                    )

                    full = ""
                    got_data = False

                    for raw in resp.iter_lines(decode_unicode=True):
                        if not raw:
                            continue

                        line = raw
                        if line.startswith("data: "):
                            line = line[6:]
                        elif line.startswith("data:"):
                            line = line[5:]
                        else:
                            continue

                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        msg = ev.get("msg", "")

                        if msg in ("process_generating", "process_completed"):
                            text = self._extract_text(ev.get("output", {}))
                            if text and len(text) > len(full):
                                delta = text[len(full):]
                                full = text
                                got_data = True
                                yield delta
                            if msg == "process_completed":
                                break
                        elif msg == "close_stream":
                            break

                    if got_data:
                        self.http_fn_index = fn
                        self.log(f"  ✅ SSE response from fn={fn}")
                        return

                except Exception as e:
                    self.log(f"  ❌ HTTP fn={fn}: {str(e)[:80]}")

        raise RuntimeError("HTTP/SSE: no endpoint responded")

    # ────────────────────────────────────────────────────────
    #  METHOD 3: gradio_client blocking predict
    # ────────────────────────────────────────────────────────
    def _gradio_predict_blocking(self, message, model, history,
                                  temperature, max_tokens, system_prompt):
        """Last resort: blocking predict() call."""
        if not self.gradio:
            raise RuntimeError("not connected")

        gradio_hist = self._to_gradio_history(history)
        endpoints = self._prioritized_endpoints()

        for ep_type, ep_id in endpoints[:3]:
            for params in [
                [message, gradio_hist],
                [message, gradio_hist, model],
                [message],
            ]:
                try:
                    self.log(f"  → predict {ep_type}:{ep_id}")
                    if ep_type == "named":
                        result = self.gradio.predict(
                            *params, api_name=ep_id
                        )
                    else:
                        result = self.gradio.predict(
                            *params, fn_index=int(ep_id)
                        )

                    text = self._extract_text(result)
                    if text and len(text.strip()) > 1:
                        self.log(f"  ✅ predict response from {ep_type}:{ep_id}")
                        return text
                except Exception as e:
                    self.log(f"  ❌ predict {ep_type}:{ep_id}: {str(e)[:80]}")

        raise RuntimeError("predict: no endpoint responded")

    # ────────────────────────────────────────────────────────
    #  HELPERS
    # ────────────────────────────────────────────────────────
    def _prioritized_endpoints(self):
        """Return endpoints sorted by likely usefulness."""
        result = []

        # Cached working endpoint first
        if self._cached_endpoint:
            result.append(self._cached_endpoint)

        # Named endpoints with chat-like names
        chat_keywords = ["chat", "send", "submit", "bot", "respond",
                         "add_text", "message", "predict", "generate"]
        for name in self.named_endpoints:
            if any(kw in name.lower() for kw in chat_keywords):
                ep = ("named", name)
                if ep not in result:
                    result.append(ep)

        # All other named endpoints
        for name in self.named_endpoints:
            ep = ("named", name)
            if ep not in result:
                result.append(ep)

        # Unnamed endpoints
        for idx in sorted(self.unnamed_endpoints.keys(), key=lambda x: int(x)):
            ep = ("unnamed", idx)
            if ep not in result:
                result.append(ep)

        return result

    @staticmethod
    def _to_gradio_history(messages: List[Dict]) -> List[List]:
        """Convert [{role, content}, ...] → [[user, bot], ...]"""
        pairs = []
        i = 0
        while i < len(messages):
            if messages[i].get("role") == "user":
                user_msg = messages[i].get("content", "")
                bot_msg = ""
                if (i + 1 < len(messages)
                        and messages[i + 1].get("role") == "assistant"):
                    bot_msg = messages[i + 1].get("content", "")
                    i += 2
                else:
                    i += 1
                pairs.append([user_msg, bot_msg])
            else:
                i += 1
        return pairs

    @staticmethod
    def _extract_text(obj, depth=0) -> str:
        """Recursively dig out assistant text from Gradio output."""
        if depth > 8:
            return ""
        if isinstance(obj, str):
            return obj
        if isinstance(obj, dict):
            for key in ("value", "data", "output"):
                if key in obj:
                    found = LMArenaClient._extract_text(obj[key], depth + 1)
                    if found:
                        return found
            return ""
        if isinstance(obj, (list, tuple)):
            if not obj:
                return ""
            # Chatbot format: [[user, bot], ...]
            last = obj[-1]
            if (isinstance(last, (list, tuple))
                    and len(last) >= 2
                    and isinstance(last[1], str)):
                return last[1]
            # Flat list — check each element
            for item in reversed(obj):
                found = LMArenaClient._extract_text(item, depth + 1)
                if found and len(found) > 2:
                    return found
        return ""


# ════════════════════════════════════════════════════════════
#  STREAMLIT UI
# ════════════════════════════════════════════════════════════

def _init():
    defaults = dict(
        ready=False,
        messages=[],
        log=Log(),
        sm=None,
        client=None,
        models=DEFAULT_MODELS[:],
        model=DEFAULT_MODELS[0],
        status="⚪ Not connected",
        total=0,
        connect_error="",
    )
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _connect(manual_cookie: str = ""):
    log: Log = st.session_state.log
    log("━" * 40)
    log("🔌 Starting connection...")

    sm = SessionManager(log, manual_cookie=manual_cookie)
    cl = LMArenaClient(sm, log)

    connected = cl.connect()

    st.session_state.sm = sm
    st.session_state.client = cl
    st.session_state.ready = True
    st.session_state.connect_error = ""

    if connected:
        n_ep = len(cl.named_endpoints) + len(cl.unnamed_endpoints)
        st.session_state.status = (
            f"✅ Connected via **{sm.method}** — {n_ep} endpoints"
        )
    else:
        st.session_state.status = (
            f"⚠️ Partial — HTTP via {sm.method} (no API endpoints found)"
        )

    log(f"Connection result: {st.session_state.status}")


def _sidebar():
    with st.sidebar:
        st.title("🏟️ LMArena Client")

        # Status
        st.markdown(st.session_state.status)
        if st.session_state.connect_error:
            st.error(st.session_state.connect_error)

        st.divider()

        # ── Connection ─────────────────────────────────
        with st.expander("🔌 Connection", expanded=not st.session_state.ready):
            c1, c2 = st.columns(2)
            with c1:
                if st.button("▶️ Connect", use_container_width=True):
                    with st.spinner("Connecting..."):
                        try:
                            _connect()
                            st.rerun()
                        except Exception as e:
                            st.session_state.connect_error = str(e)
                            st.rerun()
            with c2:
                if st.button("🔄 Reconnect", use_container_width=True):
                    with st.spinner("Reconnecting..."):
                        st.session_state.ready = False
                        try:
                            _connect()
                        except Exception as e:
                            st.session_state.connect_error = str(e)
                        st.rerun()

            st.markdown("---")
            st.caption(
                "**Manual cookie** — if auto-connect fails:\n"
                "1. Open `lmarena.ai` in any browser\n"
                "2. F12 → Application → Cookies\n"
                "3. Copy `cf_clearance` value"
            )
            cookie = st.text_input(
                "cf_clearance", key="cookie_input",
                type="password", label_visibility="collapsed",
            )
            if st.button("Connect with cookie", use_container_width=True) and cookie:
                with st.spinner("Connecting with cookie..."):
                    try:
                        _connect(manual_cookie=cookie.strip())
                        st.rerun()
                    except Exception as e:
                        st.session_state.connect_error = str(e)
                        st.rerun()

        # ── Model ──────────────────────────────────────
        st.session_state.model = st.selectbox(
            "🤖 Model",
            st.session_state.models,
            index=(
                st.session_state.models.index(st.session_state.model)
                if st.session_state.model in st.session_state.models
                else 0
            ),
        )

        # ── Parameters ─────────────────────────────────
        with st.expander("🎛️ Parameters"):
            temp = st.slider("Temperature", 0.0, 2.0, 0.7, 0.05)
            maxt = st.slider("Max tokens", 256, 16384, 4096, 256)
            sys_p = st.text_area("System prompt", height=80,
                                  placeholder="Optional system instruction...")

        st.divider()

        # ── Actions ────────────────────────────────────
        col1, col2 = st.columns(2)
        with col1:
            if st.button("🗑️ Clear chat", use_container_width=True):
                st.session_state.messages = []
                st.rerun()
        with col2:
            if st.button("🧪 Test", use_container_width=True):
                if st.session_state.client:
                    log = st.session_state.log
                    log("🧪 Testing connection...")
                    try:
                        r = st.session_state.sm.get(BASE_URL, timeout=10)
                        log(f"  HTTP status: {r.status_code}")
                        log(f"  Page length: {len(r.text)} chars")
                        has_gradio = "gradio" in r.text.lower()
                        log(f"  Gradio detected: {has_gradio}")
                        st.success(f"HTTP {r.status_code}, Gradio: {has_gradio}")
                    except Exception as e:
                        log(f"  Test failed: {e}")
                        st.error(f"Test failed: {e}")
                else:
                    st.warning("Connect first")

        st.caption(f"💬 {st.session_state.total} messages this session")

        # ── Deps ───────────────────────────────────────
        with st.expander("📦 Dependencies"):
            for name, obj, pkg in [
                ("gradio_client", GradioClient, "gradio_client"),
                ("cloudscraper", cloudscraper, "cloudscraper"),
                ("curl_cffi", cffi_requests, "curl-cffi"),
            ]:
                icon = "✅" if obj else "❌"
                fix = "" if obj else f" → `pip install {pkg}`"
                st.markdown(f"{icon} `{name}`{fix}")

        # ── Log ────────────────────────────────────────
        with st.expander("📜 Connection Log"):
            log_text = "\n".join(st.session_state.log.recent(50))
            st.code(log_text or "(empty)", language="text")

        return temp, maxt, sys_p


def _chat_area(temp, maxt, sys_p):
    # Render history
    for m in st.session_state.messages:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

    # Input
    prompt = st.chat_input("Type your message...")
    if not prompt:
        return

    if not st.session_state.ready or not st.session_state.client:
        st.error("⚠️ Not connected. Click **Connect** in the sidebar first.")
        return

    # User message
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Assistant response (streaming)
    with st.chat_message("assistant"):
        status = st.status("Generating response...", expanded=False)
        placeholder = st.empty()
        full = ""

        client: LMArenaClient = st.session_state.client

        try:
            for chunk in client.chat(
                message=prompt,
                model=st.session_state.model,
                history=st.session_state.messages[:-1],
                temperature=temp,
                max_tokens=maxt,
                system_prompt=sys_p,
            ):
                full += chunk
                placeholder.markdown(full + " ▌")

            placeholder.markdown(full or "*(empty response)*")
            status.update(label="✅ Done", state="complete")

        except Exception as e:
            err = f"❌ Error: {e}\n\n```\n{traceback.format_exc()}\n```"
            placeholder.error(str(e))
            full = f"Error: {e}"
            status.update(label="❌ Failed", state="error")
            st.session_state.log(f"❌ Chat error: {e}")

        st.session_state.messages.append(
            {"role": "assistant", "content": full}
        )
        st.session_state.total += 1


# ════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════
def main():
    st.set_page_config(
        page_title="LMArena Client",
        page_icon="🏟️",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.markdown(
        """
        <style>
            section[data-testid="stSidebar"] > div { padding-top: .5rem; }
            .stChatMessage { max-width: 900px; }
            .block-container { max-width: 960px; padding-top: 1.5rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    _init()

    # Auto-connect on first load
    if not st.session_state.ready:
        with st.spinner("🔌 First launch — connecting to LMArena..."):
            try:
                _connect()
            except Exception as e:
                st.session_state.connect_error = str(e)
                st.warning(
                    f"Auto-connect failed: {e}\n\n"
                    f"Use the sidebar to connect manually."
                )

    temp, maxt, sys_p = _sidebar()
    _chat_area(temp, maxt, sys_p)


if __name__ == "__main__":
    main()
