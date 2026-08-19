from src.utils.privacy import redact_for_trace


def test_redacts_pii_and_keys_recursively():
    payload = {
        "email": "ana@example.com",
        "cpf": "123.456.789-00",
        "nested": ["Ligue (62) 99999-0000", "sk-lf-abcdefgh123456"],
    }

    redacted = redact_for_trace(payload)

    rendered = str(redacted)
    assert "ana@example.com" not in rendered
    assert "123.456.789-00" not in rendered
    assert "99999-0000" not in rendered
    assert "abcdefgh123456" not in rendered


def test_does_not_redact_uuid_fragments_as_phone_numbers():
    node_id = "scenario-f5a9e7df-5483-4458-a6ed-923961d84e53.questionnaire.001"

    assert redact_for_trace(node_id) == node_id
