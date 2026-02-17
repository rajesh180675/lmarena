"""
LMArena Unrestricted Access Client
====================================
Bypasses Cloudflare session expiry, auto-refreshes tokens,
auto-retries on failure, streams responses.

RUN:  streamlit run lmarena_app.py
"""

import streamlit as st
import json
import time
import random
import string
import re
import os
from datetime import datetime, timedelta
from typing import Generator, Optional, List, Dict, Any
import traceback
import requests

# ── optional accelerators (graceful fallback) ──────────────
try:
    import cloudscraper
except ImportError:
    cloudscraper = None

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

try:
    from gradio_client import Client as GradioNativeClient
except ImportError:
    GradioNativeClient = None

# ── constants ──────────────────────────────────────────────
BASE_URL = "https://lmarena.ai"
COOKIE_LIFETIME_MIN = 10          # refresh BEFORE cf_clearance dies
MAX_RETRIES = 4
STREAM_TIMEOUT = 360              # 6 min – even long essays finish
SSE_RECONNECT_DELAY = 2

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
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
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
    "gemini-1.5-pro-002",
    "deepseek-v3-0324",
    "deepseek-r1-0528",
    "llama-4-maverick-17b-128e",
    "llama-3.3-70b-instruct",
    "llama-3.1-405b-instruct",
    "mistral-large-2411",
    "qwen3-235b-a22b",
    "qwen-2.5-72b-instruct",
    "command-a-03-2025",
    "command-r-plus-08-2024",
    "grok-3-mini-beta",
    "yi-lightning",
]


# ════════════════════════════════════════════════════════════
#  SESSION MANAGER — Cloudflare bypass + auto-refresh
# ════════════════════════════════════════════════════════════
class SessionManager:
    """Creates and maintains an HTTP session that survives Cloudflare."""

    def __init__(self, manual_cookie: str = ""):
        self.session = None
        self.method = "none"
        self.born = datetime.now()
        self.last_refresh = None
        self.session_hash = self._rand()
        self.manual_cookie = manual_cookie
        self.log: List[str] = []
        self._build()

    # ── helpers ────────────────────────────────────────────
    @staticmethod
    def _rand(n=12):
        return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))

    def _log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        self.log.append(entry)
        if len(self.log) > 200:
            self.log = self.log[-100:]

    # ── session builders (tried in order) ──────────────────
    def _build(self):
        builders = [
            ("curl_cffi", self._build_curl_cffi),
            ("cloudscraper", self._build_cloudscraper),
            ("manual_cookie", self._build_manual),
            ("plain_requests", self._build_plain),
        ]
        for name, fn in builders:
            try:
                if fn():
                    self.method = name
                    self.last_refresh = datetime.now()
                    self._log(f"✅ connected via {name}")
                    return
            except Exception as e:
                self._log(f"⚠️ {name} failed: {e}")
        self._log("❌ all methods exhausted — using plain requests")
        self._build_plain()
        self.method = "plain_requests (fallback)"
        self.last_refresh = datetime.now()

    def _build_curl_cffi(self):
        if cffi_requests is None:
            return False
        s = cffi_requests.Session(impersonate="chrome131")
        s.headers.update(BROWSER_HEADERS)
        r = s.get(BASE_URL, timeout=30)
        if r.status_code == 200 and self._looks_legit(r.text):
            self.session = s
            return True
        return False

    def _build_cloudscraper(self):
        if cloudscraper is None:
            return False
        s = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "desktop": True},
            delay=5,
        )
        s.headers.update(BROWSER_HEADERS)
        r = s.get(BASE_URL, timeout=30)
        if r.status_code == 200 and self._looks_legit(r.text):
            self.session = s
            return True
        return False

    def _build_manual(self):
        if not self.manual_cookie:
            return False
        s = requests.Session()
        s.headers.update(BROWSER_HEADERS)
        s.cookies.set("cf_clearance", self.manual_cookie, domain=".lmarena.ai")
        r = s.get(BASE_URL, timeout=20)
        if r.status_code == 200 and self._looks_legit(r.text):
            self.session = s
            return True
        return False

    def _build_plain(self):
        s = requests.Session()
        s.headers.update(BROWSER_HEADERS)
        self.session = s
        return True

    @staticmethod
    def _looks_legit(html: str) -> bool:
        markers = ["gradio", "Chatbot Arena", "lmsys", "arena", "model"]
        html_l = html.lower()
        return any(m.lower() in html_l for m in markers)

    # ── auto-refresh ───────────────────────────────────────
    def _maybe_refresh(self):
        if self.last_refresh is None:
            return
        age = (datetime.now() - self.last_refresh).total_seconds() / 60
        if age >= COOKIE_LIFETIME_MIN:
            self._log(f"🔄 session age {age:.0f}m — refreshing")
            old_method = self.method
            self._build()
            if self.method == "none":
                self.method = old_method  # keep old if rebuild fails

    def new_hash(self):
        self.session_hash = self._rand()

    # ── HTTP wrappers with retry + auto-refresh ────────────
    def get(self, url, **kw):
        return self._request("GET", url, **kw)

    def post(self, url, **kw):
        return self._request("POST", url, **kw)

    def get_stream(self, url, **kw):
        """Streaming GET — returns raw response (caller iterates)."""
        self._maybe_refresh()
        kw.setdefault("timeout", STREAM_TIMEOUT)
        kw["stream"] = True
        for attempt in range(MAX_RETRIES):
            try:
                r = self.session.get(url, **kw)
                if r.status_code in (403, 503):
                    self._log(f"⚠️ stream GET {r.status_code}, retry {attempt+1}")
                    self._build()
                    continue
                return r
            except Exception as e:
                self._log(f"⚠️ stream GET error: {e}, retry {attempt+1}")
                time.sleep(SSE_RECONNECT_DELAY * (attempt + 1))
                self._build()
        raise ConnectionError("stream GET failed after all retries")

    def _request(self, method, url, **kw):
        self._maybe_refresh()
        kw.setdefault("timeout", 30)
        for attempt in range(MAX_RETRIES):
            try:
                r = getattr(self.session, method.lower())(url, **kw)
                if r.status_code in (403, 503):
                    self._log(f"⚠️ {method} {r.status_code}, retry {attempt+1}")
                    self._build()
                    continue
                return r
            except Exception as e:
                self._log(f"⚠️ {method} error: {e}, retry {attempt+1}")
                time.sleep(SSE_RECONNECT_DELAY * (attempt + 1))
                self._build()
        raise ConnectionError(f"{method} failed after all retries")


