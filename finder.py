import json
import re
import time
from urllib.parse import urlparse, parse_qs

import streamlit as st
from youtube_transcript_api import YouTubeTranscriptApi
from google import genai
from google.genai import types

# ---- ফিক্স করা সেটিংস (দরকার হলে এখানে বদলান) ----
MODEL = "gemini-3.8-flash"
MIN_LEN = 20
MAX_LEN = 60
NUM_CLIPS = 10
CALL_TIMEOUT_SEC = 120   # প্রতি কলে সর্বোচ্চ অপেক্ষা
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


def find_moments(transcript, key, count):
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
    waits = [3, 6, 12]  # সাময়িক সমস্যায় এত সেকেন্ড অপেক্ষা করে আবার চেষ্টা
    for attempt in range(len(waits) + 1):
        try:
            r = client.models.generate_content(
                model=MODEL, contents=prompt,
                config={"response_mime_type": "application/json"},
            )
            return json.loads(r.text)
        except Exception as e:
            msg = str(e)
            temporary = any(c in msg.lower() for c in (
                "503", "429", "504", "unavailable", "resource_exhausted",
                "deadline", "timeout", "timed out"))
            if not temporary or attempt == len(waits):
                raise
            st.toast(f"Gemini ব্যস্ত/ধীর, {waits[attempt]} সেকেন্ড পর আবার চেষ্টা...")
            time.sleep(waits[attempt])


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

    t0 = time.time()
    bar = st.progress(0.0, text="শুরু হচ্ছে...")
    moments, failed = [], 0
    for k, part in enumerate(parts, 1):
        bar.progress((k - 1) / len(parts),
                     text=f"ভাগ {k}/{len(parts)} বিশ্লেষণ হচ্ছে... ({int(time.time() - t0)} সেকেন্ড গেছে)")
        try:
            moments += find_moments(part, api_key, per_part)
        except Exception as e:
            failed += 1
            st.warning(f"ভাগ {k} ব্যর্থ হয়েছে: {e}")
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
