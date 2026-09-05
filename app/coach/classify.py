"""Rules-first classifier for what the prospect just said (and what the broker is doing).

Runs in microseconds; an LLM only sees utterances no rule recognises. Every result maps to a
playbook situation, so the coach's output stays inside the playbook whichever path fired.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
MONTH_RE = re.compile(r"\b(" + "|".join(MONTHS) + r"|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)\b\.?", re.I)
EMAIL_RE = re.compile(r"\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b", re.I)
GENERIC_INBOX = re.compile(r"^(info|sales|contact|office|admin|hello|support|hr|billing|general)@", re.I)
EMAIL_SPOKEN_RE = re.compile(r"\b([a-z0-9._-]+) at ([a-z0-9-]+) dot (com|net|org|io|co)\b", re.I)


@dataclass
class Classification:
    situation: str | None = None
    role_hint: str | None = None      # gatekeeper | dm
    when: str = "any"                 # reflex | genuine | any
    facts: dict = field(default_factory=dict)
    signals: set[str] = field(default_factory=set)
    source: str = "rule"
    matched: str = ""


def _norm(text: str) -> str:
    t = text.lower().strip()
    t = re.sub(r"[’‘]", "'", t)
    t = re.sub(r"\b(we|they|you|i)'re\b", r"\1 are", t)
    t = re.sub(r"\bi'm\b", "i am", t)
    t = re.sub(r"\b(he|she|it|that|there|what|who|here)'s\b", r"\1 is", t)
    t = re.sub(r"\b(don|doesn|didn|isn|aren|wasn|can|couldn|won|wouldn|haven|hasn)'t\b", lambda m: {"don": "do not", "doesn": "does not", "didn": "did not", "isn": "is not", "aren": "are not", "wasn": "was not", "can": "can not", "couldn": "could not", "won": "will not", "wouldn": "would not", "haven": "have not", "hasn": "has not"}[m.group(1)], t)
    t = re.sub(r"[^a-z0-9@.'\s?-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def words(text: str) -> int:
    return len(re.findall(r"[a-z0-9']+", text.lower()))


# (situation, regex, role_hint, signal) — order matters: first match wins within a group.
HARD_NO = re.compile(r"\b(do not|dont|stop) (call|calling|contact)(ing)?\b|\b(take|remove) (me|us|our number|this number) off\b|\bdo not call list\b|\bunsubscribe\b|\bnever call\b")
TRANSFER = re.compile(r"\b(transfer|transferring|connect you|put you through|patch you|let me (get|grab) (him|her|them)|let me see if (he|she|they)|i will (get|grab) (him|her|them)|hold on|one (moment|second|sec)|hang on|please hold|hold please|putting you through)\b")
GK_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    ("gk_what_regarding", re.compile(r"\bwhat is (this|it|the call|that) (regarding|about|in (reference|regards|regard) to|concerning)\b|\bregarding what\b|\bwhat (are you|is this) calling (about|regarding|in reference to)\b|\bmay i ask what (this|it) is (regarding|about)\b|\bwhat is this in (regards|reference) to\b|\bin regards to what\b|\bwhat can i tell (him|her|them) (this|it) is (about|regarding)\b"), "negative"),
    ("gk_send_email", re.compile(r"\bsend (us |me |it |that |the |your |an |over an |over the |through an )*(email|e-mail|mail)\b|\bemail (it|that|us|them|him|her)\b|\binfo ?@|\bshoot (me |us )?(an |the )?email\b|\bput (it|that) in an email\b|\bemail (would be|is) (best|better|fine)\b|\bby email\b|\bvia email\b|\bemail (them|us|it) over\b|\bjust email\b"), "negative"),
    ("gk_who_reach", re.compile(r"\bwho (are|were) you (trying to reach|looking for|calling for|after|trying to get)\b|\bwho (do|would) you (want|need|like) to (speak|talk) (to|with)\b|\bwho is this for\b|\bwho (are you|were you) (calling|trying) (for|to)\b|\bwho did you (want|need)\b|\bwhich (person|department)\b|\bwho would you like\b"), "neutral"),
    ("gk_expecting", re.compile(r"\bexpecting (your|the|this) call\b|\bdo you have an appointment\b|\b(does|do|is) (he|she|they) (know|expecting)\b|\bhave you spoken (to|with) (him|her|them) before\b|\bknow you\b"), "negative"),
    ("gk_take_message", re.compile(r"\btake a message\b|\bleave a message\b|\bgive (him|her|them) (a|the) message\b|\bi will (let|tell) (him|her|them) (know|you called)\b|\bpass (it|that|the message|along)\b|\bwant to leave\b|\bcan i take\b"), "negative"),
    ("gk_not_available", re.compile(r"\b(not available|unavailable|in a meeting|in meetings|out of (the )?office|not in( today| right now| at the moment)?\b|is not in|stepped out|stepped away|on another (call|line)|on the other line|on a call|busy right now|out today|on vacation|not here|out for the day|out to lunch|at lunch|will not be (in|back|available)|not in the office|off today|away)\b"), "negative"),
    ("gk_all_set", re.compile(r"\b(all set|we are (all )?set|we are good|we are fine|taken care of|we are covered|we are happy|already (have|got) (that|it|coverage|insurance|someone|somebody)|not looking|do not need)\b"), "negative"),
]
DM_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    ("obj_bad_experience", re.compile(r"\b(all the same|you are all the same|burned|got screwed|last (broker|guy|agent|person)|never (called|call) back|promised (me|us)|had a bad experience|bad experience)\b"), "negative"),
    ("obj_have_broker", re.compile(r"\b(have|got|use|using|work with|working with|been with) (a|an|our|the same|my) (broker|agent|guy|girl|person|rep|agency|insurance guy|insurance person)\b|\balready (have|got) (someone|somebody|a broker|an agent|a guy)\b|\bour (broker|agent) (is|has|handles|does)\b|\bhappy with (our|my) (broker|agent|guy)\b"), "negative"),
    ("obj_all_set", re.compile(r"\b(all set|we are (all )?set|we are good|we are fine|taken care of|we are covered|we are happy|already (have|got) (that|it|coverage|insurance)|not looking|do not need (it|that|anything)|no need)\b"), "negative"),
    ("obj_not_interested", re.compile(r"\bnot interested\b|\bno thanks\b|\bno thank you\b|\bnot for (us|me)\b|\bpass\b|\bnot something we\b"), "negative"),
    ("obj_no_time", re.compile(r"\b(do not|dont) have (the )?time\b|\bbad time\b|\bi am busy\b|\bwe are busy\b|\bin the middle of\b|\bcan not talk\b|\bcannot talk\b|\brunning (into|to|late)\b|\bno time\b|\bnot a good time\b|\bgot to go\b|\bgotta go\b|\bswamped\b|\bslammed\b"), "negative"),
    ("obj_call_back_later", re.compile(r"\bcall (me |us )?(back )?(later|next (week|month|quarter|year)|in (a|a few|\d+|two|three|six) (week|month|weeks|months|days)|after|another time|some other time|tomorrow)\b|\btry (me |us )?(again )?(later|next|tomorrow|another)\b|\bnot right now\b|\bcircle back\b|\bcatch me (later|another)\b|\breach out (later|next|in)\b"), "negative"),
    ("obj_price", re.compile(r"\b(how much|what does it cost|price|pricing|cost me|costs|premium|premiums|your rates?|cheaper|quote me|a quote|the quote|ballpark|save (me|us)|savings|discount|percent)\b"), "question"),
    ("obj_send_info", re.compile(r"\bsend (me|us)? ?(over )?(some|the|your|more|an?)? ?(info|information|details|brochure|something|proposal|materials|literature|deck|pdf)\b|\bemail (me|us) (some|the|your|more)? ?(info|information|details|something)\b|\bput (something|it) together\b|\bwhat do you have\b|\bmail (me|us) something\b"), "negative"),
    ("obj_renewal_far", re.compile(r"\b(not (until|till|for)|is not (until|till|for)|does not (renew|come up)|months (away|out|off)|(next|later) (year|quarter)|a (ways|while) (off|away)|just renewed|already renewed|(signed|renewed) (last|two|three) (month|months|week|weeks)|locked in|under contract|mid[- ]term|midterm)\b"), "negative"),
    ("obj_coverage_question", re.compile(r"\b(do you (cover|handle|write|do|offer|insure)|can you (cover|insure|write|handle|do)|what carriers|which carriers|who do you (write|place|work) with|what companies|umbrella|liability|workers'? comp|general liability|cyber|deductible|coverage|carrier|claims?|excess|bop|policy limits?|limits?)\b"), "question"),
]
DM_IDENTIFIED = re.compile(r"\bthis is (he|she|him|her)\b|^speaking\b|\bspeaking\.?$|\byou (have )?got (him|her|them)\b|\byou are speaking (to|with) (him|her)\b|\bthat is me\b|\bthat would be me\b|\bi am the (owner|one|guy|person)\b|\bi handle (that|it|insurance)\b|\bi am (him|her)\b")
GK_GREETING = re.compile(r"^(?:thank you for calling|thanks for calling|good (?:morning|afternoon|evening),?|hello,?|hi,?)?\s*([a-z0-9&'. -]{2,60}?),?\s*(?:this is|it is|you are speaking with|my name is)\s+([a-z]+)\b|^([a-z]+) speaking\b|\bhow (?:can|may) i (?:help|direct)\b")
NAME_GREETING = re.compile(r"\b(this is|it is|my name is)\s+([a-z]+)\b")
PERMISSION_STRONG = re.compile(r"\b(go ahead|go on|go for it|make it quick|be quick|shoot|what is up|what do you (want|need|got)|you (have|got) (twenty|20|thirty|30|a minute|one minute|sixty|60)|i am listening|talk to me)\b")
PERMISSION_WEAK = re.compile(r"^(sure|ok|okay|fine|quickly|quick|yeah|yes|yep|alright|all right|i guess|why not)\b")
SOFT_YES = re.compile(r"\b(sure|okay|ok|yeah|yes|yep|fine|alright|all right|sounds good|that works|works for me|that is fine|let us do (it|that)|lets do (it|that)|go ahead|book it|set it up|put it (in|on)|send (me )?(the|an) invite|i can do that|that will work|why not|deal)\b")
DAY_TIME = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|tomorrow)\b.*\b(works|is (fine|good|ok|okay)|at \d|in the (morning|afternoon)|is better|would be better|is fine|then)\b|\b(ten|10|two|2|eleven|11|three|3|nine|9|four|4) (works|is (fine|good|better|ok|okay))\b")
QUESTION = re.compile(r"\?$|^(what|who|why|how|when|where|which|do|does|did|can|could|is|are|would|will)\b")


def _renewal_month(t: str) -> str | None:
    m = MONTH_RE.search(t)
    if not m:
        return None
    raw = m.group(1).lower().rstrip(".")
    for full in MONTHS:
        if full.startswith(raw[:3]):
            return full.capitalize()
    return None


def classify_prospect(text: str, state) -> Classification:
    """`state` needs: role, stage, meeting_asked, soft_yes, facts, objection_counts, permission_asked, expecting_dm."""
    raw = text.strip()
    t = _norm(raw)
    c = Classification(matched=t)
    if not t:
        return c
    n = words(t)
    facts = state.facts

    # facts the prospect just handed us (never guessed)
    em = EMAIL_RE.search(raw)
    email = em.group(0).lower() if em else None
    if not email:
        sp = EMAIL_SPOKEN_RE.search(t)
        if sp:
            email = f"{sp.group(1)}@{sp.group(2)}.{sp.group(3)}".lower()
    if email and not GENERIC_INBOX.match(email):   # info@ / sales@ is a deflection, not a contact
        c.facts["email"] = email
    month = _renewal_month(t)
    renew_ctx = bool(re.search(r"\b(renew|renewal|renews|policy|expires|expire|comes up|up for|effective|term|contract)\b", t))

    if HARD_NO.search(t):
        c.situation, c.signals = "hard_no", {"negative", "final"}
        return c
    if state.meeting_confirmed:
        return c   # the meeting is booked: the only thing left to say is goodbye
    if state.soft_yes and not c.facts.get("email") and SOFT_YES.search(t) and n <= 6:
        return c   # they already said yes; the coach already said "confirm" — stay quiet

    role = state.role
    # --- who is this? -------------------------------------------------------------
    if role != "dm":
        if DM_IDENTIFIED.search(t) or (facts.get("dm_first") and re.search(rf"\b(this is|it is|it's) {re.escape(str(facts['dm_first']).lower())}\b|^{re.escape(str(facts['dm_first']).lower())}( here| speaking|\b)", t)):
            c.situation, c.role_hint, c.signals = "dm_identified", "dm", {"positive"}
            if not facts.get("dm_first"):
                nm = NAME_GREETING.search(t)
                if nm and nm.group(2) not in ("he", "she", "him", "her", "the"):
                    c.facts["dm_first"] = nm.group(2).capitalize()
            return c
        if state.expecting_dm:
            nm = NAME_GREETING.search(t)
            if nm and nm.group(2) not in ("he", "she", "him", "her", "the"):
                c.situation, c.role_hint, c.signals = "dm_identified", "dm", {"positive"}
                if not facts.get("dm_first"):
                    c.facts["dm_first"] = nm.group(2).capitalize()
                return c
    if role != "dm":
        if TRANSFER.search(t):
            c.situation, c.role_hint, c.signals = "gk_transfer", "gatekeeper", {"positive", "transfer"}
            return c
        for sit, rx, sig in GK_PATTERNS:
            if rx.search(t):
                c.situation, c.role_hint, c.signals = sit, "gatekeeper", {sig}
                if sit in ("gk_not_available", "gk_take_message"):
                    c.signals.add("blocked")
                return c
        g = GK_GREETING.search(t)
        if g and state.stage == "intro":
            c.situation, c.role_hint, c.signals = "gk_greeting", "gatekeeper", {"neutral"}
            name = g.group(2) or g.group(3)
            if name and name not in ("he", "she", "the") and len(name) > 1:
                c.facts["gk_first"] = name.capitalize()
            return c
        if state.stage == "intro" and n <= 3:
            c.situation, c.role_hint, c.signals = "gk_greeting", None, {"neutral"}
            return c

    # --- decision maker ---------------------------------------------------------------
    if role == "dm" or c.role_hint is None:
        if state.soft_yes and (c.facts.get("email") or re.search(r"\b(send (me )?(the|an) invite|talk (then|to you then)|see you (then|there)|looking forward)\b", t)):
            c.situation, c.role_hint, c.signals = "meeting_confirmed", "dm", {"positive", "final"}
            return c
        if state.meeting_asked and (SOFT_YES.search(t) or DAY_TIME.search(t)) and not any(rx.search(t) for _, rx, _ in DM_PATTERNS[:6]):
            c.situation, c.role_hint, c.signals = "soft_yes", "dm", {"positive", "agreed"}
            return c
        if not state.meeting_asked and re.search(r"\b(let us do (it|that)|lets do (it|that)|book it|set it up|send (me )?(the|an) invite|i can do (that|fifteen|15)|put (something|it) on the calendar)\b", t):
            c.situation, c.role_hint, c.signals = "soft_yes", "dm", {"positive", "agreed"}
            return c
        if month and (renew_ctx or role == "dm"):
            c.facts["renewal_month"] = month
            far = DM_PATTERNS[8][1].search(t)
            c.situation, c.role_hint, c.signals = ("obj_renewal_far" if far else "renewal_given"), "dm", {"positive" if not far else "neutral", "fact"}
            return c
        for sit, rx, sig in DM_PATTERNS:
            if sit == "obj_coverage_question" and not QUESTION.search(t):
                continue
            if rx.search(t):
                c.situation, c.role_hint, c.signals = sit, "dm", {sig}
                seen = state.objection_counts.get(sit, 0)
                c.when = "reflex" if (seen == 0 and n <= 9) else "genuine"
                return c
        if role == "dm" and state.permission_asked and not state.permission_granted and (PERMISSION_STRONG.search(t) or (PERMISSION_WEAK.search(t) and n <= 5)):
            c.situation, c.role_hint, c.signals = "dm_permission_granted", "dm", {"positive"}
            return c
    return c


# ---- broker side ------------------------------------------------------------------------
MEETING_ASK = re.compile(r"\b(fifteen|15|twenty|20|thirty|30|ten|10) minutes\b|\b(does|would|is|how about|what about) (\w+ )?(tuesday|monday|wednesday|thursday|friday|tomorrow|next week|this week)\b|\b(tuesday|monday|wednesday|thursday|friday) (at|around|morning|afternoon)\b|\bcalendar\b|\binvite\b|\bwhat day works\b|\bwhen (works|is good)\b|\b(quick|short|brief) (call|chat|meeting|conversation|review)\b|\bsit down\b|\bget together\b|\bschedule\b")
NUMBERS_GUARD = re.compile(r"\$\s?\d|\d+\s?%|\bpercent\b|\bsave you\b|\bguarantee\b|\bcheaper\b|\blower (your )?(premium|rate|price)\b|\bcut your\b|\bhalf (the|your)\b")
FILLER = re.compile(r"\b(um+|uh+|erm|uhh+|hmm+|you know|basically|literally|kind of|sort of)\b|\blike,")
RENEWAL_ASK = re.compile(r"\b(renew|renewal|renews|expire|come up|comes up|effective date|when does your policy|when is your policy)\b")
PERMISSION_ASK = re.compile(r"\b(twenty|20|thirty|30) seconds\b|\b(got|have) (a|one) (minute|second|moment)\b|\bbad time\b|\bcatch you at a (bad|good) time\b|\bbe brief\b|\bbe quick\b")


@dataclass
class BrokerRead:
    meeting_ask: bool = False
    numbers: bool = False
    fillers: int = 0
    renewal_ask: bool = False
    permission_ask: bool = False
    word_count: int = 0


def read_broker(text: str) -> BrokerRead:
    t = _norm(text)
    return BrokerRead(meeting_ask=bool(MEETING_ASK.search(t)), numbers=bool(NUMBERS_GUARD.search(t)), fillers=len(FILLER.findall(t)),
                      renewal_ask=bool(RENEWAL_ASK.search(t)), permission_ask=bool(PERMISSION_ASK.search(t)), word_count=words(t))
