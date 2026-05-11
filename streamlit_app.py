"""
Streamlit UI for Autoloop — 6-page app for managing cold email sequences.

Pages: Dashboard, View Sequence, Run Sequence, New Vertical, Review Sequence,
       Knowledge Base (shared knowledge files).

Calls optimize.py via subprocess. Reads/writes verticals/ and shared/.
"""
import streamlit as st
import os
import re
import subprocess
import tempfile
import json
import copy
import datetime
import anthropic
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
VERTICALS_DIR = BASE_DIR / "verticals"
SHARED_DIR = BASE_DIR / "shared"
MODEL = "claude-sonnet-4-20250514"

load_dotenv(BASE_DIR / ".env")

def _get_api_key() -> str:
    """Return ANTHROPIC_API_KEY from environment. Raises RuntimeError if missing."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it as an environment variable.")
    return key

st.set_page_config(
    page_title="Autoloop",
    page_icon="✉",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def to_title(slug: str) -> str:
    """Convert underscore slug to title-cased display name."""
    return slug.replace("_", " ").title()

def get_verticals() -> list[dict]:
    """Scan verticals/ and return structured list of verticals with their personas and sequence status."""
    if not VERTICALS_DIR.exists():
        return []
    result = []
    for vdir in sorted(VERTICALS_DIR.iterdir()):
        if not vdir.is_dir():
            continue
        vertical = vdir.name
        personas = []
        personas_dir = vdir / "personas"
        if personas_dir.exists():
            for pfile in sorted(personas_dir.glob("*.md")):
                slug = pfile.stem
                seq_path = vdir / "sequences" / f"sequence_{slug}.md"
                scores = []
                if seq_path.exists():
                    content = seq_path.read_text()
                    scores = [int(m) for m in re.findall(r"T\d+:\s*(\d+)/60", content)]
                personas.append({"slug": slug, "has_sequence": seq_path.exists(), "scores": scores})
        result.append({"vertical": vertical, "personas": personas})
    return result

def parse_sequence(vertical: str, persona: str) -> list[dict]:
    seq_path = VERTICALS_DIR / vertical / "sequences" / f"sequence_{persona}.md"
    if not seq_path.exists():
        return []
    content = seq_path.read_text()
    touches = []
    for block in content.split("\n---\n"):
        block = block.strip()
        m = re.search(r"TOUCH\s+(\d+)\s*[—-]\s*Day\s+(\d+)\s*[—-]\s*([^|]+)\|\s*Score:\s*(\d+)/60", block)
        if not m:
            continue
        touch_num, day, touch_type, score = int(m[1]), int(m[2]), m[3].strip(), int(m[4])
        subj_m = re.search(r"^Subject:\s*(.+)$", block, re.MULTILINE | re.IGNORECASE)
        subject = subj_m[1].strip() if subj_m else ""
        lines = block.split("\n")
        subj_idx = next((i for i, l in enumerate(lines) if l.lower().startswith("subject:")), -1)
        sig_idx = next((i for i, l in enumerate(lines) if l.strip() == "[SIGNATURE]"), len(lines))
        body_lines = lines[subj_idx + 1: sig_idx] if subj_idx >= 0 else []
        body = "\n".join(body_lines).strip()
        touches.append({"num": touch_num, "day": day, "type": touch_type, "score": score, "subject": subject, "body": body})
    return touches

def load_learnings(vertical: str, persona: str) -> str:
    path = VERTICALS_DIR / vertical / "learnings" / f"learnings_{persona}.md"
    return path.read_text() if path.exists() else ""

def call_claude_editor(vertical: str, persona: str, current_touches: list, chat_history: list) -> dict:
    """Call Claude to make structured edits to the sequence."""
    api_key = _get_api_key()
    client = anthropic.Anthropic(api_key=api_key)
    persona_path = VERTICALS_DIR / vertical / "personas" / f"{persona}.md"
    persona_content = persona_path.read_text() if persona_path.exists() else ""

    seq_text = "\n\n---\n\n".join(
        f"Touch {t['num']} — Day {t['day']} — {t['type']} | Score: {t['score']}/60\n"
        f"Subject: {t['subject']}\n\n{t['body']}\n\n[SIGNATURE]"
        for t in current_touches
    )

    system = (
        "You are a cold email sequence editor. Refine sequences to be concise, human, and persona-relevant.\n\n"
        + (f"BUYER PERSONA:\n{persona_content}\n\n" if persona_content else "")
        + f"CURRENT SEQUENCE ({len(current_touches)} touches):\n{seq_text}\n\n"
        + "RULES:\n- Body ≤100 words\n- Subject: lowercase, 3-6 words\n"
        + "- CTA: reply-only (no meeting/call/demo)\n- Use {{first_name}}\n\n"
        + 'Respond ONLY with raw JSON (no markdown fences):\n'
        + '{"explanation":"...","edits":[{"touchNum":N,"field":"subject"|"body","newValue":"..."}]}\n'
        + 'No changes needed: {"explanation":"...","edits":[]}'
    )

    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=system,
        messages=[{"role": m["role"], "content": m["content"]} for m in chat_history],
    )
    raw = resp.content[0].text.strip() if resp.content else "{}"
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(raw)
    except Exception as e:
        print(f"WARNING: Claude editor returned unparseable JSON: {e}. Raw: {raw[:200]}")
        return {"explanation": raw, "edits": []}

def save_draft(vertical: str, persona: str, touches: list):
    """Write current touches back to sequence_{persona}.md so all pages stay in sync."""
    seq_path = VERTICALS_DIR / vertical / "sequences" / f"sequence_{persona}.md"
    if not seq_path.exists():
        return
    parts = [f"# Sequence: {persona} | Vertical: {vertical}"] + [
        f"TOUCH {t['num']} — Day {t['day']} — {t['type']} | Score: {t['score']}/60\n"
        f"Subject: {t['subject']}\n\n{t['body']}\n\n[SIGNATURE]"
        for t in touches
    ]
    seq_path.write_text("\n\n---\n\n".join(parts) + "\n\n---\n")

def call_feedback_distillation(vertical: str, persona: str, original: list, approved: list) -> str:
    """Analyze original→approved diffs and synthesize learnings bullets for the optimizer."""
    changed = [(o, a) for o in original for a in approved
               if o["num"] == a["num"] and (o["subject"] != a["subject"] or o["body"] != a["body"])]
    if not changed:
        return ""

    api_key = _get_api_key()
    client = anthropic.Anthropic(api_key=api_key)

    persona_path = VERTICALS_DIR / vertical / "personas" / f"{persona}.md"
    persona_content = persona_path.read_text() if persona_path.exists() else ""

    diff_text = ""
    for o, a in changed:
        diff_text += f"\n### Touch {o['num']} — {o['type']}\n"
        if o["subject"] != a["subject"]:
            diff_text += f"Subject ORIGINAL: {o['subject']}\nSubject APPROVED: {a['subject']}\n"
        if o["body"] != a["body"]:
            diff_text += f"Body ORIGINAL:\n{o['body']}\n\nBody APPROVED:\n{a['body']}\n"

    date_str = datetime.datetime.now().strftime("%Y-%m-%d")

    system = (
        "You are analyzing what a human reviewer changed in a cold email sequence. "
        "Your output will be appended directly to the optimizer's learnings file and used to guide future sequence generation. "
        "Be specific, pattern-focused, and actionable.\n\n"
        + (f"BUYER PERSONA:\n{persona_content[:2000]}\n\n" if persona_content else "")
        + "For each changed touch, identify:\n"
        "- What language/structure patterns did the human KEEP or strengthen (signals they work)\n"
        "- What did they REMOVE or replace (signals to avoid)\n"
        "- What this implies for future hook / proof / CTA / tone choices\n\n"
        "Output ONLY the markdown learnings section. Use this exact format for each changed touch:\n\n"
        f"## Human Feedback — Touch N ({date_str})\n"
        "1. **[Pattern name]**: [specific observation about what was kept/changed and why it matters for scoring]\n"
        "2. ...\n"
        "(3–5 bullets per touch. No preamble, no explanation outside the bullets.)"
    )

    resp = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=system,
        messages=[{"role": "user", "content": f"Here are the human-approved changes:\n{diff_text}"}],
    )
    return resp.content[0].text.strip() if resp.content else ""


def serialize_approved(vertical: str, persona: str, touches: list, original: list) -> str:
    """Write approved sequence file, feedback log, and return distilled learnings."""
    now = datetime.datetime.now().isoformat()
    seq_dir = VERTICALS_DIR / vertical / "sequences"
    log_dir = VERTICALS_DIR / vertical / "logs"
    seq_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    parts = [f"# Sequence: {persona} | Vertical: {vertical}\nReviewed: {now}"] + [
        f"TOUCH {t['num']} — Day {t['day']} — {t['type']} | Score: {t['score']}/60\n"
        f"Subject: {t['subject']}\n\n{t['body']}\n\n[SIGNATURE]"
        for t in touches
    ]
    (seq_dir / f"approved_{persona}.md").write_text("\n\n---\n\n".join(parts) + "\n\n---\n")

    changed = [t for t in touches if any(
        o["num"] == t["num"] and (o["subject"] != t["subject"] or o["body"] != t["body"])
        for o in original
    )]
    log = [f"# Review Feedback: {persona} | {vertical}", f"Approved: {now}", ""]
    if not changed:
        log.append("No changes — approved as-is.")
    else:
        log.append(f"## Changes ({len(changed)} touch{'es' if len(changed) != 1 else ''} modified)")
        for t in changed:
            o = next(x for x in original if x["num"] == t["num"])
            log.append(f"\n### Touch {t['num']} — {t['type']}")
            if o["subject"] != t["subject"]:
                log.append(f"**Subject:** ~~{o['subject']}~~ → {t['subject']}")
            if o["body"] != t["body"]:
                log += ["**Body (original):**", f"```\n{o['body']}\n```",
                        "**Body (approved):**", f"```\n{t['body']}\n```"]
    (log_dir / f"review_feedback_{persona}.md").write_text("\n".join(log))

    # Distill human feedback into learnings so the optimizer picks it up on next run
    feedback_learnings = call_feedback_distillation(vertical, persona, original, touches)
    if feedback_learnings:
        learnings_path = VERTICALS_DIR / vertical / "learnings" / f"learnings_{persona}.md"
        learnings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(learnings_path, "a") as f:
            f.write("\n\n" + feedback_learnings)

    return feedback_learnings

def stream_process(cmd: list[str]):
    """Run cmd as a subprocess and stream stdout line-by-line into a Streamlit code block. Returns exit code."""
    output_box = st.empty()
    lines: list[str] = []
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=str(BASE_DIR)
    )
    while True:
        raw = proc.stdout.readline()  # type: ignore[union-attr]
        if not raw:
            break
        line = raw.rstrip()
        lines.append(line)
        output_box.code("\n".join(lines[-80:]), language=None)
    proc.wait()
    output_box.code("\n".join(lines), language=None)
    return proc.returncode

# ── Pages ─────────────────────────────────────────────────────────────────────

def page_dashboard():
    """Show all verticals and personas with sequence scores and quick-action buttons."""
    st.title("Dashboard")
    st.caption("All verticals and personas. Green = score ≥ 45, yellow = ≥ 40, red < 40.")

    verticals = get_verticals()
    all_personas = [
        {"vertical": v["vertical"], **p}
        for v in verticals
        for p in v["personas"]
    ]

    if not all_personas:
        st.info("No verticals found. Use **New Vertical** to get started.")
        st.stop()

    with_seq = [p for p in all_personas if p["has_sequence"]]
    without_seq = [p for p in all_personas if not p["has_sequence"]]

    if with_seq:
        st.subheader("Generated Sequences")
        cols = st.columns(3)
        for i, p in enumerate(with_seq):
            avg = round(sum(p["scores"]) / len(p["scores"])) if p["scores"] else 0
            with cols[i % 3]:
                with st.container(border=True):
                    st.markdown(f"**{to_title(p['slug'])}**")
                    st.caption(p["vertical"])
                    if p["scores"]:
                        st.metric("Avg score", f"{avg}/60", delta=None)
                        score_cols = st.columns(len(p["scores"]))
                        for j, s in enumerate(p["scores"]):
                            score_cols[j].markdown(
                                f"<div style='text-align:center;font-size:11px;color:gray'>T{j+1}</div>"
                                f"<div style='text-align:center;font-size:13px;font-weight:600;"
                                f"color:{'#4ade80' if s>=45 else '#fbbf24' if s>=40 else '#f87171'}'>{s}</div>",
                                unsafe_allow_html=True,
                            )
                    c1, c2, c3 = st.columns(3)
                    if c1.button("View", key=f"view_{p['vertical']}_{p['slug']}", use_container_width=True):
                        st.session_state["seq_vertical"] = p["vertical"]
                        st.session_state["seq_persona"] = p["slug"]
                        st.switch_page(pg_view)
                    if c2.button("Review", key=f"review_{p['vertical']}_{p['slug']}", use_container_width=True):
                        st.session_state["review_vertical"] = p["vertical"]
                        st.session_state["review_persona"] = p["slug"]
                        st.switch_page(pg_review)
                    if c3.button("↺ Re-run", key=f"run_{p['vertical']}_{p['slug']}", use_container_width=True):
                        st.session_state["run_vertical"] = p["vertical"]
                        st.session_state["run_persona"] = p["slug"]
                        st.switch_page(pg_run)

    if without_seq:
        st.subheader("No Sequence Yet")
        cols = st.columns(3)
        for i, p in enumerate(without_seq):
            with cols[i % 3]:
                with st.container(border=True):
                    st.markdown(f"**{to_title(p['slug'])}**")
                    st.caption(p["vertical"])
                    if st.button("Run Sequence", key=f"newrun_{p['vertical']}_{p['slug']}", use_container_width=True):
                        st.session_state["run_vertical"] = p["vertical"]
                        st.session_state["run_persona"] = p["slug"]
                        st.switch_page(pg_run)


def page_view_sequence():
    """Read-only view of a generated sequence with per-touch scores and distilled learnings."""
    st.title("Sequence Viewer")
    st.caption("Read-only. To edit or approve, go to Review Sequence.")

    verticals = get_verticals()
    vertical_names = [v["vertical"] for v in verticals]

    col1, col2 = st.columns(2)
    with col1:
        default_v = st.session_state.get("seq_vertical", vertical_names[0] if vertical_names else "")
        vertical = st.selectbox("Vertical", vertical_names, index=vertical_names.index(default_v) if default_v in vertical_names else 0)
    with col2:
        personas = [p["slug"] for v in verticals if v["vertical"] == vertical for p in v["personas"] if p["has_sequence"]]
        default_p = st.session_state.get("seq_persona", personas[0] if personas else "")
        persona = st.selectbox("Persona", personas, index=personas.index(default_p) if default_p in personas else 0) if personas else None

    st.session_state.pop("seq_vertical", None)
    st.session_state.pop("seq_persona", None)

    if not persona:
        st.warning("No sequences found for this vertical. Run the optimizer first.")
        st.stop()

    touches = parse_sequence(vertical, persona)
    learnings = load_learnings(vertical, persona)

    if not touches:
        st.warning("Sequence file not found or empty.")
        st.stop()

    avg = round(sum(t["score"] for t in touches) / len(touches))
    c1, c2, c3 = st.columns([3, 1, 1])
    c1.markdown(f"### {to_title(persona)} — {to_title(vertical)}")
    c2.metric("Avg score", f"{avg}/60")
    if c3.button("↺ Re-run this"):
        st.session_state["run_vertical"] = vertical
        st.session_state["run_persona"] = persona
        st.switch_page(pg_run)

    st.markdown("**Touch scores**")
    score_cols = st.columns(len(touches))
    for i, t in enumerate(touches):
        color = "#4ade80" if t["score"] >= 45 else "#fbbf24" if t["score"] >= 40 else "#f87171"
        score_cols[i].markdown(
            f"<div style='text-align:center;padding:8px;background:#1e293b;border-radius:8px'>"
            f"<div style='font-size:11px;color:#94a3b8'>T{t['num']}</div>"
            f"<div style='font-size:18px;font-weight:700;color:{color}'>{t['score']}</div>"
            f"<div style='font-size:10px;color:#64748b'>{t['type'][:8]}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

    st.divider()
    left, right = st.columns([2, 1])

    with left:
        for t in touches:
            color = "#4ade80" if t["score"] >= 45 else "#fbbf24" if t["score"] >= 40 else "#f87171"
            with st.container(border=True):
                h1, h2 = st.columns([4, 1])
                h1.markdown(f"**Touch {t['num']}** &nbsp;·&nbsp; Day {t['day']} &nbsp;·&nbsp; {t['type']}", unsafe_allow_html=True)
                h2.markdown(f"<div style='text-align:right;font-size:14px;font-weight:700;color:{color}'>{t['score']}/60</div>", unsafe_allow_html=True)
                if t["subject"]:
                    st.markdown(f"**Subject:** `{t['subject']}`")
                st.code(t["body"] + "\n\n[SIGNATURE]", language=None)
                if st.button("📋 Copy", key=f"copy_{t['num']}"):
                    st.toast(f"Touch {t['num']} copied!")

    with right:
        if learnings:
            st.markdown("**Distilled Learnings**")
            sections = re.split(r"\n(?=##)", learnings.strip())
            for section in sections:
                lines = section.strip().split("\n")
                header = lines[0].lstrip("#").strip()
                body = "\n".join(lines[1:]).strip()
                if header and body:
                    with st.expander(header, expanded=False):
                        st.markdown(body)


def page_run_sequence():
    """Launch the optimizer for a vertical/persona and stream output to the UI."""
    st.title("Run Sequence")
    st.caption("Generates email variants per touch, scores them, and picks the winner. Streams live output as it runs.")

    verticals = get_verticals()
    vertical_names = [v["vertical"] for v in verticals]

    if not vertical_names:
        st.warning("No verticals found. Create one first via **New Vertical**.")
        st.stop()

    c1, c2 = st.columns(2)
    with c1:
        default_v = st.session_state.pop("run_vertical", vertical_names[0])
        vertical = st.selectbox("Vertical", vertical_names, index=vertical_names.index(default_v) if default_v in vertical_names else 0)
    with c2:
        all_personas = [p["slug"] for v in verticals if v["vertical"] == vertical for p in v["personas"]]
        default_p = st.session_state.pop("run_persona", all_personas[0] if all_personas else "")
        persona = st.selectbox("Persona", all_personas, index=all_personas.index(default_p) if default_p in all_personas else 0) if all_personas else None

    if vertical and persona:
        vdir = VERTICALS_DIR / vertical
        icp_files = list(vdir.glob("icp_*.md"))
        persona_file = vdir / "personas" / f"{persona}.md"

        if icp_files or persona_file.exists():
            st.divider()
            st.markdown("**Review before running**")
            for f in icp_files:
                with st.expander(f"ICP — {f.name}"):
                    st.markdown(f.read_text())
            if persona_file.exists():
                with st.expander(f"Persona — {persona_file.name}"):
                    st.markdown(persona_file.read_text())
            st.divider()

    c3, c4 = st.columns(2)
    iterations = c3.slider("Iterations per touch", 1, 10, 5)
    touches = c4.slider("Touches to generate", 1, 8, 8)

    api_calls = iterations * 2 * touches
    st.info(f"**{api_calls} API calls** · ~{round(iterations * touches * 0.5)} min · ~${api_calls * 0.004:.2f} estimated cost")

    if st.button("▶ Start Run", type="primary", disabled=not persona):
        st.divider()
        cmd = [
            "python3", "optimize.py",
            "--vertical", vertical,
            "--persona", persona,
            "--iterations", str(iterations),
            "--touches", str(touches),
        ]
        with st.spinner("Running optimizer..."):
            returncode = stream_process(cmd)

        if returncode == 0:
            st.success("Run complete!")
            if st.button("View Sequence →"):
                st.session_state["seq_vertical"] = vertical
                st.session_state["seq_persona"] = persona
                st.switch_page(pg_view)
        else:
            st.error(f"Process exited with code {returncode}")


PROMPT_TEMPLATE = """You are helping me set up automated cold outreach for a new industry vertical. I use a tool called Autoloop that targets specific buyer personas with multi-touch email sequences.

