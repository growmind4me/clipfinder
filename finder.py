import json
import re
import time
from urllib.parse import urlparse, parse_qs

import streamlit as st
from youtube_transcript_api import YouTubeTranscriptApi
from google import genai
from google.genai import types

# ---- Fixed settings (edit here if needed) ----
DEFAULT_MODEL = "gemini-3.8-flash"
MIN_LEN = 20
MAX_LEN = 60
NUM_CLIPS = 10
CALL_TIMEOUT_SEC = 90    # max wait per Gemini call
PART_LINES = 600         # 20-sec blocks x 600 = ~200 min per request (most videos = 1 request)
# ----------------------------------------------

st.set_page_config(page_title="Viral Clip Finder", page_icon="🔥")
st.title("🔥 Motivational Viral Clip Finder")
st.caption("ভাইরাল মোটিভেশনাল ক্লিপ ফাইন্ডার")

try:
    api_key = st.secrets["GEMINI_API_KEY"]
except Exception:
    api_key = ""
if not api_key:
    api_key = st.sidebar.text_input("Gemini API Key", type="password")


@st.cache_data(ttl=3600, show_spinner=False)
def list_models(key):
    """Lists the Flash models actually enabled for this API key."""
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
    MODEL = st.sidebar.text_input("Gemini model (list unavailable, type a name)", DEFAULT_MODEL)

