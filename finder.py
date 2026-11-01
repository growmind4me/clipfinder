import json
from urllib.parse import urlparse, parse_qs

import streamlit as st
from youtube_transcript_api import YouTubeTranscriptApi
from google import genai

st.set_page_config(page_title="Viral Moment Finder", page_icon="🔥")
st.title("🔥 Viral Moment Finder")

api_key = st.sidebar.text_input("Gemini API Key", type="password")
model_name = st.sidebar.text_input("Gemini model", "gemini-2.5-flash")
langs = st.sidebar.text_input("Transcript languages (comma separated)", "bn,en")
clip_len = st.sidebar.slider("Clip length (seconds)", 15, 90, 35)
num_clips = st.sidebar.slider("How many moments", 1, 10, 5)

url = st.text_input("YouTube লিঙ্ক")
with st.expander("ট্রান্সক্রিপ্ট আনতে এরর হলে এখানে নিজে পেস্ট করুন (ঐচ্ছিক)"):
    manual = st.text_area(
        "YouTube-এ ভিডিওর নিচে ... > Show transcript খুলে সব লেখা কপি করে এখানে দিন",
        height=150,
    )


def video_id(u):
    p = urlparse(u)
    if p.hostname in ("youtu.be",):
        return p.path.lstrip("/")
    if p.path.startswith(("/shorts/", "/live/", "/embed/")):
        return p.path.split("/")[2]
    return parse_qs(p.query).get("v", [""])[0]


def fmt(sec):
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def get_transcript(vid, languages):
    api = YouTubeTranscriptApi()
    fetched = api.fetch(vid, languages=languages)
    return [(s.start, s.text.replace("\n", " ")) for s in fetched]


def find_moments(transcript, key, model, length, count):
    prompt = f"""From this timestamped transcript, pick the {count} moments most likely to go
viral as short clips (strong hook, emotional or surprising, complete thought, ~{length}s each).
Use start/end in seconds based on the transcript timestamps.
Return ONLY a JSON list:
[{{"start": 120, "end": 155, "title": "...", "hook": "...", "why_viral": "...", "score": 1-10}}]

Transcript:
{transcript}"""
    client = genai.Client(api_key=key)
    r = client.models.generate_content(
        model=model, contents=prompt,
        config={"response_mime_type": "application/json"},
    )
    return json.loads(r.text)


if st.button("ভাইরাল অংশ খুঁজুন", disabled=not (url and api_key)):
    vid = video_id(url)
    if not vid:
        st.error("লিঙ্কটি ঠিক মনে হচ্ছে না।")
        st.stop()
    try:
        if manual.strip():
            transcript = manual.strip()
        else:
            with st.spinner("ট্রান্সক্রিপ্ট আনা হচ্ছে..."):
                lines = get_transcript(vid, [l.strip() for l in langs.split(",")])
            transcript = "\n".join(f"[{t:.0f}] {x}" for t, x in lines)
        with st.spinner("AI বিশ্লেষণ করছে..."):
            moments = find_moments(transcript, api_key, model_name, clip_len, num_clips)
    except Exception as e:
        st.error(f"সমস্যা হয়েছে: {e}")
        st.stop()

    for m in sorted(moments, key=lambda x: -x.get("score", 0)):
        st.subheader(f"⭐ {m.get('score', '?')}/10 — {m['title']}")
        st.write(f"**সময়:** {fmt(m['start'])} → {fmt(m['end'])}")
        st.write(f"**হুক:** {m.get('hook', '')}")
        st.write(f"**কেন ভাইরাল হতে পারে:** {m.get('why_viral', '')}")
        st.video(f"https://www.youtube.com/watch?v={vid}",
                 start_time=int(m["start"]), end_time=int(m["end"]))
        st.divider()
