#!/usr/bin/env python3
"""
Influencer Classifier — Multi-Agent Architecture
Run: python3 server.py  →  http://localhost:5001
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import anthropic
import concurrent.futures
import json, re, os, base64, glob, subprocess, tempfile
import requests
import yt_dlp
from ddgs import DDGS

app = Flask(__name__, static_folder='.')
CORS(app)

# ─── Constants ────────────────────────────────────────────────────────────────

CREATOR_TYPES = {
    "AI creators": ["AI Visual creators / AI Artist", "AI Tool Announcement", "AI News Channel", "AI Technology"],
    "Filmmakers": ["Tech and Gear", "Editing & Post-production", "Cinematic Storytelling", "Industry Commentary"],
    "Marketers & business creators": ["Tool Comparisons", "Creative Content Marketing", "Productivity & Workflows", "Industry Updates/Trends"],
    "Big names": ["Business & Entrepreneurship Icons", "Technology & Innovation Icons", "Cultural and Media Icons", "Celebrity Creators", "Educational Icons", "Entertainment"],
}
CONTENT_TYPES = ["Visual how to/tutorial", "Comparison/Review", "News & updates", "Steal my prompt", "Visual inspiration", "Step by step breakdown"]
CONTENT_GOALS = ["GTM", "Educational", "Awareness", "Credibility/Trust", "Performance"]
FORMATS = ["YouTube dedicated", "YouTube integration", "Shortform (IG, TikTok, YT shorts)", "Podcast", "Newsletter/Blog", "LinkedIn", "X", "Workshop/Event"]

YT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
    'Cookie': 'CONSENT=YES+1',
}

HAIKU  = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"

# ─── Raw data fetchers (no Claude) ────────────────────────────────────────────

def fetch_web_search(name, main_account=''):
    query = f"{name} {main_account} creator influencer" if main_account not in ('', '—') else f"{name} creator influencer"
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=4))
        return '\n'.join(
            f"• {r.get('title','')}: {(r.get('body') or '')[:200]}"
            for r in results if r.get('title')
        )
    except Exception:
        return ''


def fetch_channel_info(youtube_url):
    """Returns (bio, video_entries, channel_id)."""
    if not youtube_url or youtube_url == '—':
        return '', [], ''
    if not youtube_url.startswith('http'):
        youtube_url = 'https://' + youtube_url
    bio, entries, channel_id = '', [], ''
    try:
        r = requests.get(youtube_url.rstrip('/'), headers=YT_HEADERS, timeout=8)
        cids = re.findall(r'UC[a-zA-Z0-9_-]{22}', r.text)
        if not cids:
            return bio, entries, channel_id
        channel_id = cids[0]
        for pat in [
            r'"channelMetadataRenderer".*?"description":"((?:[^"\\]|\\.){10,}?)"',
            r'"shortDescription":"((?:[^"\\]|\\.){10,}?)"',
        ]:
            m = re.search(pat, r.text[:400000], re.DOTALL)
            if m:
                candidate = m.group(1).replace('\\n', '\n').replace('\\"', '"')[:600]
                if len(candidate.strip()) > 15:
                    bio = candidate
                    break
        rss = requests.get(f'https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}', timeout=8)
        if rss.status_code == 200:
            for entry in re.findall(r'<entry>(.*?)</entry>', rss.text, re.DOTALL)[:12]:
                vid_m   = re.search(r'<yt:videoId>([^<]+)</yt:videoId>', entry)
                title_m = re.search(r'<title>([^<]+)</title>', entry)
                if vid_m:
                    entries.append({'id': vid_m.group(1), 'title': title_m.group(1) if title_m else ''})
    except Exception:
        pass
    return bio, entries, channel_id


def fetch_comments(video_id):
    """yt-dlp: top comments."""
    try:
        with yt_dlp.YoutubeDL({
            'quiet': True, 'no_warnings': True, 'skip_download': True,
            'getcomments': True,
            'extractor_args': {'youtube': {'comment_sort': ['top'], 'max_comments': ['8,0,0,8']}},
        }) as ydl:
            info = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=False)
        title = info.get('title', '')
        desc  = (info.get('description') or '')[:400].strip()
        comments = [c.get('text','').strip()[:150] for c in (info.get('comments') or [])[:6] if c.get('text','').strip()]
        return {'title': title, 'description': desc, 'comments': comments}
    except Exception:
        return {}


def fetch_transcript(video_id):
    """yt-dlp: auto-subtitle → plain text."""
    out = f'/tmp/yt_sub_{video_id}'
    try:
        with yt_dlp.YoutubeDL({
            'quiet': True, 'no_warnings': True, 'skip_download': True,
            'writeautomaticsub': True, 'subtitleslangs': ['en', 'en-US'],
            'subtitlesformat': 'vtt', 'outtmpl': out,
        }) as ydl:
            ydl.download([f'https://www.youtube.com/watch?v={video_id}'])
        files = glob.glob(out + '*.vtt')
        if not files:
            return ''
        raw = open(files[0], encoding='utf-8').read()
        os.remove(files[0])
        lines = [re.sub(r'<[^>]+>', '', l.strip()) for l in raw.split('\n')
                 if l.strip() and '-->' not in l and not l.strip().startswith('WEBVTT')
                 and not l.strip().isdigit()]
        deduped = []
        for l in lines:
            if not deduped or l != deduped[-1]:
                deduped.append(l)
        return ' '.join(deduped)[:700]
    except Exception:
        return ''


def fetch_frames(video_id, n=3):
    """Download 12 s of video → N base64 JPEG frames."""
    tmpdir = tempfile.mkdtemp()
    out = os.path.join(tmpdir, 'v')
    frames = []
    try:
        with yt_dlp.YoutubeDL({
            'quiet': True, 'no_warnings': True,
            'format': 'worst[ext=mp4]/worst',
            'outtmpl': out + '.%(ext)s',
            'download_ranges': lambda i, y: [{'start_time': 0, 'end_time': 12}],
            'force_keyframes_at_cuts': True,
        }) as ydl:
            ydl.download([f'https://www.youtube.com/watch?v={video_id}'])
        vfiles = glob.glob(out + '.*')
        if not vfiles:
            return frames
        vf = vfiles[0]
        for sec in [int(12*(i+1)/(n+1)) for i in range(n)]:
            fp = os.path.join(tmpdir, f'f{sec}.jpg')
            subprocess.run(['ffmpeg', '-y', '-ss', str(sec), '-i', vf,
                            '-vframes', '1', '-q:v', '10', '-vf', 'scale=480:-1', fp],
                           capture_output=True)
            if os.path.exists(fp):
                frames.append(base64.b64encode(open(fp,'rb').read()).decode())
                os.remove(fp)
        os.remove(vf)
    except Exception:
        pass
    finally:
        try: os.rmdir(tmpdir)
        except: pass
    return frames


# ─── Taxonomy (cached across all agent calls) ────────────────────────────────

TAXONOMY_SYSTEM = """You are an expert content analyst for Artlist's influencer database.