url = st.text_input("Paste YouTube link / YouTube লিঙ্ক পেস্ট করুন")
with st.expander("If fetching the transcript fails, paste it here (optional) / ট্রান্সক্রিপ্ট আনতে এরর হলে এখানে নিজে পেস্ট করুন (ঐচ্ছিক)"):
    manual = st.text_area(
        "On YouTube: ... under the video > Show transcript, copy everything / YouTube-এ ভিডিওর নিচে ... > Show transcript খুলে সব লেখা কপি করে এখানে দিন",
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
    """Fetches a transcript in any language; human-written first."""
    tl = list(YouTubeTranscriptApi().list(vid))
    tl.sort(key=lambda t: t.is_generated)
    fetched = tl[0].fetch()
    return [(int(s.start), s.text.replace("\n", " ")) for s in fetched]


TS = re.compile(r"^\[?(\d+):(\d{2})(?::(\d{2}))?\]?\s*(.*)$")


def parse_manual(text):
    """Turns a pasted transcript into a list of (seconds, text)."""
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
    """Merges each 20-second block into one line so the AI reads less."""
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


def short_err(e):
    """One-line summary of a long API error."""
    s = str(e)
    if "429" in s or "RESOURCE_EXHAUSTED" in s:
        if re.search(r"limit: 0(?!\d)", s):
            return "no free-tier quota for this model (limit 0)"
        return "quota exceeded (429)"
    if "404" in s or "NOT_FOUND" in s:
        return "model not found (404)"
    if "503" in s or "UNAVAILABLE" in s:
        return "model is busy (503)"
    return s[:150]


def find_moments(transcript, key, count, live, model):
    prompt = f"""You are an expert short-form video editor for motivational content.
From the timestamped transcript below, pick the {count} best moments that could go viral
as motivational Shorts/Reels/TikToks.

Rules:
- Each clip MUST be between {MIN_LEN} and {MAX_LEN} seconds long (end minus start). Never exceed {MAX_LEN}.
- Start at the beginning of a sentence with a strong hook; end on a complete thought
  (never cut mid-sentence).
- Prefer powerful, quotable, emotional, or surprising motivational lines.
- Clips must not overlap.
- Use start/end in seconds based on the transcript timestamps.
- opening_line = the exact first sentence spoken in the clip, copied word for word from the transcript.
- closing_line = the exact last sentence spoken in the clip, copied word for word from the transcript.
- Write title, hook and why_viral in English, and give a natural Bengali translation of each
  in title_bn, hook_bn and why_viral_bn.
- Also give a natural Bengali translation of opening_line and closing_line in opening_line_bn
  and closing_line_bn (the English/original lines stay exactly as spoken).

Return ONLY a JSON list of {count} items:
[{{"start": 120, "end": 155, "title": "...", "title_bn": "...", "opening_line": "...", "opening_line_bn": "...", "closing_line": "...", "closing_line_bn": "...", "hook": "...", "hook_bn": "...", "why_viral": "...", "why_viral_bn": "...", "score": 1-10}}]

Transcript:
{transcript}"""
    client = genai.Client(
        api_key=key,
        http_options=types.HttpOptions(timeout=CALL_TIMEOUT_SEC * 1000),
    )
    use_thinking = True      # low "thinking" setting; dropped if the model rejects it
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
            live.write(f"📤 Sent to {model}, waiting for a reply...")
            for ch in client.models.generate_content_stream(model=model, contents=prompt, config=cfg):
                if ch.text:
                    text += ch.text
                    live.write(f"📥 {model}: receiving reply... {len(text)} chars ({int(time.time() - t1)}s)")
            return json.loads(text)
        except Exception as e:
            msg = str(e).lower()
            if use_thinking and ("thinking" in msg or "invalid_argument" in msg):
                use_thinking = False
                continue
            if "429" in msg or "resource_exhausted" in msg:
                norm = re.sub(r"[^a-z0-9.]", "", msg)
                if "perday" in norm or re.search(r"limit0(?!\d)", norm):
                    raise   # daily quota gone or no free quota: waiting is pointless
                mm = re.search(r"retryin([\d.]+)s", norm) or re.search(r"retrydelay([\d.]+)s", norm)
                delay = float(mm.group(1)) if mm else 30
                if delay <= 70 and attempt < 2:
                    live.write(f"⏳ Rate limit, Google asks to wait {int(delay)}s...")
                    time.sleep(delay + 1)
                    attempt += 1
                    continue
                raise
            temporary = isinstance(e, json.JSONDecodeError) or any(c in msg for c in ("503", "unavailable"))
            if not temporary or attempt >= len(waits):
                raise
            live.write(f"⚠️ Problem ({str(e)[:80]}...). Retrying in {waits[attempt]}s")
            time.sleep(waits[attempt])
            attempt += 1


def show(label_en, label_bn, en, bn, quote=False):
    q = (lambda t: f"“{t}”") if quote else (lambda t: t)
    st.markdown(f"**{label_en} / {label_bn}:** {q(en)}")
    if bn:
        st.write(f"↳ {q(bn)}")


if st.button("Find the best 10 clips / সেরা ১০টা ক্লিপ খুঁজুন", disabled=not (url and api_key)):
    vid = video_id(url)
    if not vid:
        st.error("That doesn't look like a valid YouTube link. / লিঙ্কটি ঠিক মনে হচ্ছে না।")
        st.stop()
    try:
        if manual.strip():
            items = parse_manual(manual)
            transcript = compress(items) if items else manual.strip()
        else:
            with st.spinner("Fetching transcript... / ট্রান্সক্রিপ্ট আনা হচ্ছে..."):
                transcript = compress(get_transcript(vid))
    except Exception as e:
        st.error(f"Transcript problem: {e}")
        st.stop()

    lines = transcript.splitlines()
    parts = ["\n".join(lines[i:i + PART_LINES]) for i in range(0, len(lines), PART_LINES)] or [transcript]
    per_part = max(3, -(-NUM_CLIPS // len(parts)) + 3)   # a few extras, some get filtered out

    bad = st.session_state.setdefault("bad_models", set())
    candidates = [MODEL] + [m for m in AVAILABLE if m != MODEL]
    candidates = [m for m in candidates if m not in bad] or candidates
    candidates = candidates[:6]

    t0 = time.time()
    bar = st.progress(0.0, text="Starting...")
    live = st.empty()
    moments, failed, quota_hit = [], 0, False
    for k, part in enumerate(parts, 1):
        bar.progress((k - 1) / len(parts),
                     text=f"Analyzing part {k}/{len(parts)}... / বিশ্লেষণ চলছে... ({int(time.time() - t0)}s)")
        done, tried = False, []
        for mdl in list(candidates):
            try:
                moments += find_moments(part, api_key, per_part, live, mdl)
                candidates.remove(mdl)
                candidates.insert(0, mdl)   # the model that worked goes first
                done = True
                break
            except Exception as e:
                reason = short_err(e)
                tried.append(f"• **{mdl}**: {reason}")
                if "limit 0" in reason or "404" in reason:
                    bad.add(mdl)
                if "429" in reason or "quota" in reason:
                    quota_hit = True
                live.write(f"❌ {mdl}: {reason}. Trying the next model...")
        if not done:
            failed += 1
            st.warning(f"Part {k} failed on every model:\n\n" + "\n".join(tried))
            break
    live.empty()
    bar.progress(1.0, text=f"Done! Took {int(time.time() - t0)}s / শেষ! {int(time.time() - t0)} সেকেন্ড লেগেছে")

    valid = []
    for m in moments:
        try:
            dur = float(m["end"]) - float(m["start"])
        except Exception:
            continue
        if MIN_LEN - 5 <= dur <= MAX_LEN + 10:   # drop clips far outside the 20-60s range
            valid.append(m)
    moments = valid

    if not moments:
        if quota_hit:
            st.error(
                "No model had free quota (429). This is not a bug in your code. / কোনো মডেলেই ফ্রি কোটা পাওয়া যায়নি, এটা আপনার কোডের ভুল নয়।\n\n"
                "• Look at the list above: models showing \"limit 0\" don't run on the free tier.\n"
                "• If it's a per-minute limit, try again in 1-2 minutes.\n"
                "• If it's a daily limit, try again after 24 hours.\n"
                "• Quick fix: enable billing at aistudio.google.com."
            )
        else:
            st.error("No clips found. Please try again in a little while. / কোনো ক্লিপ পাওয়া যায়নি, কিছুক্ষণ পর আবার চেষ্টা করুন।")
        st.stop()
    if failed:
        st.info("Part of the video failed, so some sections have no clips. Try again to get the rest.")

    moments = sorted(moments, key=lambda x: -x.get("score", 0))[:NUM_CLIPS]
    for i, m in enumerate(moments, 1):
        dur = int(m["end"] - m["start"])
        st.subheader(f"{i}. ⭐ {m.get('score', '?')}/10: {m.get('title', '')}")
        if m.get("title_bn"):
            st.write(f"↳ **{m['title_bn']}**")
        st.markdown(f"**Time / সময়:** {fmt(m['start'])} → {fmt(m['end'])} ({dur} seconds / সেকেন্ড)")
        show("Opening line", "শুরুর বাক্য", m.get("opening_line", ""), m.get("opening_line_bn", ""), quote=True)
        show("Closing line", "শেষের বাক্য", m.get("closing_line", ""), m.get("closing_line_bn", ""), quote=True)
        show("Hook", "হুক", m.get("hook", ""), m.get("hook_bn", ""))
        show("Why it could go viral", "কেন ভাইরাল হতে পারে", m.get("why_viral", ""), m.get("why_viral_bn", ""))
        st.video(f"https://www.youtube.com/watch?v={vid}",
                 start_time=int(m["start"]), end_time=int(m["end"]))
        cmd = (
            f'yt-dlp -f "bv*[height<=1080]+ba/b" --merge-output-format mp4 '
            f'--download-sections "*{int(m["start"])}-{int(m["end"])}" '
            f'--force-keyframes-at-cuts -o "clip_{i}.%(ext)s" '
            f'"https://www.youtube.com/watch?v={vid}"'
        )
        with st.expander("⬇️ Download command for this clip (run on your computer) / এই ক্লিপ ডাউনলোডের কমান্ড (নিজের কম্পিউটারে চালান)"):
            st.code(cmd, language="bash")
        st.divider()