Industry I want to target: [FILL IN YOUR INDUSTRY]

Please research this industry (or ask me targeted questions if you need my specific perspective) and then output a complete ICP brief in this format:

---
vertical: [single word slug, e.g. "healthcare"]
sub_industries: [comma-separated list of sub-segments in scope]
company_size: [employee range, revenue range, and segment label]
segment: [smb / midmarket / enterprise]
tech_stack: [key tools this industry uses, especially CRM and communication tools]
buyer_personas: [comma-separated job titles of the people who buy or champion this tool]
top_use_cases: [comma-separated list of the top 4-6 use cases for business texting in this industry]
competitors: [other texting or messaging vendors this industry uses]
qualification_signals: [specific signals that indicate a good prospect — tech stack, team size, initiatives, etc.]
---

Feel free to ask me 2-3 clarifying questions before generating the output if you need my specific insights (e.g. which personas we prefer, deal size, existing customers in this space)."""


def page_new_vertical():
    """Wizard to derive a new vertical's ICP, persona, and pain angles from AI-generated output."""
    st.title("New Vertical")
    st.caption("Creates ICP, persona, and pain angles for a new industry target. No manual file editing required.")

    st.markdown("**Step 1 — Copy this prompt**")
    st.caption("Paste into Claude, ChatGPT, or any AI. It will research the vertical and return structured output.")
    st.code(PROMPT_TEMPLATE, language=None)

    st.markdown("**Step 2 — Paste AI output**")
    st.caption("Autoloop will extract: vertical slug, ICP, buyer persona, and 8 pain angles.")
    raw_input = st.text_area(
        "AI output",
        placeholder="Paste the AI's response here...",
        height=300,
        label_visibility="collapsed",
    )

    if st.button("Derive Vertical", type="primary", disabled=not raw_input.strip()):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as tmp:
            tmp.write(raw_input)
            tmp_path = tmp.name

        st.divider()
        cmd = ["python3", "optimize.py", "--derive", "--icp", tmp_path]
        with st.spinner("Deriving..."):
            returncode = stream_process(cmd)

        os.unlink(tmp_path)

        if returncode == 0:
            program = (BASE_DIR / "shared" / "program.md").read_text()
            last_persona_match = re.findall(r"SEARCH SPACE — (.+?) \(", program)
            derived_persona = last_persona_match[-1].lower().replace(" ", "_") if last_persona_match else "it_director"
            last_vertical_match = re.findall(r"SEARCH SPACE — .+? \((.+?)\)", program)
            derived_vertical = last_vertical_match[-1] if last_vertical_match else ""

            st.success(f"Vertical **{derived_vertical or 'derived'}** created successfully.")
            st.divider()
            st.subheader("Review Derived Files")

            if derived_vertical:
                vdir = VERTICALS_DIR / derived_vertical
                icp_files = list(vdir.glob("icp_*.md"))
                persona_files = list((vdir / "personas").glob("*.md")) if (vdir / "personas").exists() else []

                for f in icp_files:
                    with st.expander(f"ICP — {f.name}", expanded=True):
                        st.markdown(f.read_text())

                for f in persona_files:
                    with st.expander(f"Persona — {f.name}", expanded=True):
                        st.markdown(f.read_text())

                st.divider()
                if st.button(f"Run Sequence for {derived_vertical} / {derived_persona} →"):
                    st.session_state["run_vertical"] = derived_vertical
                    st.session_state["run_persona"] = derived_persona
                    st.switch_page(pg_run)
        else:
            st.error(f"Derive failed (exit code {returncode})")


