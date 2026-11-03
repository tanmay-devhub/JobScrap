import logging

import pytest

from jobmonitor.security import RedactingFilter, html_to_text, neutralize_cell, redact, safe_link


@pytest.mark.parametrize(
    "value",
    [
        "=1+1",
        "+cmd|' /C calc'!A0",
        "-2+3",
        "@SUM(A1)",
        "\t=evil",
        "\r=evil",
        '  =HYPERLINK("x")',
        "＝1+1",
    ],
)
def test_formula_prefixes_neutralized(value):
    assert neutralize_cell(value).startswith("'")


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Backend Engineer", "Backend Engineer"),
        (None, ""),
        (3, "3"),
        ("a\x00b\x07c", "abc"),
        ("C++ Developer", "C++ Developer"),
    ],
)
def test_plain_values_untouched(value, expected):
    assert neutralize_cell(value) == expected


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://jobs.lever.co/x/1", True),
        ("http://example.com/a?b=1", True),
        ("javascript:alert(1)", False),
        ('=HYPERLINK("http://x")', False),
        ("data:text/html,x", False),
        ("https://u:p@example.com/", False),
        ("https://example.com/a b", False),
        ("", False),
        (None, False),
    ],
)
def test_safe_link(url, ok):
    assert safe_link(url) == (url if ok else "")


def test_html_to_text_strips_scripts():
    text = html_to_text("<p>Hello <b>world</b></p><script>steal()</script><style>x{}</style>")
    assert text == "Hello world"


@pytest.mark.parametrize(
    "secret,leak",
    [
        (
            '{"private_key": "-----BEGIN PRIVATE KEY-----\\nMIIabc\\n-----END PRIVATE KEY-----\\n"}',
            "MIIabc",
        ),
        ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
        ("token=ghp_ABCDEFGHIJKLMNOPQRSTUVWX12345", "ghp_ABCDEFGHIJKLMNOPQRSTUVWX12345"),
        ("https://user:hunter2@example.com/x", "hunter2"),
        ("GET https://api.example/x?key=AIzaSECRET&q=1", "AIzaSECRET"),
        ("access ya29.a0AfH6SMBsecret", "ya29.a0AfH6SMBsecret"),
        ("password = s3cr3t!", "s3cr3t!"),
    ],
)
def test_redaction(secret, leak):
    assert leak not in redact(secret)


def test_logging_filter_redacts_args(caplog):
    logger = logging.getLogger("t-redact")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO, logger="t-redact"):
        logger.info("calling with %s", "Bearer supersecrettoken")
    assert "supersecrettoken" not in caplog.text and "[REDACTED]" in caplog.text
