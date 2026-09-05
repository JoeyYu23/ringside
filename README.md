# Ringside — the coach in your corner during the cold call

Real-time coaching for insurance brokers on cold calls. Listens to both sides of the call, shows **one line the broker
can say out loud** under a second after the prospect stops talking, gets them past the gatekeeper, handles objections,
drives to a booked meeting, remembers the account, debriefs the call, and learns across calls.

Built for the Binder track "AI Sales Coach".

## The two design decisions that matter

1. **The coach never generates text during the call.** Every cue is a line from a curated playbook
   (`app/coach/playbook.yaml`), rendered only with facts we actually hold (account record + what the prospect said).
   A line that needs a fact we don't have is simply not eligible. There is no path that can invent a price, a
   coverage term, a carrier, or a claim about the prospect's policy — when the prospect asks for one, the line says
   *"I won't guess on the phone."*
2. **Rules first, LLM second, silence third.** A regex/state-machine classifier handles the situations the brief lists
   (what is this regarding / send an email / who are you trying to reach / we're all set / …) in well under a
   millisecond. An LLM only sees utterances no rule recognises, and it can only answer with a situation id — never
   words. If it isn't sure, the coach shows nothing. Knowing when to stay silent is a feature.

## What it does

| Brief requirement | How |
|---|---|
| Identify who is on the call, track the stage | Role (gatekeeper / decision maker) and stage (intro → gatekeeper → discovery → objection → close) from a rules state machine |
| Get past the gatekeeper | Specific lines per gatekeeper move, filled with the DM's name and renewal month when known |
| Handle objections, reflex vs genuine | First short brush-off gets the pattern-interrupt line; repeated/elaborated objections get the acknowledge-and-ask line |
| Drive toward the meeting | Ask nudges once the renewal is known; **"Stop selling"** the moment a soft yes appears; rambling / pitching-after-yes / numbers-on-the-phone nudges |
| Remember the account | SQLite memory → pre-dial brief + an *avoid-list*: lines that failed on the last call to this company are benched |
| Debrief the call | Instant rule-based debrief at hangup, then an LLM debrief grounded in the transcript; **every CRM value must appear verbatim in the transcript or it is dropped and flagged** |
| Learn across calls | Insights page: openers → meetings, objection lines that move the call forward, where calls die, segments, per-broker talk ratio / fillers / ask rate, objections the playbook doesn't have yet |
| Speed, glanceable, zero interaction | One line, big serif, amber when you must stop; stage strip, momentum, talk share. Nothing to click during the call |

Latency (measured, see tests): rule path < 1 ms; local whisper (MLX, `small.en`) median 170 ms after end of speech;
transcript → line on screen < 100 ms in the browser.

## Who plays the prospect

- **AI voice** — Gemini Live plays the gatekeeper, then (only if the broker earns it) the decision maker, with two
  voices. The gatekeeper transfers only when the caller names the renewal and the decision maker; the DM agrees only to
  a concrete fifteen-minute slot tied to the renewal. So the coach's lines have to actually work.
- **A judge on a second device** — `/prospect/<call>` on any laptop/phone on the same network. Both seats go through
  local speech recognition; speaker separation is by seat, not by voice.
- **A judge dials a phone number** — Twilio Media Streams bridge (`/twilio/voice`, `/ws/twilio`); the phone is the
  prospect seat. Needs a number + `PUBLIC_HOST`. (Code path present; not exercised without a number.)
- **Scripted, no microphone** — plays a transcript through the coach with human pacing. The fallback if audio fails.

## Run

```bash
cp .env.example .env            # GEMINI_API_KEY for the synthetic prospect; ANTHROPIC_API_KEY / GROQ_API_KEY for LLM jobs
uv venv --python 3.12 .venv && uv pip install -r requirements.txt
.venv/bin/uvicorn app.server:app --port 8080     # http://localhost:8080
```

Speech-to-text for human seats is local (`mlx-whisper`, Apple Silicon); the first start downloads the model.
No keys → everything still works except the AI-voice prospect and the LLM debrief (rules take over).

## The three-minute demo

1. Accounts page → Alex → **Brooklyn Auto Group** (memory: *"Aug 12: gatekeeper Maria → 'send an email' → no reply. Don't take the email route again."*) → AI voice → Start.
2. Maria answers. Read the line. "What is this regarding?" → *"The March renewal — Dan will know. Is Dan available?"* She transfers.
3. Dan: "We're all set, we have a broker." → *"Keep them. A second look before March costs you nothing. Fifteen minutes?"* He caves → **amber: Stop selling. Confirm the time, get the email.**
4. Hang up → debrief + CRM fields appear on their own.
5. Start a second call to the same account: the brief now knows what happened and benches the line that failed.

## Tests

```bash
.venv/bin/python -m pytest tests/                 # deterministic coach + memory (no audio, no network)
.venv/bin/python -m tests.e2e_audio                # synthesized speech → VAD → whisper → cues, against a running server
.venv/bin/python -m tests.e2e_synthetic            # scripted broker audio vs the Gemini Live gatekeeper + DM (needs GEMINI_API_KEY)
.venv/bin/python -m tests.e2e_browser              # Playwright: screenshots + transcript→cue DOM latency
.venv/bin/python -m tests.live_llm_check           # LLM classifier fallback + grounded debrief
```

## Data

We had no call recordings. Everything in the database is **synthetic and labeled** (`calls.synthetic = 1`, shown on the
insights page); the demo scripts in `data/scripts/` were written by hand; the AI prospect is a persona. The system
expects imperfect transcription (names misheard, overlap): cues never depend on exact broker wording, CRM values are
verified against the transcript, and generic inboxes (`info@`) are never recorded as a contact.

## Layout

```
app/coach/playbook.yaml   the vocabulary (57 lines, 32 situations)      app/server.py     sessions, seats, overlay events, Twilio
app/coach/classify.py     rules classifier (prospect + broker side)      app/stt.py        energy VAD + local whisper
app/coach/engine.py       state machine, cues, rule debrief              app/prospect.py   Gemini Live gatekeeper → decision maker
app/coach/llm.py          LLM fallback → situation id only               app/debrief.py    grounded LLM debrief (Claude → Groq → Gemini → rules)
app/memory.py             SQLite accounts / calls / cues / insights      app/static/       Ringside pages
```
