"""7 Dashboard: funnel, rates, results per template version."""

import altair as alt
import pandas as pd
import streamlit as st

from mailmate import pipeline, stats
from mailmate.config import ConfigError, test_address
from mailmate.ui import common as ui


# Chart colors (dataviz reference palette): one blue for single-series bars, text in text tokens.
VIZ = {"light": {"bar": "#2a78d6", "text": "#0b0b0b", "muted": "#52514e"},
       "dark": {"bar": "#3987e5", "text": "#ffffff", "muted": "#c3c2b7"}}


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _funnel_chart(f: stats.Funnel, colors: dict[str, str]) -> alt.LayerChart:
    stages = [("Uploaded", f.uploaded), ("Approved", f.approved), ("Sent", f.sent),
              ("Replied", f.replied), ("Positive", f.positive)]
    data = pd.DataFrame([{"Stage": s, "Contacts": n,
                          "Share": f"{n / f.uploaded:.0%} of uploaded" if f.uploaded else "",
                          "Label": f"{n}  ({n / f.uploaded:.0%})" if f.uploaded else str(n)} for s, n in stages])
    order = [s for s, _ in stages]
    base = alt.Chart(data).encode(
        y=alt.Y("Stage:N", sort=order, title=None, axis=alt.Axis(labelColor=colors["muted"], ticks=False, domain=False)),
        # room right of the longest bar, so its label is never cut off
        x=alt.X("Contacts:Q", title=None, axis=None, scale=alt.Scale(domain=[0, max(f.uploaded, 1) * 1.25], nice=False)),
        tooltip=[alt.Tooltip("Stage:N"), alt.Tooltip("Contacts:Q"), alt.Tooltip("Share:N", title="Share")])
    bars = base.mark_bar(color=colors["bar"], cornerRadiusEnd=4, size=18)
    labels = base.mark_text(align="left", dx=6, color=colors["text"]).encode(text="Label:N")
    return (bars + labels).properties(height=200)


def _sends_chart(days: list, colors: dict[str, str]) -> alt.Chart:
    data = pd.DataFrame([{"Day": d.strftime("%d %b"), "Emails": n, "Date": d.strftime("%a %d %b")} for d, n in days])
    return alt.Chart(data).mark_bar(color=colors["bar"], cornerRadiusTopLeft=4, cornerRadiusTopRight=4, size=14).encode(
        x=alt.X("Day:N", sort=None, title=None, axis=alt.Axis(labelColor=colors["muted"], labelAngle=0, ticks=False)),
        y=alt.Y("Emails:Q", title=None, axis=alt.Axis(labelColor=colors["muted"], tickMinStep=1, grid=True,
                                                       gridOpacity=0.25, domain=False, ticks=False)),
        tooltip=[alt.Tooltip("Date:N", title="Day"), alt.Tooltip("Emails:Q", title="Emails sent")],
    ).properties(height=180)


def page_dashboard():
    conn = ui.conn()
    ui.header("8 · Dashboard", "Real sends only, per contact: your own test address and test emails never count. "
              "'Sent' means Gmail confirmed it (6 Replies → Sync); 'positive' means an interview request. Rates are out "
              "of contacts sent to.")
    ui.sync_reminder()
    try:
        test_to = test_address()
    except ConfigError as exc:
        st.error(ui.plain(exc))
        return
    colors = VIZ["light" if getattr(st.context.theme, "type", "dark") == "light" else "dark"]
    f = stats.funnel(conn, test_to)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Contacts", f.uploaded)
    c2.metric("Sent", f.sent, help=f"{f.queued} more queued in W2, not yet confirmed by a sync" if f.queued else None)
    c3.metric("Reply rate", _pct(f.rate(f.replied)), help=f"{f.replied} of {f.sent} replied (auto-replies don't count)")
    c4.metric("Interview requests", f.positive, help=f"{_pct(f.rate(f.positive))} of sent")
    c5.metric("Bounce rate", _pct(f.rate(f.bounced)), help=f"{f.bounced} of {f.sent} bounced")

    st.subheader("Funnel")
    st.altair_chart(_funnel_chart(f, colors), width="stretch")
    st.caption("Percentages are of contacts uploaded; the rates above are of contacts sent to.")
    if f.queued:
        st.caption(f"{f.queued} email(s) are queued in W2 and count as sent after the next reply sync.")

    st.subheader("After the reply (companies)")
    reached = pipeline.reached_counts(ui.conn())
    for col, (stage, n) in zip(st.columns(len(reached)), reached.items()):
        col.metric(pipeline.LABELS[stage], n)
    st.caption("Companies that got at least this far (from 7 Pipeline); a later rejection doesn't undo it.")

    left, right = st.columns([3, 2], gap="large")
    with left:
        st.subheader("By template version")
        rows = stats.per_template(conn, test_to)
        if rows:
            st.dataframe(pd.DataFrame([{"Template": r.template, "Sent": r.sent, "Replied": r.replied,
                                        "Reply rate": _pct(r.reply_rate), "Interview requests": r.positive,
                                        "Bounced": r.bounced, "Replied after follow-up": r.replied_after_follow_up}
                                       for r in rows]), hide_index=True, width="stretch")
            st.caption("A reply counts for the version of the first email. 'After follow-up' = the first real "
                       "reply came after the follow-up went out.")
        else:
            st.caption("Nothing sent yet.")
    with right:
        st.subheader("Replies by category")
        categories = stats.reply_categories(conn, test_to)
        if categories:
            st.dataframe(pd.DataFrame([{"Category": ui.CATEGORY_LABELS.get(k, k), "Replies": v}
                                       for k, v in categories.items()]), hide_index=True, width="stretch")
        else:
            st.caption("No replies yet.")

    st.subheader("Emails sent per day (last 14 days, India time)")
    days = stats.sends_per_day(conn, test_to)
    st.altair_chart(_sends_chart(days, colors), width="stretch")
    with st.expander("As a table"):
        st.dataframe(pd.DataFrame([{"Day": d.isoformat(), "Emails sent": n} for d, n in days]),
                     hide_index=True, width="stretch")

    st.subheader("Recent replies")
    recent = stats.recent_replies(conn, test_to)
    if recent:
        st.dataframe(pd.DataFrame([{"Company": r["company"], "From": r["email"], "When": r["date"][:16].replace("T", " "),
                                    "Category": ui.CATEGORY_LABELS.get(r["category"], r["category"]),
                                    "Snippet": r["snippet"]} for r in recent]), hide_index=True, width="stretch")
    else:
        st.caption("No replies yet.")
