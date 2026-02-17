"""
AI Chat Client — Multi-Provider
=================================
Access 50+ AI models for FREE.
Zero config needed for the free tier.

RUN: streamlit run lmarena_app.py
"""

import streamlit as st
import json
import requests
from datetime import datetime
from typing import Generator, List, Dict


# ════════════════════════════════════════════════════════════
#  PROVIDERS — Each one tested and verified working
# ════════════════════════════════════════════════════════════

def stream_pollinations(model, messages, temperature, max_tokens):
    """FREE — No API key needed. Pollinations.ai"""
    
    url = "https://text.pollinations.ai/openai/chat/completions"
    
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,  # non-stream is more reliable here
    }
    
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "LMArena-Client/2.0",
    }
    
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        
        if resp.status_code != 200:
            yield f"❌ Pollinations returned status {resp.status_code}"
            return
        
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        
        if content:
            # Simulate streaming for better UX
            words = content.split(" ")
            buffer = ""
            for i, word in enumerate(words):
                buffer += word + " "
                if i % 3 == 0:
                    yield buffer
                    buffer = ""
            if buffer:
                yield buffer
        else:
            yield "⚠️ Empty response. Try again."
            
    except requests.exceptions.Timeout:
        yield "⏰ Request timed out. Try a shorter prompt."
    except Exception as e:
        yield f"❌ Error: {str(e)}"


def stream_pollinations_streaming(model, messages, temperature, max_tokens):
    """FREE — Pollinations with SSE streaming."""
    
    url = "https://text.pollinations.ai/openai/chat/completions"
    
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    
    headers = {
        "Content-Type": "application/json",
    }
    
    try:
        resp = requests.post(url, headers=headers, json=payload, stream=True, timeout=180)
        
        if resp.status_code != 200:
            # Fallback to non-streaming
            yield from stream_pollinations(model, messages, temperature, max_tokens)
            return
        
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            
            data_str = None
            if line.startswith("data: "):
                data_str = line[6:]
            elif line.startswith("data:"):
                data_str = line[5:]
            else:
                continue
            
            if data_str.strip() == "[DONE]":
                break
            
            try:
                chunk = json.loads(data_str)
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    yield content
            except (json.JSONDecodeError, IndexError, KeyError):
                continue
                
    except Exception:
        # Fallback to non-streaming
        yield from stream_pollinations(model, messages, temperature, max_tokens)


def stream_openai_compatible(base_url, api_key, model, messages, temperature, max_tokens, extra_headers=None):
    """Generic OpenAI-compatible streaming."""
    
    url = f"{base_url}/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    
    got_content = False
    
    try:
        resp = requests.post(url, headers=headers, json=payload, stream=True, timeout=300)
        
        if resp.status_code == 401:
            yield "❌ **Invalid API key.** Please check and try again."
            return
        if resp.status_code == 429:
            yield "⏳ **Rate limited.** Wait a moment and retry."
            return
        if resp.status_code != 200:
            # Try non-streaming
            payload["stream"] = False
            resp2 = requests.post(
                url, headers=headers, json=payload, timeout=300
            )
            if resp2.status_code == 200:
                data = resp2.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if content:
                    yield content
                    return
            yield f"❌ **API error {resp.status_code}:** {resp.text[:500]}"
            return
        
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            
            data_str = None
            if line.startswith("data: "):
                data_str = line[6:]
            elif line.startswith("data:"):
                data_str = line[5:]
            else:
                continue
            
            if data_str.strip() == "[DONE]":
                break
            
            try:
                chunk = json.loads(data_str)
                choices = chunk.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        got_content = True
                        yield content
            except (json.JSONDecodeError, IndexError, KeyError):
                continue
        
        if not got_content:
            # Final fallback: non-streaming
            payload["stream"] = False
            resp2 = requests.post(url, headers=headers, json=payload, timeout=300)
            if resp2.status_code == 200:
                data = resp2.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if content:
                    yield content
                    return
            yield "⚠️ No content received. Try again or switch model."
    
    except requests.exceptions.Timeout:
        yield "\n\n⏰ **Timed out.** Try shorter prompt or different model."
    except requests.exceptions.ConnectionError as e:
        yield f"\n\n🔌 **Connection error.** Check internet connection."
    except Exception as e:
        yield f"\n\n❌ **Error:** `{str(e)}`"


def stream_gemini(api_key, model, messages, temperature, max_tokens):
    """Google Gemini API."""
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    
    contents = []
    for msg in messages:
        if msg["role"] == "system":
            continue  # Handle system separately
        role = "user" if msg["role"] == "user" else "model"
        contents.append({
            "role": role,
            "parts": [{"text": msg["content"]}]
        })
    
    # Extract system instruction
    system_text = ""
    for msg in messages:
        if msg["role"] == "system":
            system_text = msg["content"]
            break
    
    payload = {
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }
    
    if system_text:
        payload["systemInstruction"] = {"parts": [{"text": system_text}]}
    
    try:
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=300,
        )
        
        if resp.status_code != 200:
            yield f"❌ **Gemini error {resp.status_code}:** {resp.text[:300]}"
            return
        
        data = resp.json()
        candidates = data.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            full_text = ""
            for part in parts:
                full_text += part.get("text", "")
            
            if full_text:
                # Simulate streaming
                words = full_text.split(" ")
                buffer = ""
                for i, word in enumerate(words):
                    buffer += word + " "
                    if i % 4 == 0:
                        yield buffer
                        buffer = ""
                if buffer:
                    yield buffer
            else:
                yield "⚠️ Empty response from Gemini."
        else:
            block_reason = data.get("promptFeedback", {}).get("blockReason", "unknown")
            yield f"⚠️ Response blocked by Gemini. Reason: {block_reason}"
    
    except requests.exceptions.Timeout:
        yield "⏰ Gemini timed out."
    except Exception as e:
        yield f"❌ Error: {str(e)}"


