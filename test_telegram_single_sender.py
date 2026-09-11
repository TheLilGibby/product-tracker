"""
Offline checks for the one-sender rule: TELEGRAM_ALERTS_ENABLED=0 stops every
Telegram send, the testing config can never post, and INSTANCE_LABEL prefixes
each post. No network: requests.post is replaced and any call to it is a
failure in the disabled cases.

    python test_telegram_single_sender.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

failures = []
calls = []


def check(label, ok):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}")
    if not ok:
        failures.append(label)


class _Resp:
    ok = True
    status_code = 200
    text = '{"ok": true}'

    def json(self):
        return {"ok": True}


def fake_post(url, **kwargs):
    calls.append((url, kwargs))
    return _Resp()


def main():
    import app.notifications.telegram as tg
    tg.requests.post = fake_post

    # --- CLI path (no app context): environment variables rule ---------------
    os.environ['TELEGRAM_BOT_TOKEN'] = '1:test'
    os.environ['TELEGRAM_CHAT_ID'] = '-100'
    os.environ.pop('INSTANCE_LABEL', None)

    os.environ['TELEGRAM_ALERTS_ENABLED'] = '0'
    calls.clear()
    check('disabled: is_configured() is False', tg.TelegramNotifier.is_configured() is False)
    check('disabled: send_message() returns False', tg.TelegramNotifier.send_message('hi') is False)
    check('disabled: send_photo_file() returns False',
          tg.TelegramNotifier.send_photo_file(b'png', caption='c') is False)
    check('disabled: the Bot API was never called', calls == [])

    os.environ['TELEGRAM_ALERTS_ENABLED'] = '1'
    calls.clear()
    check('enabled: is_configured() is True', tg.TelegramNotifier.is_configured() is True)
    check('enabled: send_message() posts', tg.TelegramNotifier.send_message('hi') is True and len(calls) == 1)
    check('enabled, no label: text is untouched', calls and calls[0][1]['json']['text'] == 'hi')

    os.environ['INSTANCE_LABEL'] = 'live <1>'
    calls.clear()
    tg.TelegramNotifier.send_message('hi')
    check('label: sendMessage text is prefixed and HTML-escaped',
          calls and calls[0][1]['json']['text'] == '[live &lt;1&gt;] hi')
    calls.clear()
    tg.TelegramNotifier.send_photo_file(b'png', caption='snap')
    check('label: sendPhoto caption is prefixed',
          calls and calls[0][1]['data']['caption'] == '[live &lt;1&gt;] snap')
    os.environ.pop('INSTANCE_LABEL')

    for raw in ('false', 'no', 'off', ''):
        os.environ['TELEGRAM_ALERTS_ENABLED'] = raw
        check(f'TELEGRAM_ALERTS_ENABLED={raw!r} disables', tg.telegram_alerts_enabled() is False)
    os.environ['TELEGRAM_ALERTS_ENABLED'] = '1'

    # --- testing config: can never post, whatever .env says ------------------
    from app import create_app
    app = create_app('testing')
    with app.app_context():
        calls.clear()
        check('testing config: is_configured() is False', tg.TelegramNotifier.is_configured() is False)
        check('testing config: send_message() returns False', tg.TelegramNotifier.send_message('hi') is False)
        check('testing config: no Bot API call', calls == [])

    # --- default config honours the flag from the environment ---------------
    os.environ['TELEGRAM_ALERTS_ENABLED'] = '0'
    from importlib import reload
    # `app.config` as an attribute is the config dict re-exported by the package;
    # the module itself lives in sys.modules.
    config_module = sys.modules['app.config']
    reload(config_module)
    check('default config: TELEGRAM_ALERTS_ENABLED=0 read as False',
          config_module.Config.TELEGRAM_ALERTS_ENABLED is False)
    os.environ['TELEGRAM_ALERTS_ENABLED'] = '1'
    reload(config_module)
    check('default config: TELEGRAM_ALERTS_ENABLED=1 read as True',
          config_module.Config.TELEGRAM_ALERTS_ENABLED is True)

    print()
    if failures:
        print(f"FAIL ({len(failures)} failure(s))")
        return 1
    print("PASS (0 failure(s))")
    return 0


if __name__ == '__main__':
    sys.exit(main())