# ════════════════════════════════════════════════════════════
#  LMARENA CLIENT — Gradio API interaction
# ════════════════════════════════════════════════════════════
class LMArenaClient:
    """Talks to LMArena's Gradio backend."""

    def __init__(self, sm: SessionManager):
        self.sm = sm
        self.models = DEFAULT_MODELS[:]
        self.config = None
        self.working_fn = None      # cache fn_index that works
        self.working_fmt = None     # cache payload format that works

    # ── config + model discovery ───────────────────────────
    def fetch_config(self) -> bool:
        """Pull Gradio config and extract model list."""
        try:
            r = self.sm.get(f"{BASE_URL}/info", timeout=15)
            if r.status_code == 200:
                self.config = r.json()
                self.sm._log("📋 got /info config")
                return True
        except:
            pass

        try:
            r = self.sm.get(BASE_URL, timeout=20)
            if r.status_code == 200:
                # modern Gradio embeds config in a script tag
                for pattern in [
                    r'window\.gradio_config\s*=\s*({.*?})\s*;',
                    r'"config"\s*:\s*({.*?"components".*?})\s*[,;]',
                ]:
                    m = re.search(pattern, r.text, re.DOTALL)
                    if m:
                        self.config = json.loads(m.group(1))
                        self.sm._log("📋 got embedded config")
                        self._extract_models()
                        return True
        except Exception as e:
            self.sm._log(f"⚠️ config fetch: {e}")
        return False

    def _extract_models(self):
        if not self.config:
            return
        try:
            for comp in self.config.get("components", []):
                props = comp.get("props", {})
                choices = props.get("choices", [])
                if len(choices) > 5:
                    flat = [c if isinstance(c, str) else (c[0] if c else "") for c in choices]
                    flat = [c for c in flat if c]
                    if flat:
                        self.models = flat
                        self.sm._log(f"🤖 found {len(flat)} models")
                        return
        except:
            pass

    # ── core chat (streaming generator) ────────────────────
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
        Send a message → yield response chunks.
        Tries: queue SSE → predict API → gradio_client.
        Auto-retries with fresh session on failure.
        """
        methods = [
            ("queue_sse", self._chat_queue_sse),
            ("api_predict", self._chat_api_predict),
            ("gradio_client", self._chat_gradio_client),
        ]

        last_exc = None
        for name, fn in methods:
            for attempt in range(2):  # each method gets 2 tries
                try:
                    chunks = list(fn(message, model, history,
                                     temperature, max_tokens, system_prompt))
                    if chunks and any(c.strip() for c in chunks):
                        self.sm._log(f"✅ {name} worked (attempt {attempt+1})")
                        yield from chunks
                        return
                except Exception as e:
                    last_exc = e
                    self.sm._log(f"⚠️ {name} attempt {attempt+1}: {e}")
                    # refresh session between retries
                    self.sm._build()
                    self.sm.new_hash()
                    time.sleep(1)

        yield f"\n\n❌ **All methods failed.**\nLast error: `{last_exc}`\n\nTry:\n1. Click **Force Reconnect** in the sidebar\n2. Paste a fresh `cf_clearance` cookie\n3. Wait a minute and retry"

    # ── method 1: queue + SSE stream ───────────────────────
    def _chat_queue_sse(self, message, model, history,
                        temperature, max_tokens, system_prompt):

        gradio_hist = self._to_gradio_history(history)

        # payload formats LMArena has used (try cached first, then all)
        formats = self._payload_formats(message, model, gradio_hist,
                                         temperature, max_tokens, system_prompt)
        if self.working_fmt is not None:
            formats = [formats[self.working_fmt]] + formats

        fn_range = ([self.working_fn] if self.working_fn is not None else []) + \
                   list(range(12))

        for fmt_idx, data in enumerate(formats):
            for fn in fn_range[:6]:
                try:
                    result = list(self._do_sse(fn, data))
                    if result:
                        self.working_fn = fn
                        self.working_fmt = fmt_idx
                        yield from result
                        return
                except Exception:
                    continue

        raise RuntimeError("no fn_index + payload combo worked")

    def _payload_formats(self, msg, model, hist, temp, maxt, sys_p):
        return [
            [msg, hist, model, temp, maxt],
            [None, model, msg, hist],
            [model, msg, hist, temp, maxt],
            [msg, hist, model],
            [msg, model],
            [msg, hist, model, sys_p, temp, maxt],
        ]

    def _do_sse(self, fn_index, data) -> Generator[str, None, None]:
        """POST /queue/join then stream /queue/data."""
        h = self.sm.session_hash

        join = {
            "data": data,
            "fn_index": fn_index,
            "session_hash": h,
        }
        r = self.sm.post(f"{BASE_URL}/queue/join", json=join, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"join {r.status_code}: {r.text[:200]}")

        # some Gradio versions return event_id
        event_id = None
        try:
            event_id = r.json().get("event_id")
        except:
            pass

        sse_url = f"{BASE_URL}/queue/data?session_hash={h}"
        if event_id:
            sse_url += f"&event_id={event_id}"

        resp = self.sm.get_stream(
            sse_url,
            headers={**BROWSER_HEADERS, "Accept": "text/event-stream",
                     "Cache-Control": "no-cache"},
        )

        full = ""
        got_output = False

        for raw_line in resp.iter_lines(decode_unicode=True):
            if not raw_line:
                continue

            line = raw_line
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

            msg_type = ev.get("msg", "")

            if msg_type in ("process_generating", "process_completed"):
                text = self._dig_text(ev.get("output", {}))
                if text and len(text) > len(full):
                    delta = text[len(full):]
                    full = text
                    got_output = True
                    yield delta
                if msg_type == "process_completed":
                    break

            elif msg_type == "close_stream":
                break

            elif msg_type == "heartbeat":
                continue

        if not got_output:
            raise RuntimeError("SSE stream ended with no output")

    # ── method 2: direct /api/predict ──────────────────────
    def _chat_api_predict(self, message, model, history,
                          temperature, max_tokens, system_prompt):
        for fn in range(6):
            for data in self._payload_formats(message, model,
                    self._to_gradio_history(history),
                    temperature, max_tokens, system_prompt)[:3]:
                try:
                    r = self.sm.post(
                        f"{BASE_URL}/api/predict",
                        json={"data": data, "fn_index": fn,
                              "session_hash": self.sm.session_hash},
                        timeout=120,
                    )
                    if r.status_code == 200:
                        text = self._dig_text(r.json())
                        if text:
                            yield text
                            return
                except:
                    continue
        raise RuntimeError("predict API failed")

    # ── method 3: gradio_client lib ────────────────────────
    def _chat_gradio_client(self, message, model, history,
                             temperature, max_tokens, system_prompt):
        if GradioNativeClient is None:
            raise RuntimeError("gradio_client not installed")

        client = GradioNativeClient(BASE_URL, verbose=False)
        api_info = client.view_api(print_info=False, return_format="dict")

        # try every named endpoint
        for ep_name in list(api_info.get("named_endpoints", {}).keys())[:5]:
            try:
                result = client.predict(message, api_name=ep_name)
                text = result if isinstance(result, str) else str(result)
                if text and len(text) > 5:
                    yield text
                    return
            except:
                continue

        raise RuntimeError("gradio_client: no endpoint worked")

    # ── helpers ────────────────────────────────────────────
    @staticmethod
    def _to_gradio_history(messages):
        """Convert [{"role":"user","content":"..."},...] → [[user,bot],...]"""
        pairs = []
        i = 0
        while i < len(messages):
            user_msg = messages[i].get("content", "") if messages[i]["role"] == "user" else ""
            bot_msg = ""
            if i + 1 < len(messages) and messages[i + 1]["role"] == "assistant":
                bot_msg = messages[i + 1].get("content", "")
                i += 2
            else:
                i += 1
            if user_msg:
                pairs.append([user_msg, bot_msg])
        return pairs

    @staticmethod
    def _dig_text(obj, depth=0) -> str:
        """Recursively extract the assistant's text from nested Gradio output."""
        if depth > 8:
            return ""
        if isinstance(obj, str):
            return obj
        if isinstance(obj, dict):
            for key in ("value", "data", "output"):
                if key in obj:
                    found = LMArenaClient._dig_text(obj[key], depth + 1)
                    if found:
                        return found
            return ""
        if isinstance(obj, (list, tuple)):
            # Chatbot list: [[user, bot], [user, bot], ...]
            if (obj and isinstance(obj[-1], (list, tuple))
                    and len(obj[-1]) >= 2 and isinstance(obj[-1][1], str)):
                return obj[-1][1]
            # flat list of components
            for item in reversed(obj):
                found = LMArenaClient._dig_text(item, depth + 1)
                if found and len(found) > 2:
                    return found
        return ""


