import json
import re
import time
from urllib.parse import urlparse, parse_qs

import streamlit as st
from youtube_transcript_api import YouTubeTranscriptApi
from google import genai
from google.genai import types

# ---- ফিক্স করা সেটিংস (দরকার হলে এখানে বদলান) ----
DEFAULT_MODEL = "gemini-3.8-flash"
MIN_LEN = 20
MAX_LEN = 60
NUM_CLIPS = 10
CALL_TIMEOUT_SEC = 90    # প্রতি কলে সর্বোচ্চ অপেক্ষা
PART_LINES = 120         # ২০ সেকেন্ডের ব্লক × ১২০ = প্রায় ৪০ মিনিট প্রতি ভাগ
# ------------------------------------------------

st.set_page_config(page_title="Viral Clip Finder", page_icon="🔥")
st.title("🔥 Motivational Viral Clip Finder")

try:
    api_key = st.secrets["GEMINI_API_KEY"]
except Exception:
    api_key = ""
if not api_key:
    api_key = st.sidebar.text_input("Gemini API Key", type="password")

@st.cache_data(ttl=3600, show_spinner=False)
def list_models(key):
    """আপনার key-তে যে Flash মডেলগুলো আসলে চালু আছে তার তালিকা আনে।"""
    client = genai.Client(api_key=key)
    out = []
    for m in client.models.list():
        name = (m.name or "").replace("models/", "")
        actions = getattr(m, "supported_actions", None) or []
        ok = (not actions) or ("generateContent" in actions)
        bad = ("image", "tts", "live", "audio", "embedding", "robotics", "computer")
        if "flash" in name and ok and not any(x in name for x in bad):
            out.append(name)
    return sorted(set(out), reverse=True)


AVAILABLE = []
if api_key:
    try:
        AVAILABLE = list_models(api_key)
    except Exception:
        AVAILABLE = []
if AVAILABLE:
    idx = AVAILABLE.index(DEFAULT_MODEL) if DEFAULT_MODEL in AVAILABLE else 0
    MODEL = st.sidebar.selectbox("Gemini model", AVAILABLE, index=idx)
else:
    MODEL = st.sidebar.text_input("Gemini model (তালিকা আনা যায়নি, নাম নিজে দিন)", DEFAULT_MODEL)

url = st.text_input("YouTube লিঙ্ক পেস্ট করুন")
with st.expander("ট্রান্সক্রিপ্ট আনতে এরর হলে এখানে নিজে পেস্ট করুন (ঐচ্ছিক)"):
    manual = st.text_area(
        "YouTube-এ ভিডিওর নিচে ... > Show transcript খুলে সব লেখা কপি করে এখানে দিন",
        height=150,
    )


def video_id(u):
    p = urlparse(u)
    if p.hostname == "youtu.be":
        return p.path.lstrip("/")
    if p.path.startswith(("/shorts/", "/live/", "/embed/")):
        return p.path.split("/")[2]
    return parse_qs(p.query).get("v", [""])[0]


def fmt(sec):
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def get_transcript(vid):
    """যেকোনো ভাষার ট্রান্সক্রিপ্ট নেয়; মানুষের লেখা থাকলে সেটাই আগে।"""
    tl = list(YouTubeTranscriptApi().list(vid))
    tl.sort(key=lambda t: t.is_generated)
    fetched = tl[0].fetch()
    return [(int(s.start), s.text.replace("\n", " ")) for s in fetched]


TS = re.compile(r"^\[?(\d+):(\d{2})(?::(\d{2}))?\]?\s*(.*)$")


def parse_manual(text):
    """নিজে পেস্ট করা ট্রান্সক্রিপ্ট থেকে (সেকেন্ড, লেখা) তালিকা বানায়।"""
    items, pending = [], None
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        m = TS.match(ln)
        if m:
            a, b, c, rest = m.groups()
            sec = int(a) * 3600 + int(b) * 60 + int(c) if c else int(a) * 60 + int(b)
            if rest:
                items.append((sec, rest))
                pending = None
            else:
                pending = sec
        elif pending is not None:
            items.append((pending, ln))
            pending = None
        elif items:
            items[-1] = (items[-1][0], items[-1][1] + " " + ln)
    return items


def compress(items, bucket=20):
    """প্রতি ২০ সেকেন্ডের লেখা এক লাইনে জুড়ে দেয়, যাতে AI-কে কম পড়তে হয়।"""
    out, cur, txt = [], None, []
    for sec, t in items:
        b = sec // bucket * bucket
        if b != cur:
            if txt:
                out.append(f"[{cur}] " + " ".join(txt))
            cur, txt = b, []
        txt.append(t)
    if txt:
        out.append(f"[{cur}] " + " ".join(txt))
    return "\n".join(out)