def page_review_sequence():
    """Inline editor and AI chat for refining a sequence before approving and distilling learnings."""
    st.title("Review Sequence")
    st.caption("Edit touches inline or ask the AI assistant, then approve to lock and save.")
    st.caption("**Save draft** syncs edits to View Sequence (reversible). **Approve** locks the sequence, writes approved_{persona}.md, and distills your changes into learnings for the next run.")

    verticals = get_verticals()
    vnames = [v["vertical"] for v in verticals]
    if not vnames:
        st.warning("No verticals found.")
        st.stop()

    c1, c2 = st.columns(2)
    with c1:
        default_v = st.session_state.pop("review_vertical", vnames[0])
        vertical = st.selectbox("Vertical", vnames,
            index=vnames.index(default_v) if default_v in vnames else 0,
            key="rsel_vertical")
    with c2:
        personas = [p["slug"] for v in verticals if v["vertical"] == vertical
                    for p in v["personas"] if p["has_sequence"]]
        if not personas:
            st.warning("No sequences yet for this vertical. Run the optimizer first.")
            st.stop()
        default_p = st.session_state.pop("review_persona", personas[0])
        persona = st.selectbox("Persona", personas,
            index=personas.index(default_p) if default_p in personas else 0,
            key="rsel_persona")

    load_key = f"{vertical}__{persona}"
    if st.session_state.get("_rlk") != load_key:
        for k in list(st.session_state.keys()):
            if k.startswith(("rsubj_", "rbody_")):
                del st.session_state[k]
        orig = parse_sequence(vertical, persona)
        if not orig:
            st.warning("Sequence file not found or empty.")
            st.stop()
        st.session_state["_rorig"] = orig
        st.session_state["_rmsgs"] = []
        st.session_state["_rapproved"] = False
        st.session_state["_rlk"] = load_key
        for t in orig:
            st.session_state[f"rsubj_{t['num']}"] = t["subject"]
            st.session_state[f"rbody_{t['num']}"] = t["body"]

    # Apply any pending chat edits BEFORE widgets are instantiated
    for k, v in st.session_state.pop("_rpending", {}).items():
        st.session_state[k] = v

    original = st.session_state["_rorig"]

    # Re-initialize widget keys if Streamlit cleared them during page navigation
    for t in original:
        if f"rsubj_{t['num']}" not in st.session_state:
            saved = st.session_state.get("_rvals", {}).get(t["num"], {})
            st.session_state[f"rsubj_{t['num']}"] = saved.get("subject", t["subject"])
            st.session_state[f"rbody_{t['num']}"] = saved.get("body", t["body"])

    # Keep _rvals in sync so values survive navigation
    st.session_state["_rvals"] = {
        t["num"]: {
            "subject": st.session_state.get(f"rsubj_{t['num']}", t["subject"]),
            "body": st.session_state.get(f"rbody_{t['num']}", t["body"]),
        }
        for t in original
    }

    messages = st.session_state["_rmsgs"]
    is_approved = st.session_state["_rapproved"]

    def cur_subj(num):
        return st.session_state.get(f"rsubj_{num}", next(t["subject"] for t in original if t["num"] == num))
    def cur_body(num):
        return st.session_state.get(f"rbody_{num}", next(t["body"] for t in original if t["num"] == num))
    def current_touches():
        return [{**t, "subject": cur_subj(t["num"]), "body": cur_body(t["num"])} for t in original]

    edited_nums = {t["num"] for t in original
                   if cur_subj(t["num"]) != t["subject"] or cur_body(t["num"]) != t["body"]}

    # ── Header ────────────────────────────────────────────────────────────────
    avg = round(sum(t["score"] for t in original) / len(original))

    if is_approved:
        st.success(f"✓ Approved and saved as `approved_{persona}.md`")
        feedback_preview = st.session_state.get("_rfeedback_preview", "")
        if feedback_preview:
            with st.expander("Feedback captured for optimizer — click to view"):
                st.markdown(feedback_preview)
        elif feedback_preview == "":
            st.caption("No edits detected — approved as-is. No new learnings added.")

    hc = st.columns([3, 1, 1, 1, 1])
    hc[0].markdown(f"### {to_title(persona)} — {to_title(vertical)}")
    hc[1].metric("Avg score", f"{avg}/60")
    show_diff = hc[2].checkbox("Show diff", disabled=not edited_nums)
    if not is_approved and hc[3].button("Save draft", use_container_width=True, disabled=not edited_nums):
        save_draft(vertical, persona, current_touches())
        st.toast("Draft saved — synced to View Sequence", icon="💾")
    if is_approved:
        hc[4].button("Approved ✓", disabled=True, use_container_width=True)
    elif hc[4].button("Approve ✓", type="primary", use_container_width=True):
        cts = current_touches()
        save_draft(vertical, persona, cts)
        with st.spinner("Saving and distilling feedback for optimizer..."):
            feedback = serialize_approved(vertical, persona, cts, original)
        st.session_state["_rapproved"] = True
        st.session_state["_rfeedback_preview"] = feedback
        st.rerun()

    # ── Score strip ───────────────────────────────────────────────────────────
    scols = st.columns(len(original))
    for i, t in enumerate(original):
        color = "#4ade80" if t["score"] >= 45 else "#fbbf24" if t["score"] >= 40 else "#f87171"
        edited_html = "<div style='font-size:9px;color:#f59e0b'>edited</div>" if t["num"] in edited_nums else ""
        scols[i].markdown(
            f"<div style='text-align:center;padding:6px 4px;background:#1e293b;border-radius:8px'>"
            f"<div style='font-size:11px;color:#94a3b8'>T{t['num']}</div>"
            f"<div style='font-size:16px;font-weight:700;color:{color}'>{t['score']}</div>"
            f"{edited_html}</div>",
            unsafe_allow_html=True,
        )

    st.divider()

    # ── Main columns ──────────────────────────────────────────────────────────
    left, right = st.columns([2, 1])

    with left:
        for t in original:
            color = "#4ade80" if t["score"] >= 45 else "#fbbf24" if t["score"] >= 40 else "#f87171"
            subj_changed = cur_subj(t["num"]) != t["subject"]
            body_changed = cur_body(t["num"]) != t["body"]
            badge = " 🟡" if t["num"] in edited_nums and not is_approved else (" ✅" if t["num"] in edited_nums and is_approved else "")

            with st.container(border=True):
                hc1, hc2 = st.columns([5, 1])
                hc1.markdown(
                    f"**Touch {t['num']}** &nbsp;·&nbsp; Day {t['day']} &nbsp;·&nbsp; {t['type']}{badge}",
                    unsafe_allow_html=True,
                )
                hc2.markdown(
                    f"<div style='text-align:right;font-size:14px;font-weight:700;color:{color}'>{t['score']}/60</div>",
                    unsafe_allow_html=True,
                )

                # Subject
                if show_diff and subj_changed:
                    sc1, sc2 = st.columns(2)
                    sc1.caption("Original")
                    sc1.markdown(f"<span style='color:#f87171;text-decoration:line-through'>{t['subject']}</span>", unsafe_allow_html=True)
                    sc2.caption("Edited")
                    sc2.markdown(f"<span style='color:#4ade80'>{cur_subj(t['num'])}</span>", unsafe_allow_html=True)
                else:
                    st.text_input("Subject", key=f"rsubj_{t['num']}", disabled=is_approved)

                # Body
                if show_diff and body_changed:
                    bc1, bc2 = st.columns(2)
                    with bc1:
                        st.caption("Original")
                        st.code(t["body"], language=None)
                    with bc2:
                        st.caption("Edited")
                        st.code(cur_body(t["num"]), language=None)
                else:
                    st.text_area("Body", key=f"rbody_{t['num']}", height=180,
                                 disabled=is_approved, label_visibility="collapsed")

                st.caption("[SIGNATURE]")

    with right:
        st.markdown("**AI Assistant**")
        st.caption("Edit touches with natural language")

        msg_box = st.container(height=460)
        with msg_box:
            if not messages:
                st.markdown(
                    "<div style='text-align:center;color:#475569;padding:60px 0'>"
                    "<div style='font-size:24px;margin-bottom:8px'>✦</div>"
                    "<div>Ask me to refine any touch.</div>"
                    "<div style='font-size:12px;color:#334155;margin-top:6px'>"
                    "e.g. \"make touch 2 shorter\"</div></div>",
                    unsafe_allow_html=True,
                )
            for msg in messages:
                with st.chat_message(msg["role"]):
                    st.write(msg["content"])
                    if msg.get("edits_applied", 0) > 0:
                        n = msg["edits_applied"]
                        st.caption(f"✓ {n} field{'s' if n != 1 else ''} updated")

        if is_approved:
            st.info("Sequence is locked after approval.")
        else:
            chat_in = st.text_area(
                "Instruction", height=80, label_visibility="collapsed",
                placeholder='e.g. "make touch 3 shorter" or "change hook in email 1 to focus on labor costs"',
                key="rchat_input",
            )
            if st.button("Send →", use_container_width=True, disabled=not (chat_in and chat_in.strip())):
                user_text = chat_in.strip()
                messages.append({"role": "user", "content": user_text})

                with st.spinner("Thinking..."):
                    result = call_claude_editor(vertical, persona, current_touches(), messages)

                pending = {}
                edits_applied = 0
                for edit in result.get("edits", []):
                    num = edit.get("touchNum")
                    field = edit.get("field")
                    val = edit.get("newValue", "")
                    if field == "subject":
                        pending[f"rsubj_{num}"] = val
                        edits_applied += 1
                    elif field == "body":
                        pending[f"rbody_{num}"] = val
                        edits_applied += 1

                messages.append({
                    "role": "assistant",
                    "content": result.get("explanation", "Done."),
                    "edits_applied": edits_applied,
                })
                st.session_state["_rmsgs"] = messages
                pending["rchat_input"] = ""
                st.session_state["_rpending"] = pending
                if edits_applied:
                    # Build updated touches from pending to save immediately
                    updated = [{**t,
                        "subject": pending.get(f"rsubj_{t['num']}", cur_subj(t["num"])),
                        "body": pending.get(f"rbody_{t['num']}", cur_body(t["num"])),
                    } for t in original]
                    save_draft(vertical, persona, updated)
                st.rerun()


