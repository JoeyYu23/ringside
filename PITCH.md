# Ringside — 3-minute pitch

**0:00 · Hook** *(accounts page already open, headphones on)*
An insurance broker makes sixty cold calls a day. Most end in four words: "Send an email to info@." When you finally reach a decision maker you get twenty seconds. The right answer depends on who picked up, what their renewal date is, and what happened last time you called. Nobody can coach that in real time.

So we built Ringside: the coach in your corner while you're on the call. One line, sayable out loud, under a second after they stop talking — and that line can never be invented.

**0:30 · Live call** *(click Brooklyn Auto Group → AI voice → Start; read every line exactly as shown)*
- Before I dial, memory: last time, reception sent us to the info@ inbox and we lost the call. That line is benched.
- *(Maria answers → "What is this regarding?")* Watch the screen: it names her renewal and her boss. *(she transfers)*
- *(Dan: "We're all set, we have a broker.")* The coach doesn't fight the broker — it ties fifteen minutes to his March renewal. *(Dan: "Alright, fine.")*
- Amber. **Stop selling.** This is where most calls are lost after they're won. Confirm the time, get the email. *(hang up)*

**2:00 · Debrief** *(scroll)*
Nothing was clicked. Outcome, which lines worked, talk ratio — I talked too much — one thing to fix, and the CRM fields. Every field is verified against the transcript word for word; anything Dan didn't say is dropped and flagged.

**2:25 · How it stays honest**
During the call nothing is generated. Fifty-seven curated lines; each needs facts we actually hold — no fact, no line. A rules engine picks the situation in under a millisecond; the LLM only ever chooses a situation id, never words; unsure means silent.

**2:40 · Learning** *(insights page)*
Across calls: which openers get through, which objection lines convert, where calls die — seventy-two percent at the gatekeeper — and per-broker coaching. Objections we don't have yet queue for review.

**2:50 · Close**
Everything you saw runs locally; history is synthetic and labeled. Speaker separation is by seat, so a phone can be the prospect. Ringside: the coach in your corner.


---

# Ringside — 75-second version

*Setup: create a scripted call first (accounts → Brooklyn Auto Group → "Scripted call, no microphone" → Start), then add `?speed=2` to the call URL. Don't press play until 0:15.*

**0:00** Sixty cold calls a day. Most end with "send an email to info@." When you reach a decision maker you get twenty seconds — and the right answer depends on who picked up, their renewal date, and what happened last time you called.

**0:15** Ringside is the coach in your corner. *(press Play)* Memory first: last time reception sent us to the inbox and we lost the call — that line is benched.

**0:25** *(gatekeeper cues scroll)* "What is this regarding?" — the line names her renewal and her boss. She transfers.

**0:35** *(Dan's objection)* "We have a broker." — keep them, fifteen minutes before March. He gives in — and the screen turns amber: **stop selling.** That's where most calls are lost after they're won.

**0:50** *(debrief appears)* Nothing clicked. Outcome, what worked, one thing to fix, and CRM fields verified word for word against the transcript — anything he didn't say is dropped.

**1:00** During the call nothing is generated: fifty-seven curated lines, each needing facts we actually hold; the LLM can only pick a situation, never words; unsure means silent. Under a millisecond, running locally.

**1:10** Across calls it learns which openers get through, where calls die, and what each broker should fix. History is synthetic and labeled. Ringside — the coach in your corner.