# ════════════════════════════════════════════════════════════
#  STREAMLIT UI
# ════════════════════════════════════════════════════════════

def _init():
    """One-time session-state bootstrap."""
    defaults = dict(
        ready=False,
        messages=[],
        sm=None,
        client=None,
        models=DEFAULT_MODELS[:],
        model=DEFAULT_MODELS[0],
        status="⚪ not connected",
        total=0,
    )
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _connect(manual_cookie: str = ""):
    """Build SessionManager + LMArenaClient."""
    sm = SessionManager(manual_cookie=manual_cookie)
    cl = LMArenaClient(sm)
    cl.fetch_config()

    st.session_state.sm = sm
    st.session_state.client = cl
    st.session_state.models = cl.models
    if st.session_state.model not in cl.models:
        st.session_state.model = cl.models[0]
    st.session_state.ready = True
    st.session_state.status = f"✅ connected via **{sm.method}**"


def _sidebar():
    with st.sidebar:
        st.title("⚙️ LMArena Client")
        st.caption(st.session_state.status)

        # ── connection ─────────────────────────────────
        with st.expander("🔌 Connection", expanded=not st.session_state.ready):
            if st.button("Connect / Reconnect", use_container_width=True):
                with st.spinner("Bypassing Cloudflare…"):
                    try:
                        _connect()
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed: {e}")

            st.markdown("**Manual cookie fallback**")
            st.caption(
                "If auto-bypass fails: open lmarena.ai in your browser → "
                "F12 → Application → Cookies → copy the `cf_clearance` value."
            )
            cookie = st.text_input("cf_clearance cookie", key="cookie_input",
                                    type="password", label_visibility="collapsed")
            if st.button("Connect with cookie", use_container_width=True) and cookie:
                with st.spinner("Connecting with cookie…"):
                    try:
                        _connect(manual_cookie=cookie.strip())
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed: {e}")

        st.divider()

        # ── model picker ──────────────────────────────
        st.session_state.model = st.selectbox(
            "🤖 Model",
            st.session_state.models,
            index=(st.session_state.models.index(st.session_state.model)
                   if st.session_state.model in st.session_state.models else 0),
        )

        # ── parameters ────────────────────────────────
        with st.expander("🎛️ Parameters"):
            temp = st.slider("Temperature", 0.0, 2.0, 0.7, 0.05)
            maxt = st.slider("Max tokens", 256, 16384, 4096, 256)
            sys_p = st.text_area("System prompt (optional)", height=80)
        st.divider()

        # ── actions ────────────────────────────────────
        col1, col2 = st.columns(2)
        with col1:
            if st.button("🗑️ Clear", use_container_width=True):
                st.session_state.messages = []
                st.rerun()
        with col2:
            if st.button("🔄 Refresh", use_container_width=True):
                if st.session_state.sm:
                    st.session_state.sm._build()
                    st.session_state.sm.new_hash()
                    st.session_state.status = f"✅ refreshed via **{st.session_state.sm.method}**"
                    st.rerun()

        st.caption(f"💬 {st.session_state.total} messages this session")

        # ── debug log ──────────────────────────────────
        if st.session_state.sm:
            with st.expander("📜 Connection log"):
                st.code("\n".join(st.session_state.sm.log[-30:]), language="text")

        # ── dependency health ──────────────────────────
        with st.expander("📦 Dependencies"):
            for name, obj, pkg in [
                ("curl_cffi", cffi_requests, "curl-cffi"),
                ("cloudscraper", cloudscraper, "cloudscraper"),
                ("gradio_client", GradioNativeClient, "gradio_client"),
            ]:
                icon = "✅" if obj else "❌"
                fix = "" if obj else f"  →  `pip install {pkg}`"
                st.markdown(f"{icon} `{name}`{fix}")

        return temp, maxt, sys_p


