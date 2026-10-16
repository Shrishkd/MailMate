"""7 Pipeline: companies I'm talking to after a reply, their stage, what's next and when, my notes."""

from datetime import datetime, time, timezone

import pandas as pd
import streamlit as st

from mailmate import pipeline, sending
from mailmate.config import ConfigError, test_address
from mailmate.pipeline import LABELS, OPEN, STAGES
from mailmate.ui import common as ui


def _when(row) -> str:
    if not row["next_at"]:
        return ""
    return f"{datetime.fromisoformat(row['next_at']).astimezone(sending.IST):%a %d %b, %H:%M} IST"


def page_pipeline():
    conn = ui.conn()
    ui.header("7 · Pipeline", "Companies you're talking to after a reply: the stage, what happens next and when, and "
              "your notes. A company comes in by itself when its recruiter asks for an interview or asks a question; "
              "add others by hand.")
    try:
        test_to = test_address()
    except ConfigError as exc:
        st.error(ui.plain(exc))
        return
    if added := pipeline.add_from_replies(conn, test_to):
        st.toast(f"Added to the pipeline: {', '.join(added)}", icon="💬")
    now = datetime.now(timezone.utc)
    rows = pipeline.entries(conn)
    if not rows:
        st.info("Nothing here yet. When a recruiter asks for an interview or asks a question, their company appears "
                "here after the next sync. You can also add a company by hand below.")
    for r in pipeline.upcoming(conn, now):
        st.info(f"📅 **{_when(r)}** · **{r['company']}** · {r['next_step'] or LABELS[r['stage']]}")
    for r in pipeline.stale(conn, now):
        st.warning(f"**{r['company']}**: *{r['next_step'] or LABELS[r['stage']]}* was on {_when(r)}. How did it go? "
                   "Update the stage below.")

    open_rows = [r for r in rows if r["stage"] in OPEN]
    if open_rows:
        for col, stage in zip(st.columns(len(OPEN)), OPEN):
            here = [r for r in open_rows if r["stage"] == stage]
            col.markdown(f"**{LABELS[stage]}** ({len(here)})")
            for r in here:
                with col.container(border=True):
                    st.markdown(f"**{r['company']}**  \n{r['name'] or r['email'] or ''}"
                                + (f"  \n📅 {_when(r)}" if r["next_at"] else "")
                                + (f"  \n*{r['next_step']}*" if r["next_step"] else ""))
    if rows:
        _editor(rows)
    _add_by_hand()
    closed = [r for r in rows if r["stage"] not in OPEN]
    if closed:
        with st.expander(f"Closed ({len(closed)})"):
            st.dataframe(pd.DataFrame([{"Company": r["company"], "Stage": LABELS[r["stage"]],
                                        "Got as far as": LABELS[OPEN[r["reached"] - 1]], "Notes": r["notes"],
                                        "Updated": ui.ist(r["updated_at"])} for r in closed]),
                         hide_index=True, width="stretch")


def _editor(rows) -> None:
    st.subheader("Update a company")
    by_key = {r["company_key"]: r for r in rows}
    if st.session_state.get("pipeline_pick") not in by_key:
        st.session_state.pipeline_pick = next(iter(by_key))
    key = st.selectbox("Company", list(by_key), key="pipeline_pick",
                       format_func=lambda k: f"{by_key[k]['company']} · {LABELS[by_key[k]['stage']]}")
    row = by_key[key]
    when = datetime.fromisoformat(row["next_at"]).astimezone(sending.IST) if row["next_at"] else None
    with st.form(f"pipeline_{key}"):
        stage = st.selectbox("Stage", STAGES, index=STAGES.index(row["stage"]), format_func=LABELS.get)
        next_step = st.text_input("Next step", row["next_step"], placeholder="e.g. Technical round with the ML lead")
        c1, c2 = st.columns(2)
        day = c1.date_input("When (date)", when.date() if when else None, format="DD/MM/YYYY")
        hour = c2.time_input("Time (IST)", when.time() if when else time(15, 0), step=900)
        notes = st.text_area("Notes", row["notes"], height=120,
                             placeholder="Who you spoke to, what they asked, what to prepare...")
        if st.form_submit_button("Save", type="primary"):
            next_at = datetime.combine(day, hour, sending.IST).astimezone(timezone.utc) if day else None
            if ui.did(lambda: pipeline.update(ui.conn(), key, stage=stage, next_step=next_step, next_at=next_at,
                                              notes=notes), "save it"):
                ui.flash(f"{row['company']}: {LABELS[stage]}" + (f", next on {day:%d %b}" if next_at else "") + ".",
                         "toast")
                st.rerun()
    st.caption("Closing a company (rejected or withdrawn) clears its date; how far it got still counts on the Dashboard.")


def _add_by_hand() -> None:
    conn = ui.conn()
    with st.expander("Add a company by hand"):
        known = [r[0] for r in conn.execute("SELECT DISTINCT company FROM contacts ORDER BY company COLLATE NOCASE")]
        with st.form("pipeline_add", clear_on_submit=True):
            picked = st.selectbox("A company from your contacts", [""] + known)
            typed = st.text_input("...or another company", placeholder="e.g. a call that came through a friend")
            note = st.text_input("How it started (optional)")
            if st.form_submit_button("Add to the pipeline"):
                company = typed.strip() or picked
                if not company:
                    st.error("Pick or type a company first.")
                    return
                contact = conn.execute("SELECT id FROM contacts WHERE company = ? ORDER BY id LIMIT 1",
                                       (company,)).fetchone()
                try:
                    added = pipeline.add(conn, company, contact[0] if contact else None, note=note)
                except ui.EXPECTED_ERRORS as exc:
                    st.error(f"**Not added.** {ui.plain(exc)}")
                    return
                if not added:
                    st.warning(f"{company} is already in the pipeline.")
                    return
                ui.flash(f"{company} added as {LABELS['talking']}.", "toast")
                st.rerun()
