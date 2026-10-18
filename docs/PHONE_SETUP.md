# Phone-Side WhatsApp Capture Setup

These instructions apply to `WHATSAPP_SOURCE=email` (the default).
For direct ingestion without phone forwarding, see [EVOLUTION_SETUP.md](EVOLUTION_SETUP.md).

This captures messages (and documents) from the school WhatsApp group
without any unofficial WhatsApp client or automation library - it works
entirely through Android's own notification and file systems, and
delivers everything as plain email so `fetch_whatsapp.py` only needs
the Gmail App Password already set up for sending the brief (no Google
Cloud project, service account, or Drive API).

## 1. Install MacroDroid

From the Play Store. Free tier is enough for these two rules.

## 2. Pick a capture address

Use a Gmail "+" alias of the account already in `SMTP_USERNAME` -
e.g. if that's `you@gmail.com`, use `you+whatsapp@gmail.com`. Gmail
delivers "+" alias mail to the same inbox untouched, so no new account,
filter, or label is required - `fetch_whatsapp.py` finds these emails by
searching IMAP for mail addressed `TO` that alias. Put it in
`WHATSAPP_EMAIL_TO` in `.env`.

Use `[WhatsApp School]` as the subject prefix for capture emails. The service
requires this marker as well as the recipient address to distinguish captures
from other email. Set `WHATSAPP_SUBJECT_PREFIX` only if you want a different
marker, and use exactly the same value in both phone rules. Subjects must start
with the marker followed by a space, or consist of the marker alone. Existing
emails without the marker are ignored; update the phone rules before collecting.

## 3. Create the text-message capture rule

- Trigger: **Notification Received** -> app: WhatsApp -> title/text
  filter: matches the school group's exact chat name.
- Action: **Send Email** (MacroDroid's native email action, not "open
  Gmail app"):
  - To: the address from step 2
  - Subject: `[WhatsApp School] %notification_title%` (marker plus sender's name)
  - Body: `%notification_text%` (the message text)

  Unlike writing a JSON line to a file, a plain email body has no
  escaping/quoting concerns - any message text is safe verbatim, so
  there's no risk of a malformed line silently dropping a message.

## 4. Create the document capture rule

WhatsApp auto-saves every incoming document (from *any* chat, not just
the school group - see caveat below) to a local folder on the phone,
independent of any automation: `Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Documents/`
(older WhatsApp versions used `WhatsApp/Media/WhatsApp Documents/`
instead - check which exists on your phone).

- Trigger: **File Added/Modified** -> folder: the WhatsApp Documents
  path above.
- Action: **Send Email**, with the trigger's file attached:
  - To: the address from step 2
  - Subject: `[WhatsApp School] Document`
  - Attachment: the newly added file (MacroDroid's file-added trigger
    exposes the file path as a local variable to attach)

  Only mark documents verified to be from the school group. The marker does
  not identify the originating chat: leave this folder rule disabled unless
  it is school-only, or manually email school PDFs with the marked subject.

  **Caveat - not scoped to the school group:** WhatsApp doesn't
  separate this folder by chat, so this rule will email *every*
  document sent in *any* WhatsApp chat on the phone, not just the
  school group. `fetch_whatsapp.py` only keeps PDF attachments (images
  and other file types are ignored), which narrows this somewhat, but
  a PDF from an unrelated chat would still be picked up and passed to
  the brief. If that's a problem in practice, this needs a different
  capture mechanism (e.g. an advanced MacroDroid config that reads the
  WhatsApp document notification's attachment URI directly, which can
  filter by chat name the same way the text rule does, but is less
  consistently supported across Android/WhatsApp versions).

## 5. Enable IMAP on the Gmail account

Gmail Settings -> **See all settings** -> **Forwarding and POP/IMAP**
tab -> enable IMAP. This is required for `fetch_whatsapp.py` to read
the captured emails; the same Gmail App Password used for
`SMTP_APP_PASSWORD` works for IMAP too, no separate credential needed.

## Verifying it works

Send a test message in the school WhatsApp group, wait a minute, then
check `WHATSAPP_EMAIL_TO`'s inbox for the captured email (or run
`python fetch_whatsapp.py` once steps 2-5 are done). Repeat with a test
PDF sent in the group to verify the document rule and attachment
saving (into `data/pdfs/`, prefixed `whatsapp_`) both work.
