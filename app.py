"""MailMate app.  Run:  streamlit run app.py

Nothing is sent from here without an explicit approval. Real sending is off by default.
"""

import streamlit as st

st.set_page_config(page_title="MailMate", page_icon="✉️", layout="wide")

st.title("MailMate")
st.caption("Recruiter outreach: you approve every email, sending is throttled.")
st.info("Scaffold only. Upload, templates, review and sending arrive in later steps. "
        "Dry-run is the default; nothing can be sent yet.")
