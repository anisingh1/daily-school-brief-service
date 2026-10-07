---
name: daily-school-brief
description: Run the Azure OpenAI daily school brief pipeline and deliver its HTML email. Use when asked to run or generate the daily school brief, or on an existing scheduled evening trigger.
---

# Daily School Brief

This is a compatibility launcher for existing agent-based triggers. The Python
pipeline performs collection, PDF reading, Azure GPT generation, validation,
HTML rendering, and delivery. Claude must not categorize or rewrite content.
The application can also run directly without this skill or Claude Code.

1. Change to the project root containing `daily_brief.py`. Use the configured
   Python environment; activate `.venv` if that is the installed environment.
2. For a persistent local installation, run exactly once:

   ```bash
   python daily_brief.py
   ```

   For a scheduled environment that clones the repository fresh each run,
   preserve the archive with:

   ```bash
   python daily_brief.py --commit-archive
   ```

   If the user explicitly requests a preview without email, add `--preview`.
3. Report the command result. A nonzero exit can mean a warning-only email was
   delivered after generation failed; do not claim a normal brief succeeded.

Do not read or summarize messages/PDFs yourself, write brief content manually,
run `commit_archive.py` separately, or call `send_email.py` again. The pipeline
already handles those steps. Do not print `.env` or API keys.