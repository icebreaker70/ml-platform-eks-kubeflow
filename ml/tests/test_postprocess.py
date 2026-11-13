from idcard.postprocess import clean_id_number, mask_rrn, postprocess_field


def test_mask_rrn():
    assert mask_rrn("900101-1234567") == "900101-1******"
    assert mask_rrn("9001011234567") == "900101-1******"
    assert mask_rrn("주민번호 900101 - 1234567") == "주민번호 900101-1******"


def test_clean_id_number_fixes_ocr_confusions():
    assert clean_id_number("9O0l01-0234S67", "card_rrc") == "900101-0234567"


def test_clean_passport_number():
    assert clean_id_number("M1234S67O", "card_passport") == "M12345670"


def test_postprocess_id_number_is_masked():
    out = postprocess_field("id_number", "770112-0657923", "card_driver")
    assert out["text"] == "770112-0******"
    assert out["valid_format"] is True


def test_rrn_masked_in_any_field():
    out = postprocess_field("name", "주민등록증 임희지 731016-0656115", "card_rrc")
    assert "0656115" not in out["text"]
    assert "731016-0******" in out["text"]


def test_postprocess_passport_not_masked():
    out = postprocess_field("id_number", "M31379567", "card_passport")
    assert out == {"text": "M31379567", "valid_format": True}