def _chat_area(temp, maxt, sys_p):
    """Main chat column."""

    # render history
    for m in st.session_state.messages:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

    # input
    prompt = st.chat_input("Type your message…")
    if not prompt:
        return

    if not st.session_state.ready:
        st.error("Connect first → click **Connect / Reconnect** in sidebar.")
        return

    # user bubble
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # assistant bubble (streaming)
    with st.chat_message("assistant"):
        placeholder = st.empty()
        full = ""
        client: LMArenaClient = st.session_state.client

        try:
            for chunk in client.chat(
                message=prompt,
                model=st.session_state.model,
                history=st.session_state.messages[:-1],   # exclude current user msg
                temperature=temp,
                max_tokens=maxt,
                system_prompt=sys_p,
            ):
                full += chunk
                placeholder.markdown(full + " ▌")

            placeholder.markdown(full)
        except Exception as e:
            err = f"❌ Error: {e}"
            placeholder.error(err)
            full = err

        st.session_state.messages.append({"role": "assistant", "content": full})
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

    st.markdown("""
    <style>
        section[data-testid="stSidebar"] > div { padding-top: .5rem; }
        .stChatMessage { max-width: 900px; }
        .block-container { max-width: 960px; padding-top: 1.5rem; }
    </style>
    """, unsafe_allow_html=True)

    _init()

    # auto-connect on first load
    if not st.session_state.ready:
        with st.spinner("🔌 First launch — connecting to LMArena…"):
            try:
                _connect()
            except Exception as e:
                st.warning(f"Auto-connect failed ({e}). Use the sidebar to connect manually.")

    temp, maxt, sys_p = _sidebar()
    _chat_area(temp, maxt, sys_p)


if __name__ == "__main__":
    main()
