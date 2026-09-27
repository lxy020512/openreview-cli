from openreview_cli.gateway.redaction import RedactingFilter, redact_key


class TestRedactKey:
    def test_returns_empty_string_for_empty_key(self) -> None:
        assert redact_key("") == ""
        assert redact_key(None) == ""  # type: ignore[arg-type]

    def test_returns_all_stars_for_short_key(self) -> None:
        assert redact_key("ab") == "**"

    def test_keeps_first_four_chars_by_default(self) -> None:
        assert redact_key("abcdefgh") == "abcd****"

    def test_respects_visible_param(self) -> None:
        assert redact_key("abcdefgh", visible=2) == "ab******"

    def test_visibility_zero(self) -> None:
        assert redact_key("abcdefgh", visible=0) == "********"


class TestRedactingFilter:
    def test_redacts_matching_string_in_record(self) -> None:
        filt = RedactingFilter(["OPENAI_API_KEY"])
        import logging

        record = logging.LogRecord(
            "test", logging.INFO, "", 0, "Using OPENAI_API_KEY=sk-abc123", (), None
        )
        assert filt.filter(record)
        assert "OPENAI_API_KEY" not in record.msg
        assert record.msg == "Using OPEN**********=sk-abc123"

    def test_passes_through_clean_messages(self) -> None:
        filt = RedactingFilter(["OPENAI_API_KEY"])
        import logging

        record = logging.LogRecord("test", logging.INFO, "", 0, "Everything is fine", (), None)
        assert filt.filter(record)
        assert record.msg == "Everything is fine"

    def test_redacts_multiple_patterns(self) -> None:
        filt = RedactingFilter(["sk-", "OPENAI"])
        import logging

        record = logging.LogRecord("test", logging.INFO, "", 0, "key: sk-abc with OPENAI", (), None)
        assert filt.filter(record)
        assert "sk-" not in record.msg
        assert "OPENAI" not in record.msg

    def test_redaction_preserves_surrounding_text(self) -> None:
        import logging

        from openreview_cli.gateway.redaction import RedactingFilter

        f = RedactingFilter(patterns=["sk-secret123"])
        record = logging.LogRecord(
            "test",
            logging.INFO,
            __file__,
            1,
            "Using sk-secret123 for slot reasoning",
            (),
            None,
        )
        f.filter(record)
        assert "sk-secret123" not in record.getMessage()
        assert "for slot reasoning" in record.getMessage()


class TestRedactText:
    def test_masks_a_literal_env_name_and_an_sk_shaped_value(self) -> None:
        from openreview_cli.gateway import redaction

        out = redaction.redact_text("OPENAI_API_KEY=sk-test-CANARY-123")
        assert "sk-test-CANARY-123" not in out
        assert "OPENAI_API_KEY" not in out
        # redact_key keeps 4 chars; the body beyond them is gone.
        assert "CANAR" not in out and "CANARY" not in out
        assert "***t" in out  # positive control: the 4th char survived, the rest is masked

    def test_leaves_a_benign_non_secret_token_untouched(self) -> None:
        from openreview_cli.gateway import redaction

        assert "us-east-1" in redaction.redact_text("region us-east-1 unreachable")

    def test_a_credential_without_an_sk_pk_rk_shape_is_not_matched(self) -> None:
        """Documented limitation (A3): with `register_secret` cut, a credential with
        no `sk-`/`pk-`/`rk-` shape is NOT masked. This node pins the gap so a future
        fix is a deliberate change, not an accident."""
        from openreview_cli.gateway import redaction

        out = redaction.redact_text("boom: key CANARY-123-NONSECRETSHAPE rejected")
        assert "CANARY-123-NONSECRETSHAPE" in out
        assert "rejected" in out
