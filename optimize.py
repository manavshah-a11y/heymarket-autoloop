"""
Sequence generation engine for the Heymarket outreach optimizer.

CLI: python optimize.py --vertical <name> --persona <slug> --iterations 5 --touches 8
Derive mode: python optimize.py --derive --icp <path_to_filled_template>

Outputs:
  verticals/<vertical>/sequences/sequence_<persona>.md  — final sequence
  verticals/<vertical>/logs/log_<persona>_opener.md     — full iteration log
  verticals/<vertical>/learnings/learnings_<persona>.md — distilled learnings
"""
import argparse
import os
import sys
from datetime import datetime
import anthropic

MODEL = "claude-sonnet-4-20250514"

TOUCH_SCHEDULE = [
    (1, 1,  "Cold intro"),
    (2, 4,  "Follow-up"),
    (3, 9,  "Value add"),
    (4, 13, "Soft bump"),
    (5, 18, "New angle"),
    (6, 22, "Integration angle"),
    (7, 29, "Break-up"),
    (8, 59, "Re-engage"),
]


# ── File I/O ──────────────────────────────────────────────────────────────────

def load_file(path):
    """Read and return file contents. Exits with an error message if the file is not found."""
    if not os.path.exists(path):
        print(f"ERROR: Required file not found: {path}")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def load_api_key() -> str:
    """Load ANTHROPIC_API_KEY from environment or .env file. Exits on missing key."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key and os.path.exists(".env"):
        with open(".env", "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("ANTHROPIC_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
                    break
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        sys.exit(1)
    return api_key


def word_count(text):
    """Return the number of whitespace-delimited words in text."""
    return len(text.split())


def append_to_file(path, content):
    """Append content to a file, creating parent directories as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(content)


def write_file(path, content):
    """Write content to a file, creating parent directories as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# ── Parsing helpers ───────────────────────────────────────────────────────────

def extract_between(text, open_tag, close_tag):
    """Return the substring between open_tag and close_tag. Returns empty string if open_tag is missing."""
    if open_tag not in text:
        return ""
    after_open = text.split(open_tag, 1)[1]
    if close_tag not in after_open:
        return after_open.strip()
    return after_open.split(close_tag, 1)[0].strip()


def parse_variants(response_text):
    """Parse <variant_a/b/c> tags from a generation response. Returns dict of letter → variant text."""
    variants = {}
    for letter in ["a", "b", "c"]:
        variants[letter] = extract_between(response_text, f"<variant_{letter}>", f"</variant_{letter}>")
    return variants


def parse_scores(response_text):
    """Parse the <scores> block from an evaluation response. Returns dict with per-variant score dicts, winner, reasoning, and weakest_criterion."""
    scores_block = extract_between(response_text, "<scores>", "</scores>")
    result = {
        "variant_a": {}, "variant_b": {}, "variant_c": {},
        "winner": "", "reasoning": "", "weakest_criterion": ""
    }
    for line in scores_block.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("variant_a:") or line.startswith("variant_b:") or line.startswith("variant_c:"):
            key = line.split(":")[0].strip()
            score_part = line.split(":", 1)[1].strip()
            score_dict = {}
            for item in score_part.split("|"):
                item = item.strip()
                if "=" in item:
                    k, v = item.split("=", 1)
                    try:
                        score_dict[k.strip()] = int(v.strip())
                    except ValueError:
                        score_dict[k.strip()] = v.strip()
            result[key] = score_dict
        elif line.startswith("winner:"):
            result["winner"] = line.split("winner:", 1)[1].strip().lower()
        elif line.startswith("reasoning:"):
            result["reasoning"] = line.split("reasoning:", 1)[1].strip()
        elif line.startswith("weakest_criterion:"):
            result["weakest_criterion"] = line.split("weakest_criterion:", 1)[1].strip()
    return result


def get_hypothesis(variant_text):
    for line in variant_text.splitlines():
        if line.startswith("Hypothesis"):
            return line.strip()
    return ""


def get_subject(variant_text):
    for line in variant_text.splitlines():
        if line.lower().startswith("subject:"):
            return line.strip()
    return "Subject: not found"


def get_body_only(variant_text):
    """Return body text only — strips Hypothesis, Subject, and [SIGNATURE] lines."""
    lines = []
    for line in variant_text.splitlines():
        s = line.strip()
        if s.startswith("Hypothesis"):
            continue
        if s.lower().startswith("subject:"):
            continue
        if s == "[SIGNATURE]":
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def truncate_at_sentence(text, max_words=100):
    """Last-resort truncation: cut at sentence boundary at or before max_words."""
    words = text.split()
    if len(words) <= max_words:
        return text
    chunk = " ".join(words[:max_words])
    # Walk back to last sentence-ending punctuation
    for i in range(len(chunk) - 1, max(len(chunk) - 40, 0), -1):
        if chunk[i] in ".!?":
            return chunk[:i + 1]
    return chunk  # no sentence boundary found, return at word limit


def score_to_string(score_dict):
    parts = []
    for k in ["hook", "relevance", "proof", "cta", "brevity", "tone", "total"]:
        parts.append(f"{k}={score_dict.get(k, '?')}")
    return " ".join(parts)


def get_total(score_dict):
    try:
        return int(score_dict.get("total", 0))
    except (ValueError, TypeError):
        return 0


# ── Proof bank + rules digest + validator ────────────────────────────────────

def load_proof_bank(proof_bank_path="shared/proof_bank.md"):
    """
    Load clean, human-verified proof points from shared/proof_bank.md.
    Falls back to extracting from product_knowledge if file doesn't exist.
    Format: COMPANY | STAT | QUOTE | CONTEXT (pipe-separated)
    """
    if not os.path.exists(proof_bank_path):
        return []
    entries = []
    with open(proof_bank_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            entries.append(line)
    return entries


def extract_proof_bank(product_knowledge):
    """Fallback: pull proof points from product_knowledge.md if proof_bank.md absent."""
    bank = []
    keywords = ["response rate", "u-haul", "uhaul", "healthcare", "fitness",
                 "soc 2", "hipaa", "60%", "25%", "20%", "hubspot", "salesforce"]
    for line in product_knowledge.splitlines():
        low = line.lower().strip()
        if any(k in low for k in keywords) and len(line.strip()) > 20:
            bank.append(line.strip()[:200])
    seen = set()
    unique = []
    for b in bank:
        key = b[:60]
        if key not in seen:
            seen.add(key)
            unique.append(b)
    return unique[:20]


def build_rules_digest():
    """
    Hard constraints for the system message. Short, unambiguous, enforced by code validation.
    """
    return """You write cold outreach emails. These rules are absolute — no exceptions.

