---
name: daily-school-brief
description: Run the daily school brief pipeline and save validated JSON, reusing unchanged cached briefs. Use when asked to run or generate the daily school brief, or on an existing scheduled trigger.
---

# Daily School Brief

This is a compatibility launcher for existing agent-based triggers. The Python
pipeline performs collection, PDF reading, Azure generation when needed, and
validation. Claude must not categorize or rewrite content.
The application can also run directly without this skill or Claude Code.

1. Change to the project root containing `daily_brief.py`. Use the configured
   Python environment; activate `.venv` if that is the installed environment.
2. For a persistent local installation, run exactly once:

   ```bash
   python daily_brief.py
   ```

   Keep `data/` on persistent storage; school data is not published to Git.
   Stop the REST service before using the CLI against the same storage.

   To reuse stored sources without fetching, add `--from-input`.
3. Report the result from `output/daily_brief_content.json`. A nonzero exit
   means the run failed and the previous saved brief was preserved; do not
   claim it is newly generated.

Do not read or summarize messages/PDFs yourself, write brief content manually,
or send email. The pipeline already handles generation and cache reuse.
Do not print `.env` or API keys.