def find_moments(transcript, key, count, live, model):
    prompt = f"""You are an expert short-form video editor for motivational content.
From the timestamped transcript below, pick the {count} best moments that could go viral
as motivational Shorts/Reels/TikToks.

Rules:
- Each clip MUST be between {MIN_LEN} and {MAX_LEN} seconds long (end minus start).
- Start on a strong hook; end on a complete thought (never cut mid-sentence).
- Prefer powerful, quotable, emotional, or surprising motivational lines.
- Clips must not overlap.
- Use start/end in seconds based on the transcript timestamps.
- Write title, hook and why_viral in Bengali.

Return ONLY a JSON list of {count} items:
[{{"start": 120, "end": 155, "title": "...", "hook": "...", "why_viral": "...", "score": 1-10}}]

Transcript:
{transcript}"""
    client = genai.Client(
        api_key=key,
        http_options=types.HttpOptions(timeout=CALL_TIMEOUT_SEC * 1000),
    )
    use_thinking = True      # "চিন্তা" কম করার সেটিং; মডেল না নিলে বাদ দেওয়া হবে
    waits = [3, 6, 12]
    attempt = 0
    while True:
        try:
            cfg = None
            if use_thinking:
                try:
                    cfg = types.GenerateContentConfig(
                        response_mime_type="application/json",
                        thinking_config=types.ThinkingConfig(thinking_level="low"),
                    )
                except Exception:
                    use_thinking = False
            if cfg is None:
                cfg = types.GenerateContentConfig(response_mime_type="application/json")
            text, t1 = "", time.time()
            live.write(f"📤 {model}-কে পাঠানো হয়েছে, উত্তরের অপেক্ষা...")
            for ch in client.models.generate_content_stream(model=model, contents=prompt, config=cfg):
                if ch.text:
                    text += ch.text
                    live.write(f"📥 {model}: উত্তর আসছে... {len(text)} অক্ষর ({int(time.time() - t1)} সেকেন্ড)")
            return json.loads(text)
        except Exception as e:
            msg = str(e).lower()
            if use_thinking and ("thinking" in msg or "invalid_argument" in msg):
                use_thinking = False
                continue
            temporary = isinstance(e, json.JSONDecodeError) or any(c in msg for c in (
                "503", "429", "unavailable", "resource_exhausted"))
            if not temporary or attempt >= len(waits):
                raise
            live.write(f"⚠️ সমস্যা ({str(e)[:80]}...) — {waits[attempt]} সেকেন্ড পর আবার চেষ্টা")
            time.sleep(waits[attempt])
            attempt += 1


if st.button("সেরা ১০টা ক্লিপ খুঁজুন", disabled=not (url and api_key)):
    vid = video_id(url)
    if not vid:
        st.error("লিঙ্কটি ঠিক মনে হচ্ছে না।")
        st.stop()
    try:
        if manual.strip():
            items = parse_manual(manual)
            transcript = compress(items) if items else manual.strip()
        else:
            with st.spinner("ট্রান্সক্রিপ্ট আনা হচ্ছে..."):
                transcript = compress(get_transcript(vid))
    except Exception as e:
        st.error(f"ট্রান্সক্রিপ্টে সমস্যা: {e}")
        st.stop()

    lines = transcript.splitlines()
    parts = ["\n".join(lines[i:i + PART_LINES]) for i in range(0, len(lines), PART_LINES)] or [transcript]
    per_part = max(3, -(-NUM_CLIPS // len(parts)) + 1)

    candidates = [MODEL] + [m for m in AVAILABLE if m != MODEL][:2]
    t0 = time.time()
    bar = st.progress(0.0, text="শুরু হচ্ছে...")
    live = st.empty()
    moments, failed = [], 0
    for k, part in enumerate(parts, 1):
        bar.progress((k - 1) / len(parts),
                     text=f"ভাগ {k}/{len(parts)} বিশ্লেষণ হচ্ছে... ({int(time.time() - t0)} সেকেন্ড গেছে)")
        done, last_err = False, None
        for mdl in candidates:
            try:
                moments += find_moments(part, api_key, per_part, live, mdl)
                candidates.remove(mdl)
                candidates.insert(0, mdl)   # যে মডেল কাজ করল সেটাই আগে
                done = True
                break
            except Exception as e:
                last_err = e
                st.toast(f"{mdl} কাজ করেনি, পরের মডেল চেষ্টা হচ্ছে...")
        if not done:
            failed += 1
            st.warning(f"ভাগ {k} ব্যর্থ হয়েছে: {last_err}")
    live.empty()
    bar.progress(1.0, text=f"শেষ! মোট {int(time.time() - t0)} সেকেন্ড লেগেছে")

    moments = [m for m in moments if "start" in m and "end" in m]
    if not moments:
        st.error("কোনো ক্লিপ পাওয়া যায়নি। কিছুক্ষণ পর আবার চেষ্টা করুন।")
        st.stop()
    if failed:
        st.info(f"{failed}টা ভাগ ব্যর্থ হওয়ায় কিছু অংশের ক্লিপ নেই। আবার চেষ্টা করলে বাকিটাও পেতে পারেন।")

    moments = sorted(moments, key=lambda x: -x.get("score", 0))[:NUM_CLIPS]
    for i, m in enumerate(moments, 1):
        dur = int(m["end"] - m["start"])
        st.subheader(f"{i}. ⭐ {m.get('score', '?')}/10 — {m.get('title', '')}")
        st.write(f"**সময়:** {fmt(m['start'])} → {fmt(m['end'])} ({dur} সেকেন্ড)")
        st.write(f"**হুক:** {m.get('hook', '')}")
        st.write(f"**কেন ভাইরাল হতে পারে:** {m.get('why_viral', '')}")
        st.video(f"https://www.youtube.com/watch?v={vid}",
                 start_time=int(m["start"]), end_time=int(m["end"]))
        cmd = (
            f'yt-dlp -f "bv*[height<=1080]+ba/b" --merge-output-format mp4 '
            f'--download-sections "*{int(m["start"])}-{int(m["end"])}" '
            f'--force-keyframes-at-cuts -o "clip_{i}.%(ext)s" '
            f'"https://www.youtube.com/watch?v={vid}"'
        )
        with st.expander("⬇️ এই ক্লিপ ডাউনলোডের কমান্ড (নিজের কম্পিউটারে চালান)"):
            st.code(cmd, language="bash")
        st.divider()