CREATOR TYPES:
• "AI creators" — Content SOLELY about using AI in content creation.
  - "AI Visual creators / AI Artist" — Showcases AI-generated videos, animations, images (the OUTPUT is the content)
  - "AI Tool Announcement" — Primarily shares/promotes/introduces new AI tools and product launches
  - "AI News Channel" — Primarily delivers AI news and industry developments (carousel/news format)
  - "AI Technology" — Primarily explains, discusses, analyzes AI technologies, platforms, innovations

• "Filmmakers" — Content focused on cameras, editing, cinematic craft, or filmmaking industry.
  - "Tech and Gear" — Cameras, lenses, lighting, audio, production equipment reviews/tutorials
  - "Editing & Post-production" — Editing, color grading, sound design, post-production workflows
  - "Cinematic Storytelling" — Short films, creative projects, artistic filmmaking, cinematic narrative
  - "Industry Commentary" — Filmmaking trends, industry developments, future of video production

• "Marketers & business creators" — Content focused on marketing, business growth, tools for marketers/entrepreneurs.
  - "Tool Comparisons" — Comparing, reviewing, evaluating software and tools
  - "Creative Content Marketing" — Creating, scaling, optimizing content to drive business growth
  - "Productivity & Workflows" — Systems, efficiency, automation, workflows, operational improvement
  - "Industry Updates/Trends" — Marketing, business, and industry news, trends, developments