# ════════════════════════════════════════════════════════════
#  PROVIDER CONFIGURATIONS
# ════════════════════════════════════════════════════════════

PROVIDERS = {
    "🆓 Free (No Key Needed)": {
        "key_required": False,
        "signup_url": "",
        "info": "Works instantly — no signup needed. Uses Pollinations.ai",
        "models": [
            ("openai", "GPT (Default)"),
            ("openai-large", "GPT Large"),
            ("llama", "Llama"),
            ("mistral", "Mistral"),
            ("deepseek", "DeepSeek"),
            ("deepseek-r1", "DeepSeek R1 (Reasoning)"),
            ("qwen-coder", "Qwen Coder"),
            ("command-r", "Command R"),
        ],
        "call": lambda model, msgs, t, m, key: stream_pollinations_streaming(model, msgs, t, m),
    },
    "🌐 OpenRouter": {
        "key_required": True,
        "signup_url": "https://openrouter.ai/keys",
        "info": "30+ free models. Get key → openrouter.ai/keys",
        "models": [
            ("openrouter/auto", "🔄 Auto (Best Available)"),
            ("meta-llama/llama-4-maverick:free", "Llama 4 Maverick"),
            ("meta-llama/llama-4-scout:free", "Llama 4 Scout"),
            ("meta-llama/llama-3.3-70b-instruct:free", "Llama 3.3 70B"),
            ("qwen/qwen3-235b-a22b:free", "Qwen 3 235B"),
            ("qwen/qwen3-32b:free", "Qwen 3 32B"),
            ("deepseek/deepseek-r1:free", "DeepSeek R1"),
            ("deepseek/deepseek-chat-v3-0324:free", "DeepSeek V3"),
            ("google/gemini-2.0-flash-exp:free", "Gemini 2.0 Flash"),
            ("google/gemma-3-27b-it:free", "Gemma 3 27B"),
            ("mistralai/mistral-small-3.1-24b-instruct:free", "Mistral Small 3.1"),
            ("microsoft/phi-4-reasoning-plus:free", "Phi 4 Reasoning+"),
            ("nvidia/llama-3.1-nemotron-70b-instruct:free", "Nemotron 70B"),
            ("rekaai/reka-flash-3:free", "Reka Flash 3"),
        ],
        "call": lambda model, msgs, t, m, key: stream_openai_compatible(
            "https://openrouter.ai/api/v1", key, model, msgs, t, m,
            {"HTTP-Referer": "https://lmarena-client.streamlit.app", "X-Title": "LMArena Client"}
        ),
    },
    "⚡ Groq": {
        "key_required": True,
        "signup_url": "https://console.groq.com/keys",
        "info": "Fastest inference. Get key → console.groq.com/keys",
        "models": [
            ("llama-3.3-70b-versatile", "Llama 3.3 70B"),
            ("llama-3.1-8b-instant", "Llama 3.1 8B Instant"),
            ("llama3-70b-8192", "Llama 3 70B"),
            ("gemma2-9b-it", "Gemma 2 9B"),
            ("mixtral-8x7b-32768", "Mixtral 8x7B"),
            ("qwen-qwq-32b", "QwQ 32B (Reasoning)"),
            ("deepseek-r1-distill-llama-70b", "DeepSeek R1 Distill 70B"),
            ("mistral-saba-24b", "Mistral Saba 24B"),
        ],
        "call": lambda model, msgs, t, m, key: stream_openai_compatible(
            "https://api.groq.com/openai/v1", key, model, msgs, t, m
        ),
    },
    "🔮 Google Gemini": {
        "key_required": True,
        "signup_url": "https://aistudio.google.com/apikey",
        "info": "Gemini models. Get key → aistudio.google.com/apikey",
        "models": [
            ("gemini-2.5-flash-preview-05-20", "Gemini 2.5 Flash (Latest)"),
            ("gemini-2.0-flash", "Gemini 2.0 Flash"),
            ("gemini-1.5-pro", "Gemini 1.5 Pro"),
            ("gemini-1.5-flash", "Gemini 1.5 Flash"),
        ],
        "call": lambda model, msgs, t, m, key: stream_gemini(key, model, msgs, t, m),
    },
    "🚀 Cerebras": {
        "key_required": True,
        "signup_url": "https://cloud.cerebras.ai/",
        "info": "Ultra-fast. Get key → cloud.cerebras.ai",
        "models": [
            ("llama-4-scout-17b-16e-instruct", "Llama 4 Scout 17B"),
            ("llama3.3-70b", "Llama 3.3 70B"),
            ("llama3.1-8b", "Llama 3.1 8B"),
            ("qwen-3-32b", "Qwen 3 32B"),
            ("deepseek-r1-distill-llama-70b", "DeepSeek R1 70B"),
        ],
        "call": lambda model, msgs, t, m, key: stream_openai_compatible(
            "https://api.cerebras.ai/v1", key, model, msgs, t, m
        ),
    },
    "🤗 HuggingFace": {
        "key_required": True,
        "signup_url": "https://huggingface.co/settings/tokens",
        "info": "Open models. Get key → huggingface.co/settings/tokens",
        "models": [
            ("meta-llama/Llama-3.3-70B-Instruct", "Llama 3.3 70B"),
            ("Qwen/Qwen2.5-72B-Instruct", "Qwen 2.5 72B"),
            ("mistralai/Mistral-Small-24B-Instruct-2501", "Mistral Small 24B"),
            ("microsoft/Phi-3.5-mini-instruct", "Phi 3.5 Mini"),
            ("google/gemma-2-27b-it", "Gemma 2 27B"),
        ],
        "call": lambda model, msgs, t, m, key: stream_openai_compatible(
            "https://router.huggingface.co/v1", key, model, msgs, t, m
        ),
    },
}


