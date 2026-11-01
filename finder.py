import json
import time
from urllib.parse import urlparse, parse_qs

import streamlit as st
from youtube_transcript_api import YouTubeTranscriptApi
from google import genai

# ---- ফিক্স করা সেটিংস (দরকার হলে এখানে বদলান) ----
MODEL = "gemini-3.8-flash"
MIN_LEN = 20
MAX_LEN = 60
NUM_CLIPS = 10
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
    return "\n".join(f"[{s.start:.0f}] {s.text.replace(chr(10), ' ')}" for s in fetched)


def find_moments(transcript, key):
    prompt = f"""You are an expert short-form video editor for motivational content.
From the timestamped transcript below, pick the {NUM_CLIPS} best moments that could go viral
as motivational Shorts/Reels/TikToks.

Rules:
- Each clip MUST be between {MIN_LEN} and {MAX_LEN} seconds long (end minus start).
- Start on a strong hook; end on a complete thought (never cut mid-sentence).
- Prefer powerful, quotable, emotional, or surprising motivational lines.
- Clips must not overlap.
- Use start/end in seconds based on the transcript timestamps.
- Write title, hook and why_viral in Bengali.

Return ONLY a JSON list of exactly {NUM_CLIPS} items:
[{{"start": 120, "end": 155, "title": "...", "hook": "...", "why_viral": "...", "score": 1-10}}]

Transcript:
{transcript}"""
    client = genai.Client(api_key=key)
    waits = [5, 10, 20, 40, 60]  # ব্যস্ত থাকলে এত সেকেন্ড অপেক্ষা করে আবার চেষ্টা
    for attempt in range(len(waits) + 1):
        try:
            r = client.models.generate_content(
                model=MODEL, contents=prompt,
                config={"response_mime_type": "application/json"},
            )
            return json.loads(r.text)
        except Exception as e:
            msg = str(e)
            temporary = any(c in msg for c in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED"))
            if not temporary or attempt == len(waits):
                raise
            st.toast(f"Gemini ব্যস্ত, {waits[attempt]} সেকেন্ড পর আবার চেষ্টা হচ্ছে...")
            time.sleep(waits[attempt])


if st.button("সেরা ১০টা ক্লিপ খুঁজুন", disabled=not (url and api_key)):
    vid = video_id(url)
    if not vid:
        st.error("লিঙ্কটি ঠিক মনে হচ্ছে না।")
        st.stop()
    try:
        if manual.strip():
            transcript = manual.strip()
        else:
            with st.spinner("ট্রান্সক্রিপ্ট আনা হচ্ছে..."):
                transcript = get_transcript(vid)
        with st.spinner("AI সেরা ক্লিপ খুঁজছে... (১-২ মিনিট লাগতে পারে)"):
            moments = find_moments(transcript, api_key)
    except Exception as e:
        st.error(f"সমস্যা হয়েছে: {e}")
        st.stop()

    moments = sorted(moments, key=lambda x: -x.get("score", 0))
    for i, m in enumerate(moments, 1):
        dur = int(m["end"] - m["start"])
        st.subheader(f"{i}. ⭐ {m.get('score', '?')}/10 — {m['title']}")
        st.write(f"**সময়:** {fmt(m['start'])} → {fmt(m['end'])} ({dur} সেকেন্ড)")
        st.write(f"**হুক:** {m.get('hook', '')}")
        st.write(f"**কেন ভাইরাল হতে পারে:** {m.get('why_viral', '')}")
        st.video(f"https://www.youtube.com/watch?v={vid}",
                 start_time=int(m["start"]), end_time=int(m["end"]))
        st.divider()
