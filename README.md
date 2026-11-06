# Kumati Manager (Python + Firebase Firestore)

## One-time Firebase setup
1. Go to https://console.firebase.google.com and click "Add project" (any name, Analytics not needed).
2. Left menu: Build > Firestore Database > Create database > choose a location near you > Production mode.
3. Click the gear icon > Project settings > Service accounts > Generate new private key.
4. Save the downloaded file as `serviceAccountKey.json` in the same folder as `app.py`.
   Keep this file private. It gives full access to your database.
5. In Firebase Console, open Build > Authentication > Sign-in method and enable
   Email/Password.
6. In Project settings > General, copy the Web API key.

## Run
    python -m pip install -r requirements.txt
    python app.py
Open http://localhost:5000

## User management

For local development, copy `.env.example` to `.env` and enter the values in
`.env` in the same folder as `app.py`:

    FIREBASE_WEB_API_KEY=your Firebase Web API key
    ADMIN_EMAIL=admin@example.com
    ADMIN_PASSWORD=use-a-strong-password
    SECRET_KEY=use-a-long-random-value

The app loads `.env` automatically at startup. `.env` is ignored by Git and
must not be included when sharing the project. Share `.env.example` instead,
then provide the real values through the deployment platform's secret
environment-variable settings.

For Firebase Admin SDK access locally, download the private key from Firebase
Console > Project settings > Service accounts > Generate new private key and
save it as `serviceAccountKey.json` beside `app.py`. This file is also ignored
and must not be shared. For a hosted deployment, store its complete JSON in
the `FIREBASE_KEY_JSON` secret instead of uploading the file.

The first admin account is created automatically if it does not exist. Admins can
create users and assign `admin`, `manager`, or `viewer` roles from the Users
button. Managers can add, edit, and delete records; viewers have read-only access.
User changes are stored in Firebase Authentication and the `users` collection.
Record and user changes are recorded in `audit_logs`.

If `FIREBASE_WEB_API_KEY` is not set, the existing `APP_PASSWORD` local
single-password fallback remains available. Do not use that fallback for a
public deployment.

Collections are created automatically: entries, persons, settings, users,
and audit_logs.
Changes made directly in the Firebase console show up in the app after you restart it.

Kumati schemes are isolated. Admins and managers can create named schemes and
switch between them; managers can access all schemes. Viewers can be assigned
to one or more schemes by an admin and only see the schemes assigned to them.
Entries, people, balances, expenses, reports, receipts, backups, and unit
prices are scoped to the selected scheme.

Admins can open `Manage kumatis` to archive an ended scheme without losing its
data, or permanently delete it. Permanent deletion requires typing `delete`
and removes the scheme's entries, people, settings, and reports permanently.

The monthly report supports PDF and Excel downloads. The Excel workbook contains
separate `Entries` and `Summary` sheets. On supported browsers, the app can also
be installed as a progressive web app from the browser's install option.

Admins can download a JSON backup from the app header. Store that file securely;
it contains application data but not Firebase credentials. Admins can also reset
user passwords from the Users panel. Individual entries have PDF receipt links,
and saved people with phone numbers have WhatsApp click-to-chat reminder links.
Admins can reset the password of any other user, including another admin,
enable disabled users, or permanently delete a user by confirming `delete`.
