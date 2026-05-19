"""Static admin dashboard asset tests."""

from pathlib import Path


def test_admin_dashboard_uses_split_assets_and_tabs():
    html = Path("app/static/admin.html").read_text()

    assert 'href="/static/admin.css"' in html
    assert 'src="/static/admin.js"' in html
    assert "<style>" not in html
    assert "<script>" not in html

    for tab in ["command", "contractors", "calls", "numbers", "subscriptions", "audit"]:
        assert f'data-tab="{tab}"' in html
