# Send Email

Use this local app to send each company a personalized internship inquiry with your résumé attached.

## Open the app

Install Python 3.10 or newer if needed, then double-click **Start Send Email.cmd**. You can also run `python app.py` in this folder. The app opens at [127.0.0.1:8765](http://127.0.0.1:8765). No Python packages are required.

If you opened `web/index.html` first, follow the launch instructions on that page.

## Sign in with Gmail

Use **Sign out** in the Gmail account panel to disconnect Gmail from Send Email. Pause a running batch first. Your setup file stays saved for the next sign-in.

### One-time setup

1. Click **Set up Gmail** in Send Email. It opens Google's Personal mode setup in a new tab. Use the Gmail account you want to send from, select or create a project, and create a **Desktop app** client. Download the JSON file when Google shows your credentials. [Google’s Personal mode guide](https://support.google.com/cloud/answer/15544987#personal-mode)
2. Return to Send Email and select the file under **Import Google setup file**.
3. Click **Enable Gmail API** and enable it for the project you created. If the button on Google's page says **Manage**, the API is already enabled. Return to Send Email and click **Sign in with Google**.

### Connect and send

Click **Sign in with Google**, choose the Gmail address from the previous steps, and allow Send Email to send mail. Return to the app when you see **Gmail connected**. Use **Send test** to verify delivery and the attachment. You can prepare and check a batch in dry run before connecting Gmail.

Send Email stores the Google app registration in `%LOCALAPPDATA%\Send Email\google-client.json` and keeps your account tokens in memory. Sign in again after restarting the app. School and work accounts may require your organization to allow the Gmail app. If you need to revoke access, use [Google Account connections](https://myaccount.google.com/connections). [Google’s guide to managing third-party access](https://support.google.com/accounts/answer/13533235)

Existing SMTP users can continue to use `.env` with the SMTP values in `.env.example`. The Google connection handles sending once you import the Google Desktop app credentials.

## Send your internship emails

1. In Excel, put one company email per row under the `email` heading. Add `company` and `contact_name` if you have them. Save as **CSV UTF-8 (Comma delimited)**. The app includes a sample with example addresses. Review rows marked invalid or duplicate after upload.
2. Open **Draft from my details**, enter your information, and click **Create draft**. Edit the subject and message, then add your résumé PDF. If a contact has no name, `{{greeting}}` becomes **Hiring Team**. The app supports up to 50,000 contacts per file, a 5 MB CSV and a 10 MB PDF. Your provider may impose smaller limits.
3. In **Review and send**, look at a few recipient previews. Enter your own address and click **Send test**. Open your inbox to check the message and PDF.
4. Leave **Dry run** checked and click **Check batch** to validate it without sending. Set the delay to fit your provider’s sending limits. When ready, clear **Dry run**, click **Start sending**, and confirm the recipient count.
5. Watch **Sending progress**. Keep the app running while it sends. Use **Pause batch** and **Resume batch** as needed. A pause lets the current send finish. After restarting the app, click **Resume batch** to continue. Download the results before starting another batch because the app keeps one batch at a time.

**Accepted** means Gmail or your SMTP provider accepted the message for delivery. Temporary SMTP errors are retried up to twice. A Gmail sending limit pauses the batch. If the app loses the connection before confirming the result or closes during a send, the row appears as **Needs review**. Check with your provider before sending that email again.

The server listens only on this computer. It saves the current batch, contacts and PDF in `%LOCALAPPDATA%\Send Email\queue.db`. The browser keeps a draft and files in memory until you start a batch, so save a copy of your draft elsewhere if you need it after closing the page.

## Verify locally

Run `python -m unittest discover -s tests -v`. The tests use fake contacts, simulated Google API responses and a mock SMTP server on loopback.
