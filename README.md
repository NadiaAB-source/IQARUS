# IQARUS TMS PRO

Current source checkpoint: 17 September 2026, based on `IQARUS_SIMPLE_REBUILD (30)`.

This repository contains the Django application source only. It intentionally excludes the live SQLite database, student records, uploaded and generated files, backups, passwords, email credentials, and Colab installer files.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py runserver
```

Configure deployment values through environment variables, including `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `DATABASE_URL`, and the email settings used by the application.