• "Big names" — Public figures/celebrities with very large audiences (1M+ or mainstream recognition).
  - "Business & Entrepreneurship Icons" — Business, entrepreneurship, leadership, company building
  - "Technology & Innovation Icons" — Technology, innovation, emerging trends, future of industries
  - "Cultural and Media Icons" — Shapes mainstream culture, media, and public conversations
  - "Educational Icons" — Teaching, learning, sharing knowledge at scale
  - "Celebrity Creators" — Public figures with large audiences around personal brand/lifestyle
  - "Entertainment" — Entertainment, storytelling, humor, challenges, audience engagement

CONTENT FORMATS:
• "Visual how to/tutorial" — Teaches how to use a tool or achieve a specific outcome
• "Comparison/Review" — Evaluates, compares, or reviews tools/platforms/products
• "News & updates" — Shares industry news, product launches, feature releases, emerging trends
• "Visual inspiration" — Showcases ONLY the creative output to inspire (no tutorial, just the result)
• "Steal my prompt" — Shares prompts, templates, or ready-to-use inputs viewers can replicate
• "Step by step breakdown" — Deconstructs a process into clear stages and explains execution

MARKETING OBJECTIVES:
• "GTM" — Supports product launches, feature releases, campaigns, strategic business initiatives
• "Educational" — Teaches audiences how to use Artlist, improve workflows, develop skills
• "Awareness" — Increases visibility and recognition of Artlist among target audiences
• "Credibility/Trust" — Builds confidence through expert endorsement, authentic use cases, industry validation
• "Performance" — Drives measurable actions: website visits, trials, sign-ups, purchases, affiliate conversions

