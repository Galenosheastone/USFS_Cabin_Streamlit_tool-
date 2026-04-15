# Gmail App Password Setup

This tool uses Gmail SMTP with an **App Password** — a 16-character code that lets the
script send email on your behalf without exposing your real Gmail password.

## Steps

1. **Enable 2-Step Verification** on your Google account (required before App Passwords work):
   - Go to: https://myaccount.google.com/security
   - Click **2-Step Verification** and follow the setup if not already enabled.

2. **Generate an App Password**:
   - Go to: https://myaccount.google.com/apppasswords
   - Under "Select app", choose **Mail** (or type a custom name like "Cabin Checker").
   - Under "Select device", choose **Other** and name it something memorable.
   - Click **Generate**.

3. **Copy the password** — it looks like `abcd efgh ijkl mnop` (16 characters with spaces).

4. **Store it privately in Streamlit secrets** instead of committing it to `config.yaml`.

   For Streamlit Community Cloud, add:

   ```toml
   [email]
   smtp_server = "smtp.gmail.com"
   smtp_port = 587
   sender_email = "your-gmail@example.com"
   sender_password = "abcd efgh ijkl mnop"
   ```

   For local development, put the same values in `.streamlit/secrets.toml`.
   The spaces in the app password are fine — Gmail accepts the password with or
   without them.

## Notes

- This is **not** your Gmail login password. It only works for SMTP and can be revoked
  any time from your Google account without changing your real password.
- If you see `SMTPAuthenticationError`, double-check that 2-Step Verification is on and
  that you pasted the app password (not your regular password).
- If the App Passwords page says "App Passwords aren't available for your account",
  your account may be managed by a Google Workspace admin — contact them or use a
  personal Gmail account instead.
