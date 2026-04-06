import streamlit as st
import os
import re
import subprocess
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).parent
VERTICALS_DIR = BASE_DIR / "verticals"
SHARED_DIR = BASE_DIR / "shared"

st.set_page_config(
    page_title="Autoloop",
    page_icon="✉",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def to_title(slug: str) -> str:
    return slug.replace("_", " ").title()

def get_verticals() -> list[dict]:
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

def stream_process(cmd: list[str]):
    output_box = st.empty()
    lines: list[str] = []
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=str(BASE_DIR)
    )
    for raw in iter(proc.stdout.readline, ""):  # type: ignore[union-attr]
        line = raw.rstrip()
        lines.append(line)
        output_box.code("\n".join(lines[-80:]), language=None)
    proc.wait()
    output_box.code("\n".join(lines), language=None)
    return proc.returncode

# ── Pages ─────────────────────────────────────────────────────────────────────

def page_dashboard():
    st.title("Dashboard")

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
                    c1, c2 = st.columns(2)
                    if c1.button("View", key=f"view_{p['vertical']}_{p['slug']}", use_container_width=True):
                        st.session_state["seq_vertical"] = p["vertical"]
                        st.session_state["seq_persona"] = p["slug"]
                        st.switch_page(pg_view)
                    if c2.button("↺ Re-run", key=f"run_{p['vertical']}_{p['slug']}", use_container_width=True):
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
    st.title("Sequence Viewer")

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
    st.title("Run Sequence")

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
    st.caption(f"{api_calls} API calls · ~{round(iterations * touches * 0.5)} min estimated")

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


def page_new_vertical():
    st.title("New Vertical")
    st.caption("Fill out the ICP and Autoloop will derive a persona, sequence arc, and pain angles.")

    with st.form("icp_form"):
        vertical = st.text_input("Vertical *", placeholder="healthcare", help="Single word slug, e.g. healthcare")
        sub_industries = st.text_input("Sub-industries", placeholder="hospitals, outpatient clinics, medical groups")
        company_size = st.text_input("Company size", placeholder="500-5000 employees, $100M-$1B revenue")
        segment = st.text_input("Segment", placeholder="midmarket")
        tech_stack = st.text_input("Tech stack", placeholder="Epic EHR, HubSpot or Salesforce CRM", help="Key tools used")
        buyer_personas = st.text_input("Buyer personas", placeholder="IT Director, Operations Manager")
        top_use_cases = st.text_input("Top use cases", placeholder="patient appointment reminders, internal staff coordination")
        competitors = st.text_input("Competitors", placeholder="Weave, Relatient, Twilio")
        qualification_signals = st.text_input("Qualification signals", placeholder="HubSpot or Salesforce in stack, 200+ staff")

        submitted = st.form_submit_button("Derive Vertical", type="primary")

    if submitted and vertical:
        icp_content = "\n".join([
            "# ICP Definition",
            f"vertical: {vertical}",
            f"sub_industries: {sub_industries}",
            f"company_size: {company_size}",
            f"segment: {segment}",
            f"tech_stack: {tech_stack}",
            f"buyer_personas: {buyer_personas}",
            f"top_use_cases: {top_use_cases}",
            f"competitors: {competitors}",
            f"qualification_signals: {qualification_signals}",
        ])

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as tmp:
            tmp.write(icp_content)
            tmp_path = tmp.name

        st.divider()
        cmd = ["python3", "optimize.py", "--derive", "--icp", tmp_path]
        with st.spinner("Deriving..."):
            returncode = stream_process(cmd)

        os.unlink(tmp_path)

        if returncode == 0:
            st.success(f"Vertical **{vertical}** derived successfully.")
            program = (BASE_DIR / "shared" / "program.md").read_text()
            last_persona_match = re.findall(r"SEARCH SPACE — (.+?) \(", program)
            derived_persona = last_persona_match[-1].lower().replace(" ", "_") if last_persona_match else "it_director"

            st.divider()
            st.subheader("Review Derived Files")

            vdir = VERTICALS_DIR / vertical
            icp_files = list(vdir.glob("icp_*.md"))
            persona_files = list((vdir / "personas").glob("*.md")) if (vdir / "personas").exists() else []

            for f in icp_files:
                with st.expander(f"ICP — {f.name}", expanded=True):
                    st.markdown(f.read_text())

            for f in persona_files:
                with st.expander(f"Persona — {f.name}", expanded=True):
                    st.markdown(f.read_text())

            st.divider()
            if st.button(f"Run Sequence for {vertical} / {derived_persona} →"):
                st.session_state["run_vertical"] = vertical
                st.session_state["run_persona"] = derived_persona
                st.switch_page(pg_run)
        else:
            st.error(f"Derive failed (exit code {returncode})")


def page_master_instructions():
    st.title("Master Instructions")

    shared_files = {
        "program.md": "Program Rules",
        "product_knowledge.md": "Product Knowledge",
        "proof_bank.md": "Proof Bank",
    }

    for filename, label in shared_files.items():
        path = SHARED_DIR / filename
        if not path.exists():
            continue
        st.subheader(label)
        col1, col2 = st.columns([6, 1])
        with col2:
            if st.button("Edit", key=f"edit_{filename}"):
                st.session_state[f"editing_{filename}"] = not st.session_state.get(f"editing_{filename}", False)
        if st.session_state.get(f"editing_{filename}", False):
            new_content = st.text_area(label, value=path.read_text(), height=400, key=f"ta_{filename}", label_visibility="collapsed")
            if st.button("Save", key=f"save_{filename}", type="primary"):
                path.write_text(new_content)
                st.session_state[f"editing_{filename}"] = False
                st.success(f"{label} saved.")
                st.rerun()
        else:
            with st.container(border=True):
                st.markdown(path.read_text())
        st.divider()


# ── Navigation ────────────────────────────────────────────────────────────────

pg_dashboard = st.Page(page_dashboard, title="Dashboard", icon="📊", default=True)
pg_view = st.Page(page_view_sequence, title="View Sequence", icon="📄")
pg_run = st.Page(page_run_sequence, title="Run Sequence", icon="▶")
pg_new = st.Page(page_new_vertical, title="New Vertical", icon="✨")
pg_instructions = st.Page(page_master_instructions, title="Master Instructions", icon="📋")

pg = st.navigation([pg_dashboard, pg_view, pg_run, pg_new, pg_instructions])
pg.run()
