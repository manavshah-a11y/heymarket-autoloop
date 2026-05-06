# Autoloop

Cold email sequence optimizer for Heymarket. Uses Claude AI to generate, score, and iteratively improve multi-touch outreach sequences for specific industry verticals and buyer personas.

---

## How it works

1. **Define a vertical** — describe an industry and buyer persona (e.g. healthcare / IT Director)
2. **Run the optimizer** — Claude generates 3 variants per email touch, scores them, picks the winner, and repeats across up to 8 touches
3. **Review and edit** — edit sequences inline or chat with the AI to refine them
4. **Approve** — lock the sequence and the learnings feed back into the next run

```mermaid
flowchart TD
    A([Start]) --> B{Got a vertical?}

    B -- No --> C[New Vertical]
    C --> C1[Copy AI prompt from app]
    C1 --> C2[Paste into Claude / ChatGPT]
    C2 --> C3[Paste output back into app]
    C3 --> C4[Autoloop derives ICP + Persona + Pain angles]
    C4 --> D

    B -- Yes --> D[Run Sequence]
    D --> D1[Select vertical + persona\nSet iterations & touches]
    D1 --> D2[optimize.py runs]

    subgraph optimizer [Optimizer loop — per touch]
        D2 --> D3[Generate 3 email variants]
        D3 --> D4[Validate each variant\nword count, CTA rules]
        D4 --> D5[Score all 3 variants]
        D5 --> D6[Pick winner]
        D6 --> D7{More iterations?}
        D7 -- Yes --> D3
        D7 -- No --> D8[Lock touch\nDistill learnings]
    end

    D8 --> D9{More touches?}
    D9 -- Yes → next touch --> D3
    D9 -- No --> D10[Write sequence file]

    D10 --> E[Review Sequence]
    E --> E1[Edit inline or via AI chat]
    E1 --> E2[Approve]
    E2 --> E3[Save final sequence\nLearnings feed next run]
    E3 --> F([Dashboard])
```

---

## Setup

**Requirements:** Python 3.11+

```bash
pip install -r requirements.txt
```

Create a `.env` file in the project root (copy from `.env.example`):

```
ANTHROPIC_API_KEY=your-key-here
```

Run the app:

```bash
streamlit run streamlit_app.py
```

Opens at `http://localhost:8501`

---

## Pages

### Dashboard
Overview of all verticals and personas. Shows average sequence scores and which personas have sequences generated.

### New Vertical
Two ways to add a new vertical:

**Option A (recommended):** Use the built-in prompt template — copy it, paste into Claude or ChatGPT, then paste the AI's output back in. Autoloop will auto-derive the ICP, persona file, and pain angles.

**Option B:** Manually create files in `verticals/<name>/` following the existing structure.

### Run Sequence
Pick a vertical and persona, set iterations (how many variants to test per touch) and number of touches (up to 8), then run. Streams live output as it runs. Each run costs `iterations × touches × 2` API calls.

### View Sequence
Read-only view of a generated sequence with scores per touch.

### Review Sequence
Edit sequences inline or use the AI chat panel to request changes. When satisfied, approve — this saves the final version and distills learnings for the next run.

### Master Instructions
Edit the three shared knowledge files that inform every sequence:
- **Program** (`shared/program.md`) — sequence rules and pain angles per persona
- **Product Knowledge** (`shared/product_knowledge.md`) — Heymarket features, integrations, proof points
- **Proof Bank** (`shared/proof_bank.md`) — verified customer stats and quotes (cited verbatim in emails)

---

## File structure

```
streamlit_app.py          # main app
optimize.py               # sequence generation engine (called by the app)
scrape.py                 # scrapes heymarket.com to refresh product_knowledge.md
icp_template.md           # template for defining a new vertical

shared/
  program.md              # sequence rules + pain angles for each persona
  product_knowledge.md    # Heymarket product context
  proof_bank.md           # verified proof points (cite verbatim)

verticals/
  <vertical>/
    icp_<vertical>.md     # ideal customer profile
    personas/             # one .md file per buyer persona
    sequences/            # generated sequences (sequence_<persona>.md)
    learnings/            # distilled learnings from past runs
    logs/                 # full iteration logs
```

---

## Refreshing product knowledge

`scrape.py` scrapes heymarket.com and writes to `shared/product_knowledge.md`. Run it if the product knowledge feels stale:

```bash
python scrape.py
```

---

## Deployment

Single service, single port. No database, no Node, no separate frontend.

**Environment variable required:**
```
ANTHROPIC_API_KEY=<key>
```

**Start command** (handled automatically via `Procfile`):
```
streamlit run streamlit_app.py --server.address=0.0.0.0 --server.port=${PORT:-8501} --server.headless=true
```

**Important:** the app writes data to `verticals/` and `shared/` on disk. The deployment environment must have a **persistent volume** mounted at the project root — otherwise data is lost on restart.
