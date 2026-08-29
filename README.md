# IQARUS TMS PRO

Private source repository for the IQARUS Training Management System.

## Repository safety

This source package intentionally excludes:

- The live SQLite database and all student records.
- Saved course lists and uploaded stamped lists.
- Passwords, email credentials, secret keys and environment files.
- Colab installer and backup files.

Never commit `db.sqlite3`, `.env`, passwords, tokens, generated student files or
uploaded documents to GitHub.

## Application

- Framework: Django 5.2
- Production server: Gunicorn
- Static files: WhiteNoise
- Database configuration: `DATABASE_URL`
- Spreadsheet generation: OpenPyXL and pandas
- QR generation: qrcode/Pillow

The application must be deployed to a Django-compatible hosting service with a
production database. GitHub stores the source code; GitHub Pages does not run
the Django application.

Production hosting configuration and transfer of approved live data will be
handled as a separate deployment step.
