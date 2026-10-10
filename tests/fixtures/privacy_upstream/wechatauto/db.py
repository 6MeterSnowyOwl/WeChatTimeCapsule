"""Explicit synthetic SQLite only; private marker never belongs in a report."""
import os
from pathlib import Path
import sqlite3
import sys
from .logger import wxlog


def emit_private_observation():
    marker = os.environ['SYNTHETIC_PRIVATE_MARKER']
    print(marker)
    print(marker, file=sys.stderr)
    wxlog.info(marker)


def list_accounts():
    emit_private_observation()
    return [dict(account='synthetic-locator', wxid='synthetic-stable', last_activity=1)]


class WeChatDB:
    def __init__(self, *, account):
        emit_private_observation()
        if account != 'synthetic-locator':
            raise ValueError('unexpected synthetic account')
        if os.environ.get('SYNTHETIC_UPSTREAM_FAILURE') == 'constructor':
            raise RuntimeError(os.environ['SYNTHETIC_PRIVATE_MARKER'])
        self.account = account
        self.root = Path(os.environ['SYNTHETIC_DB_ROOT'])
        self._db_files = [('contact/contact.db', str(self.root / 'contact.db'), None)]

    def get_self_info(self):
        emit_private_observation()
        if os.environ.get('SYNTHETIC_UPSTREAM_FAILURE') == 'query':
            raise RuntimeError(os.environ['SYNTHETIC_PRIVATE_MARKER'])
        return dict(nick_name='Synthetic', username='synthetic-stable')

    def _open(self, relative):
        emit_private_observation()
        path = self.root / ('contact.db' if relative == 'contact/contact.db' else 'message.db')
        return sqlite3.connect(f'file:{path}?mode=ro', uri=True)

    def _message_dbs(self):
        return ['message/message_0.db']