def page_master_instructions():
    """Edit shared knowledge files (program rules, product context, proof bank) that inform every sequence."""
    st.title("Knowledge Base")
    st.caption("Shared files that inform every sequence. Edits here apply to all future optimizer runs.")

    shared_files = {
        "program.md": "Program Rules",
        "product_knowledge.md": "Product Knowledge",
        "proof_bank.md": "Proof Bank",
    }

    tabs = st.tabs(list(shared_files.values()))
    for tab, (filename, label) in zip(tabs, shared_files.items()):
        with tab:
            path = SHARED_DIR / filename
            if not path.exists():
                st.info(f"{filename} not found.")
                continue
            if st.button("Edit", key=f"edit_{filename}"):
                st.session_state[f"editing_{filename}"] = not st.session_state.get(f"editing_{filename}", False)
            if st.session_state.get(f"editing_{filename}", False):
                new_content = st.text_area(label, value=path.read_text(), height=600, key=f"ta_{filename}", label_visibility="collapsed")
                if st.button("Save", key=f"save_{filename}", type="primary"):
                    path.write_text(new_content)
                    st.session_state[f"editing_{filename}"] = False
                    st.toast(f"{label} saved.", icon="✅")
                    st.rerun()
            else:
                with st.container(border=True):
                    st.markdown(path.read_text())


# ── Navigation ────────────────────────────────────────────────────────────────

pg_dashboard = st.Page(page_dashboard, title="Dashboard", icon="📊", default=True)
pg_view = st.Page(page_view_sequence, title="View Sequence", icon="📄")
pg_run = st.Page(page_run_sequence, title="Run Sequence", icon="▶")
pg_new = st.Page(page_new_vertical, title="New Vertical", icon="✨")
pg_review = st.Page(page_review_sequence, title="Review Sequence", icon="✅")
pg_instructions = st.Page(page_master_instructions, title="Master Instructions", icon="📋")

pg = st.navigation([pg_dashboard, pg_view, pg_run, pg_new, pg_review, pg_instructions])
pg.run()