DECISION RULES:
1. Big Name (1M+ or celebrity) → always "Big names" regardless of topic
2. AI beats Filmmaker in ties — creator teaching BOTH AI + filmmaking → "AI creators" if AI is central
3. Video evidence > bio > web search (videos are ground truth)
Respond ONLY in valid JSON."""

# ─── Claude sub-agents ────────────────────────────────────────────────────────

def _parse(text):
    return json.loads(re.sub(r'```json|```', '', text).strip())

def call_claude(client, user_content, system, model=HAIKU, max_tokens=400):
    try:
        msg = client.messages.create(
            model=model, max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_content}]
        )
        return _parse(msg.content[0].text)
    except Exception as e:
        return {"error": str(e)}

def call_claude_cached(client, user_content, model=SONNET, max_tokens=800):
    """Call Claude with the shared taxonomy system prompt cached."""
    try:
        msg = client.messages.create(
            model=model, max_tokens=max_tokens,
            system=[{"type": "text", "text": TAXONOMY_SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_content}]
        )
        return _parse(msg.content[0].text)
    except Exception as e:
        return {"error": str(e)}


def agent_web(client, web_text, name):
    """Agent 1 (Haiku): What does the internet say about this person?"""
    if not web_text:
        return {"summary": "no results", "follower_hint": "unknown", "niche": "unclear"}
    return call_claude(client,
        f"Search results for '{name}':\n{web_text}",
        'You analyze web search results about a content creator. '
        'Return ONLY JSON: {"summary": "1-2 sentences about who they are", '
        '"follower_hint": "under_1M / over_1M / celebrity", "niche": "what they are known for"}'
    )


def agent_bio(client, bio):
    """Agent 2 (Haiku): What does the channel bio say?"""
    if not bio or len(bio.strip()) < 10:
        return {"signal": "no bio available", "type_hint": "unclear"}
    return call_claude(client,
        f"Channel bio:\n{bio}",
        'You analyze a YouTube channel bio to determine creator type. '
        'Return ONLY JSON: {"signal": "key phrases from bio", '
        '"type_hint": "AI creators / Filmmakers / Marketers & business creators / Big names / unclear"}'
    )


def agent_video(client, title, transcript, comments, frames_b64):
    """Agent 3 (Sonnet): Unified per-video agent — sees title + transcript + comments + frames together."""
    comments_text = '\n'.join(f'• {c}' for c in comments[:6]) if comments else 'No comments available.'
    text_block = {
        "type": "text",
        "text": (
            f'Analyze this YouTube video:\n\n'
            f'TITLE: "{title}"\n\n'
            f'TRANSCRIPT (first ~700 words):\n{transcript or "No transcript available."}\n\n'
            f'TOP COMMENTS:\n{comments_text}\n\n'
            'Based on EVERYTHING above (title + transcript + comments + frames if provided), return ONLY JSON:\n'
            '{"video_summary": "1 sentence: what this video is about", '
            '"category": "AI creators/Filmmakers/Marketers & business creators/Big names/unclear", '
            '"content_format": "Visual how to/tutorial/Comparison/Review/News & updates/Visual inspiration/Steal my prompt/Step by step breakdown/unclear", '
            '"visual_style": "AI-generated/screen-recording/talking-head/cinematic/gear-review/unclear", '
            '"key_signals": "strongest signals that led to the category", '
            '"audience": "who watches this content"}'
        )
    }
    content = [text_block] + [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": f}}
        for f in frames_b64[:3]
    ]
    try:
        msg = client.messages.create(
            model=SONNET, max_tokens=600,
            system=[{"type": "text", "text": TAXONOMY_SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}]
        )
        return _parse(msg.content[0].text)
    except Exception as e:
        return {"error": str(e)}


def agent_aggregator(client, creator_name, web_report, bio_report, video_reports):
    """Final aggregator (Sonnet + cached taxonomy): synthesizes all agent reports → final classification."""

    videos_summary = ''
    for i, vr in enumerate(video_reports, 1):
        videos_summary += f'\n[Video {i}: "{vr["title"]}"]\n'
        if vr.get('title_only'):
            videos_summary += '  (title only, no deep analysis)\n'
        else:
            videos_summary += f'  Analysis: {json.dumps(vr.get("analysis", {}))}\n'

    prompt = f"""CREATOR: {creator_name}

── Agent 1: Web Search ──
{json.dumps(web_report, indent=2)}

── Agent 2: Channel Bio ──
{json.dumps(bio_report, indent=2)}

── Agent 3: Video Analyses (1 agent per video, sees title+transcript+comments+frames) ──
{videos_summary}

Now follow these steps using the taxonomy in your system prompt:

STEP 1 — TYPE: Count AI vs Filmmaker vs Marketer signals across all video analyses + web + bio.
  Big Name (1M+ / celebrity) always wins. AI beats Filmmaker in ties.

STEP 2 — SUBTYPE: Pick the most specific subtype from the taxonomy for the chosen type.

STEP 3 — CONTENT TYPE: What format do MOST of the videos follow?

STEP 4 — MARKETING OBJECTIVE: What is Artlist's best use for this creator?