1. Body word limit: 100 words MAX. Count before writing. Violation = rejected.
2. Subject: lowercase, under 8 words, no punctuation, no emoji.
3. Body: plain text only. No bullets, bold, HTML, or markdown.
4. Tokens: only {{first_name}}, {{company}}, {{title}}.
5. End body with exactly: [SIGNATURE]
6. CTA: one reply-based question. No calls, demos, meetings, Calendly, or time asks.
7. Proof: only cite companies/stats from the PROOF BANK provided. Never invent names or stats. Quote company names VERBATIM — do not add, remove, or modify any word in the company name.
8. Never write: "circling back", "touching base", "hope this finds you", "I wanted to reach out", "quick question", "leverage", "synergy"."""


def build_persona_brief(persona_md):
    """
    Extract a compact 7-bullet brief from the full persona file.
    Avoids stuffing 600 words of persona context into every prompt.
    """
    lines = persona_md.splitlines()
    brief_parts = {
        "role": "",
        "pressures": [],
        "language": [],
        "reply_triggers": [],
    }
    section = None
    for line in lines:
        stripped = line.strip()
        if "## Day-to-Day Pressures" in stripped or "## Daily" in stripped:
            section = "pressures"
        elif "## Language" in stripped:
            section = "language"
        elif "## What Makes Them Reply" in stripped or "## What Makes" in stripped:
            section = "reply"
        elif stripped.startswith("## "):
            section = None
        elif "## Role" in stripped or "## Relationship" in stripped:
            section = "role"

        if section == "pressures" and stripped.startswith("-") and len(brief_parts["pressures"]) < 3:
            brief_parts["pressures"].append(stripped[1:].strip()[:100])
        elif section == "language" and stripped.startswith("-") and len(brief_parts["language"]) < 5:
            brief_parts["language"].append(stripped[1:].strip()[:60])
        elif section == "reply" and stripped.startswith("-") and len(brief_parts["reply_triggers"]) < 3:
            brief_parts["reply_triggers"].append(stripped[1:].strip()[:100])

    # Extract role line (first non-empty line after ## Role)
    in_role = False
    for line in lines:
        if "## Role" in line or "Title variants" in line:
            in_role = True
        elif in_role and line.strip() and not line.startswith("#"):
            brief_parts["role"] = line.strip()[:150]
            break

    brief = "PERSONA BRIEF:\n"
    if brief_parts["role"]:
        brief += f"Role: {brief_parts['role']}\n"
    if brief_parts["pressures"]:
        brief += "Top pains: " + " | ".join(brief_parts["pressures"]) + "\n"
    if brief_parts["language"]:
        brief += "Their language: " + ", ".join(brief_parts["language"]) + "\n"
    if brief_parts["reply_triggers"]:
        brief += "Reply triggers: " + " | ".join(brief_parts["reply_triggers"]) + "\n"
    return brief


def build_icp_snapshot(icp_md):
    """
    Extract a 3-line snapshot from the full ICP file.
    """
    lines = icp_md.splitlines()
    size_line = ""
    stack_line = ""
    usecase_line = ""

    for line in lines:
        s = line.strip().lower()
        if not size_line and ("employee" in s or "revenue" in s or "midmarket" in s or "company size" in s):
            size_line = line.strip()[:120]
        if not stack_line and ("hubspot" in s or "salesforce" in s or "tech stack" in s or "p1:" in s):
            stack_line = line.strip()[:120]
        if not usecase_line and ("use case" in s or "customer service" in s or "top use" in s):
            usecase_line = line.strip()[:120]

    snapshot = "ICP SNAPSHOT:\n"
    if size_line:
        snapshot += f"Size: {size_line}\n"
    if stack_line:
        snapshot += f"Stack signal: {stack_line}\n"
    if usecase_line:
        snapshot += f"Primary use case: {usecase_line}\n"
    return snapshot


FORBIDDEN_CTA_PHRASES = [
    "book a demo", "calendly", "schedule a call", "schedule time",
    "15 minutes", "15-minute", "30 minutes", "30-minute",
    "hop on a call", "jump on a call", "quick call", "phone call",
    "hop on", "jump on", "get on a call", "five minutes", "10 minutes",
    "chat live", "quick sync", "sync up", "meeting", "screen share",
    "walk you through", "walk through", "worth a call", "worth a chat",
]

FORBIDDEN_BODY_PHRASES = [
    "circling back", "touching base", "hope this finds you",
    "i wanted to reach out", "quick question", "leverage", "synergy",
    "at scale",
]


def validate_variant(variant_text):
    """
    Check word count and CTA rules. Returns (passed, list_of_issues).
    'passed' is False if any HARD violation (OVER WORD LIMIT or FORBIDDEN CTA).
    """
    issues = []
    body = get_body_only(variant_text)
    wc = len(body.split())

    if wc > 100:
        issues.append(f"OVER WORD LIMIT: {wc} words (limit 100)")

    body_lower = body.lower()
    for phrase in FORBIDDEN_CTA_PHRASES:
        if phrase in body_lower:
            issues.append(f"FORBIDDEN CTA: '{phrase}'")
            break

    for phrase in FORBIDDEN_BODY_PHRASES:
        if phrase in body_lower:
            issues.append(f"FORBIDDEN PHRASE: '{phrase}'")
            break

    # Fabrication heuristic — flag capitalised word before story verb
    fabrication_patterns = ["achieved", "reported", "saw", "experienced", "noted", "eliminated"]
    for pat in fabrication_patterns:
        if pat in body_lower:
            words = body.split()
            for idx, w in enumerate(words):
                if w.lower() == pat and idx > 0:
                    prev = words[idx - 1].rstrip(".,")
                    if prev and prev[0].isupper() and prev.lower() not in [
                        "heymarket", "uhaul", "u-haul", "salesforce", "hubspot",
                        "zapier", "klaviyo", "attentive", "braze", "brevo", "they"
                    ]:
                        issues.append(f"POSSIBLE HALLUCINATION: '{prev} {pat}' — verify against proof bank")
            break

    hard_failures = [i for i in issues if i.startswith("OVER") or i.startswith("FORBIDDEN")]
    passed = len(hard_failures) == 0
    return passed, issues





# ── Search space parsing ──────────────────────────────────────────────────────

def parse_search_space(program_md, persona):
    """Extract the 8 pain angles for a persona from the SEARCH SPACE section in program.md."""
    persona_display = persona.replace("_", " ").title()
    marker = f"SEARCH SPACE — {persona_display}"

    if marker not in program_md:
        for line in program_md.splitlines():
            if "SEARCH SPACE" in line and persona.replace("_", " ").lower() in line.lower():
                marker = line.strip().lstrip("#").strip()
                break

    if marker not in program_md:
        print(f"ERROR: Could not find SEARCH SPACE for persona '{persona}' in program.md")
        sys.exit(1)

    section_start = program_md.index(marker)
    rest = program_md[section_start:]
    angles = []
    for line in rest.splitlines()[1:]:
        stripped = line.strip()
        if stripped.startswith("Pain angle") and ":" in stripped:
            angles.append(stripped.split(":", 1)[1].strip())
        elif stripped.startswith("##") and angles:
            break
    return angles


# ── Log resume parsing ────────────────────────────────────────────────────────

def parse_log_resume(log_path, iterations, touch_schedule):
    """
    Parse existing log to determine which touches are complete and
    reconstruct their best variants + scores.
    Returns: dict keyed by touch_num -> {winner_text, best_score, complete}
    """
    result = {}
    if not os.path.exists(log_path):
        return result

    with open(log_path, "r", encoding="utf-8") as f:
        content = f.read()

    for touch_num, day, touch_type in touch_schedule:
        touch_key = f"Touch {touch_num} —"
        # Count how many iterations are logged for this touch
        iter_count = content.count(f"## Touch {touch_num} — Iteration")
        if iter_count == 0:
            continue

        # Find best variant across all logged iterations for this touch
        best_score = 0
        best_text = ""

        # Split on touch sections
        touch_marker = f"## Touch {touch_num} — Iteration"
        parts = content.split(touch_marker)
        for part in parts[1:]:
            # Find the winner variant text and score from this iteration
            winner_line = ""
            for line in part.splitlines():
                if line.startswith("Winner: Variant "):
                    winner_line = line
                    break

            if not winner_line:
                continue

            winner_letter = winner_line.split("Winner: Variant ")[1][0].upper()
            # Extract score
            score = 0
            for line in part.splitlines():
                if line.startswith("Winner: Variant ") and "Score:" in line:
                    score_part = line.split("Score:")[1].strip()
                    try:
                        score = int(score_part.split("/")[0].strip())
                    except ValueError:
                        print("    WARNING: Could not parse score from log entry, skipping")
                    break

            # Extract variant text
            variant_marker = f"### Variant {winner_letter}"
            if variant_marker in part:
                var_start = part.index(variant_marker)
                var_section = part[var_start:]
                var_lines = var_section.splitlines()[1:]
                body_lines = []
                for line in var_lines:
                    if line.startswith("### Variant") or line.startswith("### Result"):
                        break
                    if "hook=" in line:
                        break
                    body_lines.append(line)
                var_text = "\n".join(body_lines).strip()

                if score > best_score:
                    best_score = score
                    best_text = var_text

        complete = (iter_count >= iterations)
        result[touch_num] = {
            "winner_text": best_text,
            "best_score": best_score,
            "iter_count": iter_count,
            "complete": complete
        }

    return result


# ── API calls ─────────────────────────────────────────────────────────────────

def build_iteration_history_block(iteration_history):
    """
    Build a full scored history block from all prior iterations.
    iteration_history: list of dicts, one per completed iteration:
      {iter_num, variants: {a/b/c: text}, scores: {variant_a/b/c: dict}, winner, reasoning, weakest_criterion}
    """
    if not iteration_history:
        return ""

    block = "ITERATION HISTORY — use this to understand what has and hasn't worked:\n"
    block += "(Study the score patterns across all variants, not just winners. "
    block += "High-scoring criteria show what works. Low-scoring criteria show what to fix.)\n\n"

    for entry in iteration_history:
        block += f"--- Iteration {entry['iter_num']} ---\n"
        for letter in ["a", "b", "c"]:
            vtext = entry["variants"].get(letter, "")
            vscores = entry["scores"].get(f"variant_{letter}", {})
            score_str = " | ".join([
                f"hook={vscores.get('hook','?')}",
                f"relevance={vscores.get('relevance','?')}",
                f"proof={vscores.get('proof','?')}",
                f"cta={vscores.get('cta','?')}",
                f"brevity={vscores.get('brevity','?')}",
                f"tone={vscores.get('tone','?')}",
                f"total={vscores.get('total','?')}/60"
            ])
            hyp = get_hypothesis(vtext)
            subj = get_subject(vtext)
            # Strip hypothesis and subject lines from body for compactness
            body_lines = []
            for line in vtext.splitlines():
                if line.startswith("Hypothesis") or line.lower().startswith("subject:"):
                    continue
                body_lines.append(line)
            body = "\n".join(body_lines).strip()
            winner_marker = " ← WINNER" if letter == entry["winner"] else ""
            block += f"\nVariant {letter.upper()}{winner_marker}\n"
            if hyp:
                block += f"{hyp}\n"
            block += f"{subj}\n"
            block += f"Scores: {score_str}\n"
            block += f"{body}\n"

        block += f"\nWinner: Variant {entry['winner'].upper()} | Reasoning: {entry['reasoning']}\n"
        block += f"Weakest criterion this iteration: {entry['weakest_criterion']}\n\n"

    # Add pattern summary instructions
    block += "---\n"
    block += "Pattern analysis instructions:\n"
    block += "- Which criteria are consistently low across all variants? Fix those first.\n"
    block += "- Which structural choices (question opener, stat opener, story opener) score highest on hook?\n"
    block += "- Which variants scored highest on proof? What made them specific?\n"
    block += "- Do NOT repeat any subject line, hook structure, or opening sentence already tried.\n"
    block += "- Your goal is to improve on the highest total score so far.\n\n"

    return block


def call_generate(client, context_files, persona, vertical,
                  touch_num, day, touch_type,
                  locked_winners, current_winner_score,
                  iteration_history, unused_angles, angles_used_in_sequence,
                  proof_bank, learnings_block="", retry_note=""):
    """Generate 3 email variants for a touch. Returns raw Claude response text containing <variant_a/b/c> blocks."""
    product_knowledge, program_md, icp_md, persona_md = context_files
    persona_display = persona.replace("_", " ").title()

    # Compact persona brief and ICP snapshot (not full files)
    persona_brief = build_persona_brief(persona_md)
    icp_snapshot = build_icp_snapshot(icp_md)

    # Prior touches: subject + first content line only
    prior_context = ""
    if locked_winners:
        prior_context = "PRIOR TOUCHES (avoid repeating these angles, hooks, subjects):\n"
        for i, wt in enumerate(locked_winners):
            t_num, _, _ = TOUCH_SCHEDULE[i]
            subj = get_subject(wt)
            first_line = ""
            for line in wt.splitlines():
                l = line.strip()
                if l and not l.startswith("Hypothesis") and not l.lower().startswith("subject:"):
                    first_line = l[:80]
                    break
            prior_context += f"  T{t_num}: {subj} | {first_line}\n"

    # Angle instruction
    if touch_num == 1:
        unused_text = "\n".join(f"  - {a}" for a in unused_angles) if unused_angles else "  (all angles used — all available again)"
        angle_instruction = f"ANGLE: Pick one unused pain angle per variant (each variant uses a DIFFERENT angle):\n{unused_text}"
    else:
        used_text = "\n".join(f"  - {a[:80]}" for a in angles_used_in_sequence) if angles_used_in_sequence else "  (none yet)"
        angle_instruction = f"ANGLE: Touch {touch_num} — {touch_type}. Follow the {persona_display} sequence arc. Already used:\n{used_text}"

    proof_lines = "\n".join(f"  • {p}" for p in proof_bank)
    proof_block = f"PROOF BANK — cite VERBATIM, exact company names only, no additions:\n{proof_lines}"

    # Distilled learnings (replaces raw iteration history after first run)
    if learnings_block:
        history_section = f"WHAT WE LEARNED (distilled from prior iterations):\n{learnings_block}"
    else:
        history_section = build_iteration_history_block(iteration_history)

    retry_block = f"\n⚠ RETRY — REJECTED FOR:\n{retry_note}\nFix every violation above before outputting.\n" if retry_note else ""

    prompt = f"""{persona_brief}
{icp_snapshot}
{proof_block}