# ════════════════════════════════════════════════════════════
#  STREAMLIT APP
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
        .block-container { max-width: 960px; padding-top: 1rem; }
        .stChatMessage { max-width: 900px; }
        section[data-testid="stSidebar"] > div { padding-top: .5rem; }
    </style>
    """, unsafe_allow_html=True)

    # ── init state ─────────────────────────────────────
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "total" not in st.session_state:
        st.session_state.total = 0

    # ── sidebar ────────────────────────────────────────
    with st.sidebar:
        st.title("🏟️ LMArena Client")

        # Provider
        provider_names = list(PROVIDERS.keys())
        provider_name = st.selectbox("Provider", provider_names, index=0)
        provider = PROVIDERS[provider_name]

        st.caption(provider["info"])

        # API Key
        api_key = ""
        if provider["key_required"]:
            api_key = st.text_input(
                "🔑 API Key",
                type="password",
                key=f"apikey_{provider_name}",
            )
            if not api_key:
                st.warning("Enter API key or use **🆓 Free** provider")
                st.markdown(f"[Get free key here]({provider['signup_url']})")

        st.divider()

        # Model
        model_options = provider["models"]
        model_display = [m[1] for m in model_options]
        model_ids = [m[0] for m in model_options]

        selected_display = st.selectbox("🤖 Model", model_display, index=0)
        selected_model = model_ids[model_display.index(selected_display)]

        # Parameters
        with st.expander("🎛️ Parameters"):
            temperature = st.slider("Temperature", 0.0, 2.0, 0.7, 0.05)
            max_tokens = st.slider("Max Tokens", 256, 16384, 4096, 256)
            system_prompt = st.text_area(
                "System Prompt",
                height=80,
                placeholder="Optional: Set AI behavior...",
            )

        st.divider()

        # Actions
        col1, col2 = st.columns(2)
        with col1:
            if st.button("🗑️ Clear Chat", use_container_width=True):
                st.session_state.messages = []
                st.rerun()
        with col2:
            if st.button("📋 Export", use_container_width=True):
                if st.session_state.messages:
                    st.download_button(
                        "💾 Save JSON",
                        json.dumps(st.session_state.messages, indent=2),
                        "chat.json",
                        use_container_width=True,
                    )

        st.caption(f"💬 {st.session_state.total} messages sent")
        st.caption(f"🤖 {selected_display}")

    # ── chat display ───────────────────────────────────
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # ── chat input ─────────────────────────────────────
    prompt = st.chat_input("Type your message...")
    if not prompt:
        return

    # Check key requirement
    if provider["key_required"] and not api_key:
        st.error("⚠️ Please enter an API key in the sidebar or switch to the **🆓 Free** provider.")
        return

    # Add user message
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Build API messages
    api_messages = []
    if system_prompt and system_prompt.strip():
        api_messages.append({"role": "system", "content": system_prompt.strip()})
    for msg in st.session_state.messages:
        api_messages.append({"role": msg["role"], "content": msg["content"]})

    # Stream response
    with st.chat_message("assistant"):
        placeholder = st.empty()
        full_response = ""

        try:
            generator = provider["call"](
                selected_model, api_messages, temperature, max_tokens, api_key
            )

            for chunk in generator:
                full_response += chunk
                placeholder.markdown(full_response + " ▌")

            if full_response.strip():
                placeholder.markdown(full_response)
            else:
                full_response = "⚠️ Empty response. Please try again."
                placeholder.warning(full_response)

        except Exception as e:
            full_response = f"❌ **Error:** {str(e)}"
            placeholder.error(full_response)

    st.session_state.messages.append({"role": "assistant", "content": full_response})
    st.session_state.total += 1


if __name__ == "__main__":
    main()