Respond ONLY in valid JSON (no markdown):
{{
  "agent1_profile": "1-2 sentences: what web + bio revealed",
  "agent2_content": "1-2 sentences: what the video analyses collectively showed",
  "agent3_conflict": "none OR describe conflicting signals and resolution",
  "agent4_decision": "final reasoning with signal counts",
  "type": "exact type",
  "subtype": "exact subtype",
  "content_type": "exact content type",
  "content_goal": "exact marketing objective",
  "confidence": "High|Medium|Low"
}}"""

    return call_claude_cached(client, prompt, model=SONNET, max_tokens=1500)


# ─── Main pipeline ────────────────────────────────────────────────────────────

def run_full_pipeline(client, creator):
    """
    Parallel multi-agent pipeline:
    t=0: web search + channel info (parallel)
    t=2: for each of 4 videos: title+transcript+frames+comments (all parallel)
         + bio agent + web agent (Claude calls, also parallel)
    t=N: aggregator
    """
    name         = creator.get('Name', '')
    main_account = creator.get('Main Account', '—')
    youtube_url  = creator.get('youtube', '') or creator.get('YouTube', '')

    # ── Phase 1: fetch raw data (no Claude) ─────────────────────────
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_web     = ex.submit(fetch_web_search, name, main_account)
        f_channel = ex.submit(fetch_channel_info, youtube_url)
        web_text           = f_web.result()
        bio, video_entries, _ = f_channel.result()

    deep_entries    = video_entries[:4]
    shallow_titles  = [e['title'] for e in video_entries[4:10]]

    # ── Phase 2: fetch raw video data (no Claude) — all in parallel ──
    def fetch_video_raw(entry):
        vid_id = entry['id']
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            f_c = ex.submit(fetch_comments, vid_id)
            f_t = ex.submit(fetch_transcript, vid_id)
            f_f = ex.submit(fetch_frames, vid_id, 3)
            c_data     = f_c.result()
            transcript = f_t.result()
            frames     = f_f.result()
        return {
            'id': vid_id,
            'title':       c_data.get('title', entry['title']),
            'description': c_data.get('description', ''),
            'comments':    c_data.get('comments', []),
            'transcript':  transcript,
            'frames':      frames,
        }

    raw_videos = [None] * len(deep_entries)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(fetch_video_raw, e): i for i, e in enumerate(deep_entries)}
        for future in concurrent.futures.as_completed(futures):
            raw_videos[futures[future]] = future.result()

    # ── Phase 3: 7 Claude calls in parallel ──────────────────────────
    # 2 Haiku (web + bio) + 4 Sonnet (1 per video, all signals merged) = 7 total
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as ex:
        f_web_agent = ex.submit(agent_web, client, web_text, name)
        f_bio_agent = ex.submit(agent_bio, client, bio)
        video_futures = {
            i: ex.submit(agent_video, client,
                         v['title'], v['transcript'], v['comments'], v['frames'])
            for i, v in enumerate(raw_videos) if v is not None
        }

        web_report = f_web_agent.result()
        bio_report = f_bio_agent.result()

        video_reports = []
        for i, v in enumerate(raw_videos):
            if v is None:
                continue
            video_reports.append({
                'title':    v['title'],
                'analysis': video_futures[i].result(),
            })

    # Shallow titles (no agent, title only hint for aggregator)
    for t in shallow_titles:
        video_reports.append({'title': t, 'title_only': True})

    # ── Phase 4: Aggregator ───────────────────────────────────────────
    return agent_aggregator(client, name, web_report, bio_report, video_reports)


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


@app.route('/classify', methods=['POST'])
def classify():
    data    = request.json
    creator = data.get('creator', {})
    api_key = (os.environ.get('ANTHROPIC_API_KEY') or data.get('api_key', '')).strip()
    if not api_key:
        return jsonify({'error': 'No API key configured on server'}), 400

    try:
        client = anthropic.Anthropic(api_key=api_key)
        result = run_full_pipeline(client, creator)

        # Validate outputs
        if result.get('type') not in CREATOR_TYPES:
            result['type'] = 'Filmmakers'; result['confidence'] = 'Low'
        valid_sub = CREATOR_TYPES.get(result['type'], [])
        if result.get('subtype') not in valid_sub:
            result['subtype'] = valid_sub[0] if valid_sub else '—'; result['confidence'] = 'Low'
        if result.get('content_type') not in CONTENT_TYPES:
            result['content_type'] = 'Visual how to/tutorial'
        if result.get('content_goal') not in CONTENT_GOALS:
            result['content_goal'] = 'Educational'
        if result.get('format') not in FORMATS:
            result['format'] = 'YouTube dedicated'

        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    debug = not os.environ.get('ANTHROPIC_API_KEY')  # no debug in prod
    print(f'\n Influencer Classifier running on port {port}\n')
    app.run(host='0.0.0.0', debug=debug, port=port)
