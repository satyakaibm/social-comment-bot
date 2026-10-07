"""Opt-in real-browser integration checks, using only a disposable database and fake mail.

Run with CMT_BROWSER_TESTS=1 and Playwright available to Node (NODE_PATH if needed).
"""
import os
import re
import subprocess
import threading
from pathlib import Path

import pytest
from flask import jsonify
from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server

from app import config, dashboard, db, mailer


@pytest.mark.skipif(os.environ.get('CMT_BROWSER_TESTS') != '1', reason='Opt-in Playwright browser integration')
def test_profile_browser_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'comments.db')
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(config, 'AVATAR_DIR', tmp_path / 'avatars')
    monkeypatch.setattr(config, 'DASHBOARD_USERNAME', 'admin')
    monkeypatch.setattr(config, 'DASHBOARD_PASSWORD_HASH', generate_password_hash('test-password'))
    monkeypatch.setattr(config, 'EMAIL_CODE_RESEND_SECONDS', 1)
    monkeypatch.setattr(mailer, 'configured', lambda: True)
    sent = []
    monkeypatch.setattr(mailer, 'send', lambda **kw: sent.append(kw))
    db.init_db()
    app = dashboard.create_app()

    @app.get('/test-mail')
    def mail():
        return jsonify(code=re.search(r'code is: (\d{6})', sent[-1]['body']).group(1))

    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run([os.environ.get('CMT_NODE', 'node'),
            str(Path(__file__).parent / 'browser/profile_sanity.cjs'),
            f'http://127.0.0.1:{server.server_port}'], capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