{prior_context}
{history_section}
{retry_block}
---
Generate 3 variants for Touch {touch_num} (Day {day} — {touch_type}). Target: {persona_display} at a {vertical} company.
{angle_instruction}

Before each variant state: Hypothesis [A/B/C]: [what you're testing and why it should score higher]
No repeated subject lines, hooks, or openers from prior touches or history.

<variant_a>
Hypothesis A: ...
Subject: ...
[body — 100 words max]
[SIGNATURE]
</variant_a>
<variant_b>
Hypothesis B: ...
Subject: ...
[body — 100 words max]
[SIGNATURE]
</variant_b>
<variant_c>
Hypothesis C: ...
Subject: ...
[body — 100 words max]
[SIGNATURE]
</variant_c>"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=build_rules_digest(),
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


def call_evaluate(client, variants_text, persona, vertical, touch_num, day, touch_type):
    """Score all 3 variants on 6 criteria and pick the winner. Returns raw Claude response text with a <scores> block."""
    persona_display = persona.replace("_", " ").title()

    prompt = f"""You are a skeptical {persona_display} at a $300M {vertical} company.
You receive 40+ cold emails per day. You delete most in 3 seconds.
You have no patience for vague claims, fake urgency, or vendor pitches that don't understand your world.

Score these 3 variants for Touch {touch_num} (Day {day} — {touch_type}) of an 8-touch cold outreach sequence targeting {persona_display}.

Score each on these 6 criteria (1-10 each):
- Hook strength: does line 1 earn a read in 5 seconds?
- Persona relevance: does this know the {persona_display}'s actual pain?
- Proof: is there a specific customer, stat, or integration — not a claim?
- CTA quality: single reply-only ask, zero friction? Penalise heavily for any call/meeting/demo/time ask.
- Brevity: under 100 words body only, no filler phrases?
- Human tone: does this sound like a person or a template?

After scoring all 3, identify the winner (highest total out of 60).
Give one sentence of reasoning for why the winner beat the others.
Identify the single weakest criterion across all variants and why.

VARIANTS TO EVALUATE:
{variants_text}

Output format — use these exact delimiters:
<scores>
variant_a: hook=X|relevance=X|proof=X|cta=X|brevity=X|tone=X|total=X
variant_b: hook=X|relevance=X|proof=X|cta=X|brevity=X|tone=X|total=X
variant_c: hook=X|relevance=X|proof=X|cta=X|brevity=X|tone=X|total=X
winner: [a/b/c]
reasoning: [one sentence]
weakest_criterion: [criterion name] — [one sentence explanation]
</scores>"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


def call_distill(client, iteration_history, touch_num, touch_type, persona):
    """
    After a touch completes, distill the raw iteration history into a compact
    learnings block (~150 words). Future runs load this instead of raw history.
    Karpathy-style: synthesize, don't accumulate.
    """
    persona_display = persona.replace("_", " ").title()
    raw_history = build_iteration_history_block(iteration_history)

    prompt = f"""You just ran {len(iteration_history)} iterations optimizing Touch {touch_num} ({touch_type}) for {persona_display} cold outreach.

Here is the full iteration history:
{raw_history}

Distill this into a compact learnings block (150 words max). Focus on:
1. What hook structures scored highest on hook criterion? (question / stat / story)
2. What made proof-heavy variants score higher or lower on proof?
3. What CTA patterns scored best?
4. What tone patterns scored best on human tone?
5. What to avoid — patterns that consistently scored low.

Write this as a compact, specific reference for the NEXT run — not a summary of what happened, but actionable guidance.
Format as 5 numbered bullets. Be specific (e.g. "stat openers scored hook=8-9" not "good hooks work")."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text.strip()


def load_learnings(learnings_path):
    """Load distilled learnings file if it exists."""
    if not os.path.exists(learnings_path):
        return ""
    with open(learnings_path, "r", encoding="utf-8") as f:
        return f.read().strip()


def save_learnings(learnings_path, touch_num, touch_type, distilled):
    """Append distilled learnings for a touch to the learnings file."""
    os.makedirs(os.path.dirname(learnings_path), exist_ok=True)
    with open(learnings_path, "a", encoding="utf-8") as f:
        f.write(f"\n## Touch {touch_num} — {touch_type}\n")
        f.write(distilled)
        f.write("\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def call_derive(client, icp_text, product_knowledge):
    """
    Given a filled ICP template + product knowledge, auto-generate:
    - A persona brief (it_director.md style)
    - A structured ICP file
    - 8 pain angles for each persona

    Returns a dict: {vertical, persona_slug, persona_md, icp_md, angles}
    """
    prompt = f"""You are building a cold outreach system for Heymarket, a business texting platform.

Here is a new ICP (Ideal Customer Profile) the user wants to target:
{icp_text}

Here is Heymarket's product knowledge (features, proof points, integrations):
{product_knowledge[:3000]}

Your job is to derive everything needed to run the outreach optimizer for the PRIMARY buyer persona listed in the ICP.

Output the following sections using these exact delimiters:

<vertical>
[single word slug, e.g. "healthcare"]
</vertical>

<persona_slug>
[snake_case slug of the primary buyer persona, e.g. "it_director"]
</persona_slug>

<icp_md>
# ICP — [Vertical Name]

## Sub-industries in scope
[list]

## Company size
[employees, revenue, segment]

## Tech stack signals
[P1/P2 priority stack]

## Top use cases
[numbered list with frequency/weight if known]

## Qualification signals
[bullet list]

## Competitors
[bullet list with one-line differentiation]
</icp_md>

<persona_md>
# Persona: [Title] — [Vertical]

## Role and Scope
[2-3 sentences on what they own and their buying authority]

## Relationship to Heymarket
[Buyer vs user. Their key questions. What wins them.]

## Day-to-Day Pressures
[5-7 bullet points — specific operational pains]

## Key Objections and Responses
[3-4 objections with one-line responses]

## Language They Use
[10-15 terms they actually use — jargon, acronyms]

## What Makes Them Reply to a Cold Email
[3-4 bullet points of reply triggers]
[3-4 bullet points of delete triggers]

## Best Heymarket Proof Points for This Persona
[5-6 specific proof points]

## Sequence Arc
T1–T2: [theme]
T3–T4: [theme]
T5–T6: [theme]
T7–T8: [theme]
</persona_md>

<angles>
Pain angle 1: **[name]** — [1-2 sentence description of the pain, specific to this vertical and persona]
Pain angle 2: **[name]** — [description]
Pain angle 3: **[name]** — [description]
Pain angle 4: **[name]** — [description]
Pain angle 5: **[name]** — [description]
Pain angle 6: **[name]** — [description]
Pain angle 7: **[name]** — [description]
Pain angle 8: **[name]** — [description]
</angles>"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.content[0].text

    result = {
        "vertical": extract_between(raw, "<vertical>", "</vertical>").strip().lower(),
        "persona_slug": extract_between(raw, "<persona_slug>", "</persona_slug>").strip().lower(),
        "icp_md": extract_between(raw, "<icp_md>", "</icp_md>").strip(),
        "persona_md": extract_between(raw, "<persona_md>", "</persona_md>").strip(),
        "angles": extract_between(raw, "<angles>", "</angles>").strip(),
    }
    return result


def main():
    """CLI entry point. Parses args, loads context, runs the iterative optimization loop, and writes the final sequence file."""
    parser = argparse.ArgumentParser(description="Heymarket outreach sequence optimizer")
    parser.add_argument("--vertical", required=False)
    parser.add_argument("--persona", required=False)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--touches", type=int, default=8, help="How many touches to run (default: 8)")
    parser.add_argument("--derive", action="store_true", help="Derive persona/ICP files from an ICP template")
    parser.add_argument("--icp", help="Path to filled ICP template (used with --derive)")
    args = parser.parse_args()

    # ── Derive mode: generate persona + ICP files from a template ────────────
    if args.derive:
        if not args.icp:
            print("ERROR: --derive requires --icp <path_to_template>")
            sys.exit(1)
        icp_text = load_file(args.icp)
        product_knowledge = load_file("shared/product_knowledge.md")

        api_key = load_api_key()
        client = anthropic.Anthropic(api_key=api_key)
        print("Deriving persona, ICP, and pain angles from template...")
        derived = call_derive(client, icp_text, product_knowledge)

        vertical = derived["vertical"]
        persona_slug = derived["persona_slug"]

        if not vertical or not persona_slug:
            print("ERROR: Could not parse vertical or persona_slug from derive output.")
            sys.exit(1)

        # Write files
        icp_path = f"verticals/{vertical}/icp_{vertical}.md"
        persona_path = f"verticals/{vertical}/personas/{persona_slug}.md"
        program_path = "shared/program.md"

        write_file(icp_path, derived["icp_md"])
        write_file(persona_path, derived["persona_md"])
        print(f"ICP written:     {icp_path}")
        print(f"Persona written: {persona_path}")

        # Append the new search space to program.md
        persona_display = persona_slug.replace("_", " ").title()
        angles_section = f"\n\n## SEARCH SPACE — {persona_display} ({vertical})\n\n"
        angles_section += f"Use these 8 pain angles to generate T1 variants. Rotate through angles across iterations. No angle repeated until all 8 have been used.\n\n"
        angles_section += derived["angles"]
        with open(program_path, "a", encoding="utf-8") as f:
            f.write(angles_section)
        print(f"Pain angles appended to: {program_path}")
        print()
        print(f"Done. Run: python optimize.py --vertical {vertical} --persona {persona_slug}")
        return

    if not args.vertical or not args.persona:
        print("ERROR: --vertical and --persona are required (or use --derive --icp <template>)")
        sys.exit(1)

    vertical = args.vertical.lower()
    persona = args.persona.lower()
    iterations = max(1, args.iterations)

    product_knowledge_path = "shared/product_knowledge.md"
    program_path = "shared/program.md"
    icp_path = f"verticals/{vertical}/icp_{vertical}.md"
    persona_path = f"verticals/{vertical}/personas/{persona}.md"
    log_path = f"verticals/{vertical}/logs/log_{persona}_opener.md"
    sequence_path = f"verticals/{vertical}/sequences/sequence_{persona}.md"
    learnings_path = f"verticals/{vertical}/learnings/learnings_{persona}.md"

    # ── Step 0: Startup checks ────────────────────────────────────────────────

    product_knowledge = load_file(product_knowledge_path)
    program_md = load_file(program_path)
    icp_md = load_file(icp_path)
    persona_md = load_file(persona_path)
    context_files = (product_knowledge, program_md, icp_md, persona_md)
    clean_bank = load_proof_bank("shared/proof_bank.md")
    if clean_bank:
        proof_bank = clean_bank
        print(f"Proof bank loaded: {len(proof_bank)} verified proof points (from proof_bank.md)")
    else:
        proof_bank = extract_proof_bank(product_knowledge)
        print(f"Proof bank loaded: {len(proof_bank)} proof points (extracted from product_knowledge — add shared/proof_bank.md for cleaner results)")
    learnings = load_learnings(learnings_path)
    if learnings:
        print(f"Learnings loaded: {len(learnings.split())} words from prior runs")
    else:
        print("No prior learnings found — starting fresh")

    wc = word_count(product_knowledge)
    if wc < 300:
        print(f"WARNING: product_knowledge.md looks thin ({wc} words). Run scrape.py to improve results. Proceeding anyway.")

    active_touches = TOUCH_SCHEDULE[:max(1, min(args.touches, len(TOUCH_SCHEDULE)))]
    total_api_calls = iterations * 2 * len(active_touches)
    print(f"Vertical:             {vertical}")
    print(f"Persona:              {persona}")
    print(f"Iterations per touch: {iterations}")
    print(f"Touches:              {len(TOUCH_SCHEDULE)}")
    print(f"Total API calls:      {total_api_calls} ({iterations} iterations × 2 calls × {len(TOUCH_SCHEDULE)} touches)")
    print(f"Log:                  {log_path}")
    print()

    # ── Step 1: Search space + resume state ───────────────────────────────────

    angles = parse_search_space(program_md, persona)
    resume_state = parse_log_resume(log_path, iterations, active_touches)

    if resume_state:
        completed = [t for t, s in resume_state.items() if s["complete"]]
        if completed:
            print(f"Resuming: touches {completed} already complete, skipping.")
            print()

    # Load API key
    api_key = load_api_key()
    client = anthropic.Anthropic(api_key=api_key)

    # ── Step 2: Iterative optimization loop — all 8 touches ──────────────────

    locked_winners = []          # best variant text per touch, in order
    touch_best_scores = {}       # touch_num -> best score achieved
    touch_score_progression = {} # touch_num -> [score iter1, score iter2, ...]
    used_angles = []             # for T1 angle rotation
    angles_used_in_sequence = [] # angles used across locked touches (for T2+)

    for touch_num, day, touch_type in active_touches:

        # Check resume: if this touch is fully complete, load its winner and skip
        if touch_num in resume_state and resume_state[touch_num]["complete"]:
            state = resume_state[touch_num]
            locked_winners.append(state["winner_text"])
            touch_best_scores[touch_num] = state["best_score"]
            touch_score_progression[touch_num] = [state["best_score"]]
            print(f"Touch {touch_num} — already complete (best score: {state['best_score']}/60). Skipping.")
            continue

        print(f"═══ Touch {touch_num} — Day {day} — {touch_type} ═══")

        current_winner = None
        current_winner_score = 0
        iteration_history = []   # full scored record of every iteration for this touch
        first_iter_score = None
        score_progression = []

        # Determine starting iteration (partial resume)
        start_iter = 1
        if touch_num in resume_state:
            start_iter = resume_state[touch_num]["iter_count"] + 1
            current_winner = resume_state[touch_num]["winner_text"]
            current_winner_score = resume_state[touch_num]["best_score"]
            print(f"  Resuming from iteration {start_iter} (current best: {current_winner_score}/60)")

        for i in range(start_iter, iterations + 1):
            print(f"  Iteration {i}/{iterations}")

            # Determine unused angles for T1
            unused_angles = [a for a in angles if a not in used_angles]
            if not unused_angles:
                used_angles = []
                unused_angles = angles[:]

            # Call A: Generate — pass full iteration history + proof bank
            print(f"    Generating 3 variants...")
            gen_response = call_generate(
                client, context_files, persona, vertical,
                touch_num, day, touch_type,
                locked_winners, current_winner_score,
                iteration_history, unused_angles, angles_used_in_sequence,
                proof_bank, learnings_block=learnings
            )
            variants = parse_variants(gen_response)

            # Validate — collect violations per variant
            validation_notes = {}
            any_hard_failure = False
            for letter in ["a", "b", "c"]:
                _, issues = validate_variant(variants.get(letter, ""))
                validation_notes[letter] = issues
                if issues and any(i.startswith("OVER") or i.startswith("FORBIDDEN") for i in issues):
                    any_hard_failure = True

            # If hard violations found → single retry with explicit correction note
            if any_hard_failure:
                violations_desc = []
                for letter in ["a", "b", "c"]:
                    issues = validation_notes.get(letter, [])
                    hard = [i for i in issues if i.startswith("OVER") or i.startswith("FORBIDDEN")]
                    if hard:
                        violations_desc.append(f"Variant {letter.upper()}: {'; '.join(hard)}")
                retry_note = (
                    "Your previous variants were REJECTED for these violations:\n"
                    + "\n".join(f"  - {v}" for v in violations_desc)
                    + "\n\nSTRICT REQUIREMENTS FOR THIS RETRY:\n"
                    + "- Count every word in the body before outputting. Body must be 100 words or fewer. Count does NOT include Hypothesis, Subject, or [SIGNATURE].\n"
                    + "- Do NOT ask for a call, phone call, meeting, demo, screen share, or any synchronous interaction.\n"
                    + "- The CTA must be a single question answerable by a one-line email reply. Nothing else.\n"
                    + "- If you cannot make the point in 100 words, cut a sentence. Do not compress.\n"
                    + "- Variants still over 100 words after this retry will be truncated."
                )
                print(f"    ✗ Hard violations detected — retrying with correction note...")
                gen_response = call_generate(
                    client, context_files, persona, vertical,
                    touch_num, day, touch_type,
                    locked_winners, current_winner_score,
                    iteration_history, unused_angles, angles_used_in_sequence,
                    proof_bank, learnings_block=learnings, retry_note=retry_note
                )
                variants = parse_variants(gen_response)

                # Re-validate after retry
                validation_notes = {}
                for letter in ["a", "b", "c"]:
                    _, issues = validate_variant(variants.get(letter, ""))
                    validation_notes[letter] = issues

                # Last resort: truncate any still-over-limit variants at sentence boundary
                for letter in ["a", "b", "c"]:
                    issues = validation_notes.get(letter, [])
                    if any(i.startswith("OVER") for i in issues):
                        body = get_body_only(variants[letter])
                        truncated = truncate_at_sentence(body, max_words=100)
                        # Rebuild variant with truncated body
                        hyp = get_hypothesis(variants[letter])
                        subj = get_subject(variants[letter])
                        variants[letter] = f"{hyp}\n{subj}\n{truncated}\n[SIGNATURE]"
                        validation_notes[letter].append("TRUNCATED at sentence boundary (last resort)")
                        print(f"    ⚠ Variant {letter.upper()} truncated at sentence boundary")

            # Print validation status
            all_cta_failed = True
            for letter in ["a", "b", "c"]:
                issues = validation_notes.get(letter, [])
                passed = not any(i.startswith("OVER") or i.startswith("FORBIDDEN") for i in issues)
                if passed:
                    all_cta_failed = False
                subj = get_subject(variants.get(letter, ""))
                status = "✓" if passed else "✗"
                issue_str = f" [{'; '.join(issues)}]" if issues else ""
                print(f"    Variant {letter.upper()} {status}: {subj}{issue_str}")

            # If all 3 still fail CTA after retry → skip scoring, carry forward previous winner
            cta_all_failed = all(
                any(i.startswith("FORBIDDEN") for i in validation_notes.get(l, []))
                for l in ["a", "b", "c"]
            )
            if cta_all_failed:
                print(f"    ✗ All 3 variants failed CTA validation after retry — skipping scoring, carrying forward previous winner.")
                log_entry = f"\n## Touch {touch_num} — Iteration {i} [SKIPPED — all variants failed CTA]\n\n"
                append_to_file(log_path, log_entry)
                continue

            # Call B: Evaluate (only compliant variants)
            print(f"    Evaluating...")
            variants_text = "\n\n".join([
                f"VARIANT {l.upper()}:\n{variants.get(l, '')}" for l in ["a", "b", "c"]
            ])
            eval_response = call_evaluate(
                client, variants_text, persona, vertical,
                touch_num, day, touch_type
            )

            scores = parse_scores(eval_response)
            winner_letter = scores.get("winner", "a").strip().lower()
            if winner_letter not in ["a", "b", "c"]:
                print(f"    WARNING: Unexpected winner value '{winner_letter}' — defaulting to 'a'")
                winner_letter = "a"

            winner_text = variants.get(winner_letter, "")
            winner_score_dict = scores.get(f"variant_{winner_letter}", {})
            iter_winner_score = get_total(winner_score_dict)

            if first_iter_score is None:
                first_iter_score = iter_winner_score
            score_progression.append(iter_winner_score)

            # Update best if improved
            if iter_winner_score > current_winner_score:
                current_winner = winner_text
                current_winner_score = iter_winner_score

            weakest = scores.get("weakest_criterion", "")

            # Append full iteration record to history
            iteration_history.append({
                "iter_num": i,
                "variants": variants,
                "scores": scores,
                "winner": winner_letter,
                "reasoning": scores.get("reasoning", ""),
                "weakest_criterion": weakest,
            })

            # Update used angles for T1
            if touch_num == 1:
                hyp = get_hypothesis(winner_text).lower()
                for angle in angles:
                    words = angle.lower().split()[:4]
                    if any(w in hyp for w in words if len(w) > 4):
                        if angle not in used_angles:
                            used_angles.append(angle)
                        break

            print(f"    Winner: Variant {winner_letter.upper()} | Score: {iter_winner_score}/60 | Best so far: {current_winner_score}/60")
            print(f"    Weakest: {weakest[:80]}")
            print()

            # Build log entry for this iteration
            log_entry = f"\n## Touch {touch_num} — Iteration {i}\n\n"
            for letter in ["a", "b", "c"]:
                vtext = variants.get(letter, "")
                vscores = scores.get(f"variant_{letter}", {})
                winner_marker = " ← WINNER" if letter == winner_letter else ""
                issues = validation_notes.get(letter, [])
                flag = " ⚠ " + " | ".join(issues) if issues else ""
                log_entry += f"### Variant {letter.upper()}{winner_marker}{flag}\n"
                hyp = get_hypothesis(vtext)
                subj = get_subject(vtext)
                body = get_body_only(vtext)
                if hyp:
                    log_entry += f"{hyp}\n\n"
                if subj and subj != "Subject: not found":
                    log_entry += f"{subj}\n"
                log_entry += f"{body}\n\n"
                log_entry += f"{score_to_string(vscores)}\n\n"

            log_entry += f"### Result\n"
            log_entry += f"Winner: Variant {winner_letter.upper()} | Score: {iter_winner_score}/60\n"
            log_entry += f"Reasoning: {scores.get('reasoning', '')}\n"
            log_entry += f"Weakest criterion: {weakest}\n"
            log_entry += f"Best score for Touch {touch_num} so far: {current_winner_score}/60\n\n"

            append_to_file(log_path, log_entry)

        # Lock this touch
        locked_winners.append(current_winner)
        touch_best_scores[touch_num] = current_winner_score
        touch_score_progression[touch_num] = score_progression

        # Track angle used in this touch for sequence diversity
        hyp_line = get_hypothesis(current_winner)
        if hyp_line and hyp_line not in angles_used_in_sequence:
            angles_used_in_sequence.append(hyp_line[:100])

        # Distill learnings from this touch — Karpathy-style: synthesize, don't accumulate
        if iteration_history:
            print(f"    Distilling learnings from Touch {touch_num}...")
            distilled = call_distill(client, iteration_history, touch_num, touch_type, persona)
            save_learnings(learnings_path, touch_num, touch_type, distilled)
            # Reload so next touch benefits immediately
            learnings = load_learnings(learnings_path)
            print(f"    Learnings saved to {learnings_path}")

        print(f"Touch {touch_num} locked. Best score: {current_winner_score}/60 (started at {first_iter_score}/60)")
        print()

    # ── Step 3: Assemble sequence file ───────────────────────────────────────

    sequence_content = f"# Sequence: {persona} | Vertical: {vertical}\n"
    sequence_content += f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
    sequence_content += f"Iterations per touch: {iterations}\n\n"

    sequence_content += "## Touch scores\n"
    for touch_num, day, touch_type in active_touches:
        score = touch_best_scores.get(touch_num, "?")
        sequence_content += f"T{touch_num}: {score}/60\n"
    sequence_content += "\n---\n\n"

    for idx, (touch_num, day, touch_type) in enumerate(active_touches):
        raw_winner = locked_winners[idx] if idx < len(locked_winners) else "(not generated)"
        best_score = touch_best_scores.get(touch_num, "?")
        # Strip hypothesis line — sequence is the deliverable, not the research log
        subj = get_subject(raw_winner)
        body = get_body_only(raw_winner)
        if raw_winner == "(not generated)":
            clean_winner = raw_winner
        elif subj and subj != "Subject: not found":
            clean_winner = f"{subj}\n\n{body}\n\n[SIGNATURE]"
        else:
            clean_winner = f"{body}\n\n[SIGNATURE]"
        sequence_content += f"TOUCH {touch_num} — Day {day} — {touch_type} | Score: {best_score}/60\n"
        sequence_content += f"{clean_winner}\n\n---\n\n"

    write_file(sequence_path, sequence_content)
    print(f"Sequence written to: {sequence_path}")

    # ── Step 4: End-of-run summary ────────────────────────────────────────────

    weakest_touch_num = min(touch_best_scores, key=touch_best_scores.get) if touch_best_scores else "?"
    weakest_score = touch_best_scores.get(weakest_touch_num, "?")

    score_line = "  ".join([f"T{t}: {touch_best_scores.get(t, '?')}/60" for t, _, _ in active_touches])

    improvement_lines = []
    for touch_num, _, _ in active_touches:
        prog = touch_score_progression.get(touch_num, [])
        if len(prog) >= 2:
            improvement_lines.append(f"  T{touch_num}: {prog[0]}/60 → {prog[-1]}/60")
        elif len(prog) == 1:
            improvement_lines.append(f"  T{touch_num}: {prog[0]}/60 (1 iteration)")

    summary = f"""
═══════════════════════════════════════
RUN COMPLETE
Vertical:             {vertical}
Persona:              {persona}
Iterations per touch: {iterations}
Total API calls:      {total_api_calls}

Touch scores (best variant per touch):
  {score_line}

Score progression per touch:
{chr(10).join(improvement_lines)}

Weakest touch overall: T{weakest_touch_num} — {weakest_score}/60
Sequence file:         {sequence_path}
Log file:              {log_path}
═══════════════════════════════════════
"""

    print(summary)
    append_to_file(log_path, summary)


if __name__ == "__main__":
    main()
