# Reproducible development entry point

Use Python 3.11+ in an isolated virtual environment:

```sh
cd backend
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest tests/
```

Dependencies have bounded major versions, not a fully pinned production lock. Test and lock the resolved environment before deployment. Engine tests do not establish authentication, tenant isolation or extraction accuracy. `seed_demo.py` writes a demo database; run it only against disposable data. Do not expose the unauthenticated MVP to the internet. Postgres isolation, authentication, async extraction and document storage remain separate production work.
