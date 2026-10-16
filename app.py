"""MailMate app.  Run:  streamlit run app.py

Nothing is sent from here without an explicit approval. Real sending is off by default.
The pages live in mailmate/ui/ (one module per page); this file only starts them.
"""

from mailmate.ui.main import main

main()
